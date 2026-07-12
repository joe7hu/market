from __future__ import annotations

from contextlib import closing
import sys

import psycopg
import pytest

from investment_panel.database.migrations import HEAD_REVISION, downgrade_database, main as migration_main, upgrade_database
from investment_panel.database.authority import close_cached_runtimes, runtime_for_url
from investment_panel.database.runtime import DatabaseRuntime


@pytest.fixture
def postgres_dsn(postgresql) -> str:
    info = postgresql.info
    credentials = info.user if not info.password else f"{info.user}:{info.password}"
    return f"postgresql://{credentials}@{info.host}:{info.port}/{info.dbname}"


@pytest.fixture
def migrated_postgres_dsn(postgres_dsn: str) -> str:
    upgrade_database(postgres_dsn)
    return postgres_dsn


def test_migration_creates_layered_postgresql_authority(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    with closing(psycopg.connect(postgres_dsn)) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        schemas = {
            row[0]
            for row in connection.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name = ANY(%s)",
                [["catalog", "ingest", "raw", "analysis", "app", "ops"]],
            ).fetchall()
        }
        tables = connection.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = ANY(%s)",
            [["catalog", "ingest", "raw", "analysis", "app", "ops"]],
        ).fetchone()[0]
    assert revision == HEAD_REVISION
    assert schemas == {"catalog", "ingest", "raw", "analysis", "app", "ops"}
    assert tables >= 35


def test_migration_round_trip_removes_only_market_schemas(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    downgrade_database(postgres_dsn)
    with closing(psycopg.connect(postgres_dsn)) as connection:
        schemas = connection.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name = ANY(%s)",
            [["catalog", "ingest", "raw", "analysis", "app", "ops"]],
        ).fetchall()
    assert schemas == []


def test_existing_0001_database_upgrades_through_forward_migrations(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn, "20260711_0001")
    with closing(psycopg.connect(postgres_dsn)) as connection:
        before = connection.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'analysis' AND table_name = 'option_outcome' AND column_name = 'current_return'"
        ).fetchone()[0]
    assert before == 0

    upgrade_database(postgres_dsn)
    with closing(psycopg.connect(postgres_dsn)) as connection:
        after = connection.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'analysis' AND table_name = 'option_outcome' AND column_name = 'current_return'"
        ).fetchone()[0]
        constraint = connection.execute(
            "SELECT count(*) FROM information_schema.table_constraints "
            "WHERE constraint_schema = 'app' AND table_name = 'catalyst' "
            "AND constraint_name = 'uq_app_catalyst_market_event'"
        ).fetchone()[0]
        heartbeat = connection.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'ops' AND table_name = 'job_run' AND column_name = 'heartbeat_at'"
        ).fetchone()[0]
        authority_column = connection.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'analysis' AND table_name = 'strategy_revision' "
            "AND column_name = 'authority_group'"
        ).fetchone()[0]
    assert (after, constraint, heartbeat, authority_column) == (1, 1, 1, 1)


def test_strategy_authority_migration_reconciles_duplicate_active_revisions(
    postgres_dsn: str,
) -> None:
    upgrade_database(postgres_dsn, "20260711_0003")
    with closing(psycopg.connect(postgres_dsn)) as connection:
        base = connection.execute(
            "INSERT INTO analysis.strategy_revision "
            "(strategy_key, revision, name, status, parameters, promoted_at) "
            "VALUES ('options-radar-core', 1, 'core', 'active', '{}', now() - interval '1 day') "
            "RETURNING id"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO analysis.strategy_revision "
            "(strategy_key, revision, name, status, parameters, supersedes_id, promoted_at) "
            "VALUES ('options-radar-core__agent_existing', 1, 'candidate', 'active', '{}', %s, now())",
            [base],
        )
        connection.commit()

    upgrade_database(postgres_dsn)

    with closing(psycopg.connect(postgres_dsn)) as connection:
        rows = connection.execute(
            "SELECT strategy_key, status, authority_group FROM analysis.strategy_revision "
            "ORDER BY id"
        ).fetchall()
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                "INSERT INTO analysis.strategy_revision "
                "(strategy_key, revision, name, status, parameters, authority_group) "
                "VALUES ('duplicate-active', 1, 'duplicate', 'active', '{}', 'options-radar-core')"
            )
    assert rows == [
        ("options-radar-core", "superseded", "options-radar-core"),
        ("options-radar-core__agent_existing", "active", "options-radar-core"),
    ]


def test_runtime_commits_writes_and_serves_read_only_transactions(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn, min_size=1, max_size=2)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            row = connection.execute(
                "INSERT INTO catalog.instrument (symbol, asset_class) VALUES (%s, %s) RETURNING id",
                ["NVDA", "equity"],
            ).fetchone()
        with runtime.read() as connection:
            stored = connection.execute("SELECT symbol, asset_class FROM catalog.instrument WHERE id = %s", [row["id"]]).fetchone()
            read_only = connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
        assert stored == {"symbol": "NVDA", "asset_class": "equity"}
        assert read_only == "on"
    finally:
        runtime.close()


def test_runtime_job_lock_is_process_safe(migrated_postgres_dsn: str) -> None:
    first = DatabaseRuntime(migrated_postgres_dsn, min_size=1, max_size=1)
    second = DatabaseRuntime(migrated_postgres_dsn, min_size=1, max_size=1)
    first.open()
    second.open()
    try:
        with first.job_lock("options-radar") as first_acquired:
            with second.job_lock("options-radar") as second_acquired:
                assert first_acquired is True
                assert second_acquired is False
        with second.job_lock("options-radar") as acquired_after_release:
            assert acquired_after_release is True
    finally:
        first.close()
        second.close()


def test_runtime_requires_expected_schema_revision(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        runtime.check_schema_revision(HEAD_REVISION)
        with pytest.raises(RuntimeError, match="expected future_revision"):
            runtime.check_schema_revision("future_revision")
    finally:
        runtime.close()


def test_migration_cli_upgrades_configured_database(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKET_DATABASE_URL", postgres_dsn)
    monkeypatch.setattr(sys, "argv", ["market-db-migrate"])
    migration_main()
    with closing(psycopg.connect(postgres_dsn)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD_REVISION


def test_runtime_rejects_non_postgresql_authority() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        DatabaseRuntime("data/investment.duckdb")


def test_authority_reuses_and_closes_process_runtime(migrated_postgres_dsn: str) -> None:
    first = runtime_for_url(migrated_postgres_dsn)
    second = runtime_for_url(migrated_postgres_dsn)
    assert first is second

    close_cached_runtimes()

    replacement = runtime_for_url(migrated_postgres_dsn)
    try:
        assert replacement is not first
    finally:
        close_cached_runtimes()
