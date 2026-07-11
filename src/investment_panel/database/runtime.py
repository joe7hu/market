"""PostgreSQL connection-pool and transaction semantics.

The public interface intentionally exposes PostgreSQL behavior instead of
emulating DuckDB connections. Callers choose a read or write transaction and
the runtime enforces bounded pool, statement, and lock waits.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class RuntimeProfile:
    statement_timeout_ms: int
    lock_timeout_ms: int = 2_000


API_PROFILE = RuntimeProfile(statement_timeout_ms=3_000)
JOB_PROFILE = RuntimeProfile(statement_timeout_ms=900_000)


class DatabaseRuntime:
    """Own the process-wide PostgreSQL pool and its transaction interface."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 8,
        pool_timeout_seconds: float = 2.0,
    ) -> None:
        if not dsn.startswith(("postgresql://", "postgresql+psycopg://")) and not dsn.startswith("dbname="):
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
