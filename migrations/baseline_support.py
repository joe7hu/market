"""Bootstrap roles and adopt the last historical schema without copying facts."""

from __future__ import annotations

import os
import re
from pathlib import Path

from sqlalchemy import text


def prepare_roles(connection) -> None:
    login = os.environ.get("MARKET_APP_LOGIN_ROLE", "").strip()
    password = os.environ.get("MARKET_APP_DATABASE_PASSWORD", "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", login):
        raise RuntimeError("MARKET_APP_LOGIN_ROLE must name the explicitly configured application login")
    if password and len(password) < 16:
        raise RuntimeError("MARKET_APP_DATABASE_PASSWORD must contain at least 16 characters")
    for role in ("market_app", "market_research_signer", "market_migrator"):
        if not connection.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}).scalar():
            connection.connection.driver_connection.execute(
                f'CREATE ROLE "{role}" NOLOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS '
                'NOCREATEROLE NOCREATEDB NOREPLICATION'
            )
    if password and login == "market_app":
        escaped = password.replace("'", "''")
        connection.connection.driver_connection.execute(f"ALTER ROLE market_app LOGIN PASSWORD '{escaped}'")
    if login != "market_app":
        connection.connection.driver_connection.execute(f'GRANT market_app TO "{login}"')
    validate_roles(connection)


def validate_roles(connection) -> None:
    login = os.environ.get("MARKET_APP_LOGIN_ROLE", "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", login):
        raise RuntimeError("MARKET_APP_LOGIN_ROLE must name the explicitly configured application login")
    for role in {login, "market_app", "market_research_signer", "market_migrator"}:
        row = connection.execute(text("""SELECT rolcanlogin,rolsuper,rolbypassrls,rolinherit,
            rolcreaterole,rolcreatedb,rolreplication FROM pg_roles WHERE rolname=:role"""), {"role": role}).first()
        protected = role in {"market_research_signer", "market_migrator"}
        if row is None or any(row[1:]) or (protected and row[0]) or (role == login and not row[0]):
            raise RuntimeError("configured application login or protected role has unsafe attributes")
    if login in {"market_research_signer", "market_migrator"}:
        raise RuntimeError("application login cannot be a protected role")
    unsafe = connection.execute(text("""SELECT count(*) FROM pg_auth_members m
        JOIN pg_roles member ON member.oid=m.member JOIN pg_roles parent ON parent.oid=m.roleid
        WHERE member.rolname IN (:login,'market_app')
          AND (member.rolname='market_app' OR parent.rolname<>'market_app' OR m.admin_option)"""), {"login": login}).scalar()
    if unsafe:
        raise RuntimeError("configured application login has an unsafe role membership path")
    if login != "market_app" and not connection.execute(text(
        "SELECT pg_has_role(:login,'market_app','MEMBER')"), {"login": login}
    ).scalar():
        raise RuntimeError("configured application login must be a member of market_app")


def install_secrets(connection) -> None:
    for variable, table in (
        ("MARKET_RESEARCH_EVALUATOR_SIGNING_KEY", "research_evaluator_signing_secret"),
        ("MARKET_PHASE4_ALLOCATION_SIGNING_KEY", "phase4_allocation_signing_secret"),
    ):
        key = os.environ.get(variable, "").strip()
        if not key:
            continue
        if len(key) < 16:
            raise RuntimeError(f"{variable} must contain at least 16 characters")
        connection.execute(text(f"""INSERT INTO analysis.{table}(singleton,secret)
            VALUES(true,convert_to(:secret,'UTF8')) ON CONFLICT(singleton) DO NOTHING"""), {"secret": key})


def repair_legacy_schema(connection) -> None:
    """One transactional maintenance-window repair; no fact rows are removed."""
    connection.connection.driver_connection.execute("""
        GRANT SELECT, INSERT, UPDATE ON analysis.event_decision_packet,
            analysis.event_scout_event, app.decision_truth TO market_app;
        GRANT SELECT, INSERT ON app.trade_journal TO market_app;
        GRANT SELECT, INSERT, UPDATE ON app.alert TO market_app;
        DROP TABLE app.review_page_snapshot;
        DROP INDEX ops.ix_storage_archive_manifest_reference_source;
        DROP INDEX analysis.ix_research_evidence_trial_result;
        CREATE INDEX ix_analysis_decision_recent_page ON analysis.decision(as_of DESC,id DESC);
        CREATE INDEX ix_analysis_option_decision_contract_predecessor
            ON analysis.option_decision(contract_id,decision_id);
    """)
    # The live compacted schema predates one conservation check. Reuse the
    # baseline definition rather than maintaining a second function body.
    schema = Path(__file__).with_name("baseline.sql").read_text()
    guard = re.search(
        r"CREATE FUNCTION analysis\.enforce_phase4_review_item_guard\(\).*?AS \$(\w*)\$.*?\$\1\$;",
        schema, re.DOTALL,
    )
    if guard is None:
        raise RuntimeError("baseline funding guard is missing")
    connection.connection.driver_connection.execute(guard[0].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
