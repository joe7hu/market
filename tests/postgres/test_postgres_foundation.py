from __future__ import annotations

from contextlib import closing
import sys

import psycopg
import pytest
from psycopg.errors import RaiseException
from psycopg.sql import Identifier, Literal, SQL

from investment_panel.infrastructure.postgres.migrations import HEAD_REVISION, downgrade_database, main as migration_main, upgrade_database
from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.authority import close_cached_runtimes, runtime_for_url
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.application.read_models.loaders import load_panel_data
from conftest import typed_config


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


def test_storage_archive_privileges_are_available_to_application_role(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    with closing(psycopg.connect(postgres_dsn)) as connection:
        privileges = connection.execute(
            """
            SELECT has_schema_privilege('market_app', 'ops', 'USAGE'),
                   has_table_privilege('market_app', 'ops.storage_archive_manifest', 'SELECT'),
                   has_table_privilege('market_app', 'ops.storage_archive_manifest', 'INSERT'),
                   has_table_privilege('market_app', 'ops.storage_archive_manifest', 'UPDATE'),
                   has_table_privilege('market_app', 'ops.storage_archive_checkpoint', 'SELECT'),
                   has_table_privilege('market_app', 'ops.storage_archive_checkpoint', 'INSERT'),
                   has_table_privilege('market_app', 'ops.storage_archive_checkpoint', 'UPDATE'),
                   has_table_privilege('market_app', 'ops.storage_archive_manifest_reference', 'SELECT'),
                   has_table_privilege('market_app', 'ops.storage_archive_manifest_reference', 'INSERT'),
                   has_table_privilege('market_app', 'ops.storage_archive_manifest_reference', 'UPDATE'),
                   has_table_privilege('market_app', 'ops.storage_archive_manifest_reference', 'DELETE'),
                   has_sequence_privilege('market_app', 'ops.storage_archive_manifest_id_seq', 'USAGE')
            """
        ).fetchone()
        assert tuple(privileges) == (True,) * 12


def test_runtime_read_path_privileges_are_available_to_application_role(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        privileges = connection.execute(
            """SELECT has_table_privilege('market_app', 'analysis.agent_experiment', 'SELECT'),
                      has_table_privilege('market_app', 'analysis.agent_experiment', 'INSERT'),
                      has_table_privilege('market_app', 'analysis.agent_experiment', 'UPDATE'),
                      has_table_privilege('market_app', 'app.thesis_review_event', 'SELECT'),
                      has_table_privilege('market_app', 'app.thesis_review_event', 'INSERT'),
                      has_table_privilege('market_app', 'app.option_history_policy', 'SELECT'),
                      has_table_privilege('market_app', 'app.option_history_policy', 'INSERT'),
                      has_table_privilege('market_app', 'app.option_history_policy', 'UPDATE'),
                      has_sequence_privilege('market_app', 'app.thesis_review_event_id_seq', 'USAGE'),
                      COALESCE((SELECT indexrel.indisvalid
                                FROM pg_index indexrel
                                JOIN pg_class index_class ON index_class.oid = indexrel.indexrelid
                                JOIN pg_namespace index_schema ON index_schema.oid = index_class.relnamespace
                                WHERE index_schema.nspname = 'analysis'
                                  AND index_class.relname = 'ix_option_relative_value_generation_contract'), FALSE)"""
        ).fetchone()
        assert tuple(privileges) == (True,) * 10


def test_empty_ci_style_migration_bootstraps_only_a_safe_application_login(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ci_login = "phase1_ci_login"
    ci_password = "phase1-ci-bootstrap-password"
    with closing(psycopg.connect(postgres_dsn)) as connection:
        connection.execute(
            SQL("DROP ROLE IF EXISTS {};").format(Identifier(ci_login))
        )
        connection.execute(
            SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS "
                "NOINHERIT NOCREATEROLE NOCREATEDB NOREPLICATION").format(
                Identifier(ci_login), Literal(ci_password),
            )
        )
        connection.commit()
    monkeypatch.setenv("MARKET_APP_LOGIN_ROLE", ci_login)
    monkeypatch.setenv("MARKET_APP_DATABASE_PASSWORD", "phase1-ci-bootstrap-password")

    try:
        upgrade_database(postgres_dsn)

        with closing(psycopg.connect(postgres_dsn)) as connection:
            role = connection.execute(
                """
                SELECT rolcanlogin, rolsuper, rolbypassrls, rolinherit,
                       rolcreaterole, rolcreatedb, rolreplication
                FROM pg_roles WHERE rolname = %s
                """,
                [ci_login],
            ).fetchone()
            membership = connection.execute(
                """
                SELECT pg_has_role(%s, 'market_app', 'member'),
                       pg_has_role(%s, 'market_research_signer', 'member')
                           OR pg_has_role(%s, 'market_migrator', 'member')
                """,
                [ci_login, ci_login, ci_login],
            ).fetchone()
            privileges = connection.execute(
                """
                SELECT has_function_privilege(
                           'market_app',
                           'analysis.write_research_evaluator_output(uuid,uuid,uuid,text,text,text,text,text,text,integer,boolean,jsonb,text)',
                           'EXECUTE'
                       ),
                       has_table_privilege('market_app', 'analysis.research_evaluator_output', 'SELECT'),
                       has_schema_privilege('market_app', 'ops', 'USAGE'),
                       has_table_privilege('market_app', 'ops.job_run', 'SELECT'),
                       has_table_privilege('market_app', 'ops.job_run', 'INSERT'),
                       has_table_privilege('market_app', 'ops.job_run', 'UPDATE'),
                       has_table_privilege('market_app', 'raw.quote_history', 'SELECT'),
                       has_table_privilege('market_app', 'raw.quote_confirmation', 'SELECT'),
                       has_table_privilege('market_app', 'raw.quote_fact_availability', 'SELECT'),
                       has_table_privilege('market_app', 'raw.price_bar', 'SELECT'),
                       has_table_privilege('market_app', 'raw.price_bar', 'INSERT'),
                       has_table_privilege('market_app', 'raw.price_bar', 'UPDATE'),
                       has_table_privilege('market_app', 'raw.quote', 'SELECT'),
                       has_table_privilege('market_app', 'raw.quote', 'INSERT'),
                       has_table_privilege('market_app', 'raw.quote', 'UPDATE'),
                       has_table_privilege('market_app', 'raw.price_bar_history', 'INSERT'),
                       has_table_privilege('market_app', 'raw.quote_history', 'INSERT'),
                       has_table_privilege('market_app', 'raw.price_bar_confirmation', 'INSERT'),
                       has_table_privilege('market_app', 'raw.quote_confirmation', 'INSERT'),
                       has_table_privilege('market_app', 'raw.price_bar_fact_availability', 'INSERT'),
                       has_table_privilege('market_app', 'raw.quote_fact_availability', 'INSERT'),
                       has_table_privilege('market_app', 'raw.price_bar_fact_availability', 'UPDATE'),
                       has_table_privilege('market_app', 'raw.quote_fact_availability', 'UPDATE'),
                       has_table_privilege('market_app', 'analysis.agent_task', 'SELECT'),
                       has_table_privilege('market_app', 'raw.broker_position_snapshot', 'SELECT'),
                       has_table_privilege('market_app', 'app.decision_inbox_item', 'SELECT'),
                       has_table_privilege('market_app', 'app.decision_inbox_sync_state', 'SELECT'),
                       has_table_privilege('market_app', 'app.decision_inbox_sync_state', 'INSERT'),
                       has_table_privilege('market_app', 'app.notification_outbox', 'SELECT'),
                       has_table_privilege('market_app', 'ingest.payload', 'SELECT'),
                       has_table_privilege('market_app', 'app.setting', 'SELECT'),
                       has_table_privilege('market_app', 'app.publication_content_item', 'SELECT'),
                       has_table_privilege('market_app', 'analysis.option_outcome', 'SELECT')
                """
            ).fetchone()

        assert tuple(role) == (True, False, False, False, False, False, False)
        assert tuple(membership) == (True, False)
        assert tuple(privileges) == (True, False, *([True] * 31))
    finally:
        with closing(psycopg.connect(postgres_dsn)) as connection:
            connection.execute(
                SQL("REVOKE market_app FROM {};").format(Identifier(ci_login))
            )
            connection.execute(
                SQL("DROP ROLE IF EXISTS {};").format(Identifier(ci_login))
            )
            connection.commit()


def test_clean_migration_fails_closed_without_a_configured_application_login(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MARKET_APP_LOGIN_ROLE", raising=False)
    monkeypatch.delenv("MARKET_APP_DATABASE_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="MARKET_APP_LOGIN_ROLE"):
        upgrade_database(postgres_dsn)


def test_mungermode_registration_sets_local_refresh_owner(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        IngestionRepository(runtime).register_source(
            "mungermode-market-valuations", name="Munger Mode", family="market_data",
            kind="market_valuation", origin="https://mungermode.com/api/v1/market/metrics",
        )
        with runtime.read() as connection:
            row = connection.execute(
                "SELECT operational_state, enabled, health_owner, freshness_seconds "
                "FROM ingest.source WHERE id = 'mungermode-market-valuations'"
            ).fetchone()
        assert tuple(row.values()) == ("active", True, "update_market_valuations", 86400)
    finally:
        runtime.close()


def test_migration_round_trip_removes_only_market_schemas(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("CREATE SCHEMA unrelated")
        connection.execute("CREATE TABLE unrelated.keep_me (value integer)")
        connection.execute("INSERT INTO unrelated.keep_me VALUES (1)")
    downgrade_database(postgres_dsn)
    with closing(psycopg.connect(postgres_dsn)) as connection:
        schemas = connection.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name = ANY(%s)",
            [["catalog", "ingest", "raw", "analysis", "app", "ops"]],
        ).fetchall()
        assert connection.execute("SELECT value FROM unrelated.keep_me").fetchone()[0] == 1
    assert schemas == []
    upgrade_database(postgres_dsn)
    with psycopg.connect(postgres_dsn) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD_REVISION


def test_baseline_contains_current_authority_columns_and_uniqueness(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    with closing(psycopg.connect(postgres_dsn)) as connection:
        after = connection.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'analysis' AND table_name = 'option_outcome' AND column_name = 'current_return'"
        ).fetchone()[0]
        constraint = connection.execute(
            "SELECT count(*) FROM pg_indexes "
            "WHERE schemaname = 'app' AND tablename = 'catalyst' "
            "AND indexname = 'uq_app_catalyst_current_event_key'"
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


def test_recovery_events_require_the_current_cohort(migrated_postgres_dsn: str) -> None:
    with psycopg.connect(migrated_postgres_dsn, autocommit=True) as connection:
        instrument_id = connection.execute(
            "INSERT INTO catalog.instrument (symbol, asset_class) VALUES ('AAOI', 'equity') RETURNING id"
        ).fetchone()[0]
        current = connection.execute(
            "SELECT id, status, required_qualified_dates FROM analysis.option_recovery_cohort "
            "WHERE objective_version = 'short_horizon_convex_v2' AND status = 'collecting'"
        ).fetchone()
        assert current is not None
        assert current[1:] == ("collecting", 5)
        event = connection.execute(
            "INSERT INTO analysis.option_event "
            "(instrument_id, detected_at, started_at, reference_price, event_low, severity_score) "
            "VALUES (%s, now(), now(), 25, 20, 30) RETURNING cohort_id, objective_version",
            [instrument_id],
        ).fetchone()
        assert event == (current[0], "short_horizon_convex_v2")
        retired = connection.execute(
            "INSERT INTO analysis.option_recovery_cohort "
            "(objective_version, code_version, started_at, status, required_qualified_dates) "
            "VALUES ('short_horizon_convex_v1', 'legacy-test', now(), 'retired', 5) RETURNING id"
        ).fetchone()[0]
        with pytest.raises(RaiseException, match="current v2 cohort"):
            connection.execute(
                "INSERT INTO analysis.option_event "
                "(instrument_id, detected_at, started_at, reference_price, event_low, severity_score, cohort_id) "
                "VALUES (%s, now(), now(), 25, 20, 30, %s)", [instrument_id, retired],
            )
        connection.execute("UPDATE analysis.option_recovery_cohort SET status = 'retired' WHERE id = %s", [current[0]])
        with pytest.raises(RaiseException, match="current options recovery cohort is required"):
            connection.execute(
                "INSERT INTO analysis.option_event "
                "(instrument_id, detected_at, started_at, reference_price, event_low, severity_score) "
                "VALUES (%s, now(), now(), 25, 20, 30)", [instrument_id],
            )


def test_strategy_evaluation_availability_does_not_invent_history(
    postgres_dsn: str,
) -> None:
    upgrade_database(postgres_dsn)
    with closing(psycopg.connect(postgres_dsn)) as connection:
        revision_id = connection.execute(
            """
            INSERT INTO analysis.strategy_revision
                (strategy_key, revision, name, status, parameters, authority_group)
            VALUES (
                'migration-evaluation', 1, 'migration evaluation', 'active', '{}',
                'migration-evaluation'
            )
            RETURNING id
            """
        ).fetchone()[0]
        historical_cutoff = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        evaluation_id = connection.execute(
            """
            INSERT INTO analysis.strategy_evaluation
                (strategy_revision_id, evaluation_type, evaluated_at, verdict, metrics)
            VALUES (%s, 'out_of_sample', now() - interval '1 year', 'pass', '{}')
            RETURNING id
            """,
            [revision_id],
        ).fetchone()[0]
        connection.commit()

    with closing(psycopg.connect(postgres_dsn)) as connection:
        available_at = connection.execute(
            "SELECT available_at FROM analysis.strategy_evaluation WHERE id = %s",
            [evaluation_id],
        ).fetchone()[0]

    assert available_at > historical_cutoff


def test_strategy_authority_is_append_only(
    postgres_dsn: str,
) -> None:
    upgrade_database(postgres_dsn)
    with closing(psycopg.connect(postgres_dsn)) as connection:
        revision_id = connection.execute(
            """
            INSERT INTO analysis.strategy_revision
                (strategy_key, revision, name, status, parameters, authority_group)
            VALUES ('immutable-strategy', 1, 'immutable strategy', 'active',
                    '{"model":"v1"}', 'immutable-strategy')
            RETURNING id
            """
        ).fetchone()[0]
        evaluation_id = connection.execute(
            """
            INSERT INTO analysis.strategy_evaluation
                (strategy_revision_id, evaluation_type, evaluated_at, verdict, metrics)
            VALUES (%s, 'out_of_sample', now(), 'pass', '{}')
            RETURNING id
            """,
            [revision_id],
        ).fetchone()[0]
        connection.commit()

    upgrade_database(postgres_dsn)
    with closing(psycopg.connect(postgres_dsn, autocommit=True)) as connection:
        with pytest.raises(RaiseException, match="evaluation authority is immutable"):
            connection.execute(
                "UPDATE analysis.strategy_evaluation SET verdict = 'fail' WHERE id = %s",
                [evaluation_id],
            )
        with pytest.raises(RaiseException, match="evaluation authority is immutable"):
            connection.execute(
                "DELETE FROM analysis.strategy_evaluation WHERE id = %s",
                [evaluation_id],
            )
        with pytest.raises(RaiseException, match="revision parameters are immutable"):
            connection.execute(
                "UPDATE analysis.strategy_revision "
                "SET parameters = '{\"model\":\"v2\"}' WHERE id = %s",
                [revision_id],
            )

        connection.execute(
            "UPDATE analysis.strategy_revision SET status = 'superseded' WHERE id = %s",
            [revision_id],
        )
        new_revision_id = connection.execute(
            """
            INSERT INTO analysis.strategy_revision
                (strategy_key, revision, name, status, parameters, authority_group,
                 supersedes_id)
            VALUES ('immutable-strategy', 2, 'immutable strategy', 'active',
                    '{"model":"v2"}', 'immutable-strategy', %s)
            RETURNING id
            """,
            [revision_id],
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO analysis.strategy_evaluation
                (strategy_revision_id, evaluation_type, evaluated_at, verdict, metrics)
            VALUES (%s, 'out_of_sample', clock_timestamp(), 'fail', '{}')
            """,
            [revision_id],
        )
        authority = connection.execute(
            """
            SELECT revision, parameters->>'model' AS model
            FROM analysis.strategy_revision
            WHERE strategy_key = 'immutable-strategy'
            ORDER BY revision
            """
        ).fetchall()
        evaluation_count = connection.execute(
            "SELECT count(*) FROM analysis.strategy_evaluation "
            "WHERE strategy_revision_id IN (%s, %s)",
            [revision_id, new_revision_id],
        ).fetchone()[0]

    assert authority == [(1, "v1"), (2, "v2")]
    assert evaluation_count == 2


def test_strategy_authority_allows_only_one_active_revision_per_group(migrated_postgres_dsn: str) -> None:
    with psycopg.connect(migrated_postgres_dsn, autocommit=True) as connection:
        base = connection.execute(
            "INSERT INTO analysis.strategy_revision "
            "(strategy_key, revision, name, status, parameters, authority_group) "
            "VALUES ('options-radar-core', 1, 'core', 'active', '{}', 'options-radar-core') RETURNING id"
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                "INSERT INTO analysis.strategy_revision "
                "(strategy_key, revision, name, status, parameters, authority_group) "
                "VALUES ('duplicate-active', 1, 'duplicate', 'active', '{}', 'options-radar-core')"
            )
        connection.execute("UPDATE analysis.strategy_revision SET status = 'superseded' WHERE id = %s", [base])
        connection.execute(
            "INSERT INTO analysis.strategy_revision "
            "(strategy_key, revision, name, status, parameters, authority_group, supersedes_id) "
            "VALUES ('options-radar-core__agent_existing', 1, 'candidate', 'active', '{}', 'options-radar-core', %s)",
            [base],
        )
        rows = connection.execute(
            "SELECT strategy_key, status, authority_group FROM analysis.strategy_revision "
            "WHERE authority_group = 'options-radar-core' ORDER BY id"
        ).fetchall()
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
                ["FNDX", "equity"],
            ).fetchone()
        with runtime.read() as connection:
            stored = connection.execute("SELECT symbol, asset_class FROM catalog.instrument WHERE id = %s", [row["id"]]).fetchone()
            read_only = connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
        assert stored == {"symbol": "FNDX", "asset_class": "equity"}
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


def test_panel_metadata_exposes_actual_and_expected_schema_revisions(migrated_postgres_dsn: str) -> None:
    panel_data = load_panel_data(typed_config(migrated_postgres_dsn), table_names=("source_health",))

    assert panel_data.status.ready is True
    assert panel_data.metadata["schema_revision"] == HEAD_REVISION
    assert panel_data.metadata["expected_schema_revision"] == HEAD_REVISION


def test_panel_readiness_rejects_deployed_schema_behind_source_head(migrated_postgres_dsn: str) -> None:
    with psycopg.connect(migrated_postgres_dsn) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'stale_revision'")
    try:
        panel_data = load_panel_data(typed_config(migrated_postgres_dsn), table_names=("source_health",))
        assert panel_data.status.ready is False
        assert panel_data.metadata["schema_revision"] == "stale_revision"
        assert panel_data.metadata["expected_schema_revision"] == HEAD_REVISION
    finally:
        with psycopg.connect(migrated_postgres_dsn) as connection:
            connection.execute("UPDATE alembic_version SET version_num = %s", [HEAD_REVISION])


def test_migration_cli_upgrades_configured_database(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKET_DATABASE_URL", postgres_dsn)
    monkeypatch.setattr(sys, "argv", ["market-db-migrate"])
    migration_main()
    with closing(psycopg.connect(postgres_dsn)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD_REVISION


def test_explicit_migration_dsn_overrides_market_database_environment(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MARKET_DATABASE_URL", "postgresql://127.0.0.1:1/not-the-test-database")

    upgrade_database(postgres_dsn)

    with closing(psycopg.connect(postgres_dsn)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD_REVISION


def test_runtime_rejects_non_postgresql_authority() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        DatabaseRuntime("sqlite:///data/retired.db")


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
