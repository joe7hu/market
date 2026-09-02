"""PostgreSQL connection-pool and transaction semantics.

The public interface intentionally exposes PostgreSQL behavior. Callers choose
a read or write transaction and
the runtime enforces bounded pool, statement, and lock waits.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from typing import Any, Iterator

from psycopg import Connection, IsolationLevel
from psycopg import errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class RuntimeProfile:
    statement_timeout_ms: int
    lock_timeout_ms: int = 2_000


API_PROFILE = RuntimeProfile(statement_timeout_ms=3_000)
JOB_PROFILE = RuntimeProfile(statement_timeout_ms=900_000)
APPLICATION_ROLE = "market_app"
EVALUATOR_WRITER_SIGNATURE = (
    "analysis.write_research_evaluator_output(uuid,uuid,uuid,text,text,text,text,text,text,"
    "integer,boolean,jsonb,text)"
)


class DatabaseRuntime:
    """Own the process-wide PostgreSQL pool and its transaction interface."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 8,
        pool_timeout_seconds: float = 5.0,
    ) -> None:
        # psycopg's safe ``make_conninfo`` form may begin with a keyword such
        # as ``user=`` rather than ``dbname=``. Accept it when it identifies a
        # PostgreSQL database, while continuing to reject non-PostgreSQL URLs.
        if not dsn.startswith(("postgresql://", "postgresql+psycopg://")) and "dbname=" not in dsn:
            raise ValueError("Market database URL must identify PostgreSQL")
        self.dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        self.pool_timeout_seconds = pool_timeout_seconds
        self.pool = ConnectionPool(
            conninfo=self.dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=pool_timeout_seconds,
            kwargs={"row_factory": dict_row},
            open=False,
        )

    def open(self) -> None:
        self.pool.open(wait=True, timeout=self.pool_timeout_seconds)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def read(self, profile: RuntimeProfile = API_PROFILE) -> Iterator[Connection[dict[str, Any]]]:
        with self.pool.connection() as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                _set_local_timeouts(connection, profile)
                yield connection

    @contextmanager
    def snapshot(self, profile: RuntimeProfile = API_PROFILE) -> Iterator[Connection[dict[str, Any]]]:
        """Read a related model bundle from one repeatable PostgreSQL snapshot."""
        with self.pool.connection() as connection:
            previous_isolation = connection.isolation_level
            previous_read_only = connection.read_only
            connection.isolation_level = IsolationLevel.REPEATABLE_READ
            connection.read_only = True
            try:
                with connection.transaction():
                    _set_local_timeouts(connection, profile)
                    yield connection
            finally:
                connection.read_only = previous_read_only
                connection.isolation_level = previous_isolation

    @contextmanager
    def transaction(self, profile: RuntimeProfile = API_PROFILE) -> Iterator[Connection[dict[str, Any]]]:
        with self.pool.connection() as connection:
            with connection.transaction():
                _set_local_timeouts(connection, profile)
                yield connection

    @contextmanager
    def job_lock(self, job_name: str) -> Iterator[bool]:
        """Hold one PostgreSQL session advisory lock for a complete job run."""

        with self.pool.connection() as connection:
            acquired = bool(
                connection.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
                    [job_name],
                ).fetchone()["acquired"]
            )
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", [job_name])
                    connection.commit()

    def check_schema_revision(self, expected_revision: str) -> None:
        with self.read() as connection:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        actual = str(row["version_num"]) if row else ""
        if actual != expected_revision:
            raise RuntimeError(f"PostgreSQL schema revision {actual or 'missing'}; expected {expected_revision}")


def _set_local_timeouts(connection: Connection[dict[str, Any]], profile: RuntimeProfile) -> None:
    connection.execute("SELECT set_config('statement_timeout', %s, true)", [f"{profile.statement_timeout_ms}ms"])
    connection.execute("SELECT set_config('lock_timeout', %s, true)", [f"{profile.lock_timeout_ms}ms"])


def activate_application_role(connection: Connection[dict[str, Any]]) -> None:
    """Run the protected evaluator call as the configured application role.

    The migration grants this role to the configured production login. The
    switch is scoped to the current transaction, so a NOINHERIT login must
    explicitly activate the role before ordinary research persistence. The
    evaluator writer is never reached through the migration role.
    """

    configured_login = os.environ.get("MARKET_APP_LOGIN_ROLE", "").strip()
    if not configured_login:
        raise RuntimeError("MARKET_APP_LOGIN_ROLE is required for evaluator authority")
    identity = connection.execute(
        """SELECT session_user, current_user, rolcanlogin, rolsuper,
                  rolbypassrls, rolinherit, rolcreaterole, rolcreatedb,
                  rolreplication,
                  pg_has_role(session_user, %s, 'MEMBER') AS role_membership
           FROM pg_roles WHERE rolname = session_user""",
        [APPLICATION_ROLE],
    ).fetchone()
    if identity is None or identity["session_user"] != configured_login:
        raise RuntimeError("configured PostgreSQL connection cannot activate market_app evaluator authority")
    if (
        not identity["rolcanlogin"]
        or identity["rolsuper"]
        or identity["rolbypassrls"]
        or identity["rolinherit"]
        or identity["rolcreaterole"]
        or identity["rolcreatedb"]
        or identity["rolreplication"]
    ):
        raise RuntimeError("configured PostgreSQL login has unsafe attributes or is not a member of market_app")
    protected = connection.execute(
        """SELECT count(*) AS role_count,
                  count(*) FILTER (
                      WHERE NOT rolcanlogin AND NOT rolsuper
                        AND NOT rolbypassrls AND NOT rolinherit
                        AND NOT rolcreaterole AND NOT rolcreatedb
                        AND NOT rolreplication
                  ) AS safe_count
           FROM pg_roles
           WHERE rolname IN ('market_research_signer', 'market_migrator')"""
    ).fetchone()
    membership = connection.execute(
        """WITH RECURSIVE role_graph(member, granted_role) AS (
               SELECT member, roleid FROM pg_auth_members
               UNION
               SELECT graph.member, membership.roleid
               FROM role_graph graph
               JOIN pg_auth_members membership ON membership.member = graph.granted_role
           )
           SELECT bool_or(protected_role.rolname IN ('market_research_signer', 'market_migrator'))
                      AS reaches_protected,
                  bool_or(protected_role.rolname <> 'market_app') AS reaches_unapproved
           FROM role_graph graph
           JOIN pg_roles protected_role ON protected_role.oid = graph.granted_role
           WHERE graph.member = (SELECT oid FROM pg_roles WHERE rolname = %s)""",
        [configured_login],
    ).fetchone()
    direct_membership = connection.execute(
        """SELECT EXISTS (
               SELECT 1
               FROM pg_auth_members
               WHERE member = (SELECT oid FROM pg_roles WHERE rolname = %s)
                 AND roleid = (SELECT oid FROM pg_roles WHERE rolname = 'market_app')
           ) OR (SELECT oid FROM pg_roles WHERE rolname = %s) =
               (SELECT oid FROM pg_roles WHERE rolname = 'market_app') AS direct_market_app,
           NOT EXISTS (
               SELECT 1
               FROM pg_auth_members
               WHERE member = (SELECT oid FROM pg_roles WHERE rolname = 'market_app')
           ) AS market_app_is_leaf""",
        [configured_login, configured_login],
    ).fetchone()
    if (
        protected is None
        or protected["role_count"] != 2
        or protected["safe_count"] != 2
        or membership is None
        or membership["reaches_protected"]
        or membership["reaches_unapproved"]
        or direct_membership is None
        or not direct_membership["direct_market_app"]
        or not direct_membership["market_app_is_leaf"]
    ):
        raise RuntimeError("configured application login has an unsafe role membership path")
    if identity["current_user"] != APPLICATION_ROLE and not identity["role_membership"]:
        raise RuntimeError("configured PostgreSQL login has unsafe attributes or is not a member of market_app")
    try:
        if identity["current_user"] != APPLICATION_ROLE:
            connection.execute("SET LOCAL ROLE market_app")
    except errors.InsufficientPrivilege as exc:
        raise RuntimeError(
            "configured PostgreSQL login cannot activate market_app evaluator authority"
        ) from exc
    ownership = connection.execute(
        """SELECT
               (SELECT count(*) FROM pg_class
                WHERE oid IN ('analysis.research_evaluator_signing_secret'::regclass,
                              'analysis.research_evaluator_output'::regclass)
                  AND relowner = (SELECT oid FROM pg_roles WHERE rolname = 'market_research_signer')) = 2
               AND (SELECT count(*) FROM pg_proc procedure
                    JOIN pg_namespace namespace ON namespace.oid = procedure.pronamespace
                    WHERE namespace.nspname = 'analysis'
                      AND procedure.proname IN (
                          'research_evaluator_signing_key',
                          'research_evaluator_output_hash_v2',
                          'research_evaluator_signature_payload',
                          'enforce_research_evaluator_output',
                          'enforce_research_evidence_manifest',
                          'enforce_validation_dossier_seal',
                          'enforce_research_trial_terminal_immutability',
                          'enforce_research_result_actual_availability',
                          'enforce_research_gate_actual_availability',
                          'enforce_research_universe_actual_availability',
                          'enforce_research_revision_promotion_hardened',
                          'enforce_research_revision_promotion',
                          'enforce_strategy_forecast_authority',
                          'research_evidence_complete',
                          'research_validation_evidence_complete'
                      )
                      AND procedure.proowner = (SELECT oid FROM pg_roles WHERE rolname = 'market_research_signer')) = 15
           AS ownership_valid"""
    ).fetchone()
    row = connection.execute(
        """SELECT current_user, pg_has_role(current_user, %s, 'USAGE') AS role_active,
                  has_function_privilege(current_user, %s, 'EXECUTE') AS writer_allowed
           """,
        [APPLICATION_ROLE, EVALUATOR_WRITER_SIGNATURE],
    ).fetchone()
    if (
        row is None
        or row["current_user"] != APPLICATION_ROLE
        or not row["role_active"]
        or not row["writer_allowed"]
        or ownership is None
        or not ownership["ownership_valid"]
    ):
        raise RuntimeError(
            "configured PostgreSQL connection cannot activate market_app evaluator authority"
        )
