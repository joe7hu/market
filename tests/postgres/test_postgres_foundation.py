from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
import sys

import psycopg
import pytest
from psycopg.errors import RaiseException
from psycopg.sql import Identifier, Literal, SQL

from investment_panel.database.migrations import HEAD_REVISION, downgrade_database, main as migration_main, upgrade_database
from investment_panel.database.authority import close_cached_runtimes, runtime_for_url
from investment_panel.database.runtime import DatabaseRuntime
from app.data_access.loaders import load_panel_data
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
                       has_table_privilege('market_app', 'ops.job_run', 'UPDATE')
                """
            ).fetchone()

        assert tuple(role) == (True, False, False, False, False, False, False)
        assert tuple(membership) == (True, False)
        assert tuple(privileges) == (True, False, True, True, True, True)
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


def test_mungermode_migration_backfills_local_refresh_owner(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn, "20260821_0047")
    with closing(psycopg.connect(postgres_dsn)) as connection:
        connection.execute(
            """
            INSERT INTO ingest.source (id, name, family, kind, origin, enabled,
                                       operational_state, health_owner, freshness_seconds)
            VALUES ('mungermode-market-valuations', 'Munger Mode', 'market_data',
                    'market_valuation', 'https://mungermode.com/api/v1/market/metrics',
                    FALSE, 'active', 'external:mungermode', 86400)
            """
        )
        connection.commit()

    upgrade_database(postgres_dsn)

    with closing(psycopg.connect(postgres_dsn)) as connection:
        row = connection.execute(
            """
            SELECT operational_state, enabled, health_owner, freshness_seconds
            FROM ingest.source
            WHERE id = 'mungermode-market-valuations'
            """
        ).fetchone()

    assert tuple(row) == ("active", True, "update_market_valuations", 86400)


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


def test_recovery_cohort_reset_refuses_open_orders_then_quarantines_legacy_audit(
    postgres_dsn: str,
) -> None:
    """Exercise the reset against real pre-v2 rows, including rollback.

    The migration is intentionally allowed to preserve historical rows only
    after it proves that no recovery paper order could be converted underneath
    an active staged position.
    """

    upgrade_database(postgres_dsn, "20260803_0024")
    observed = datetime(2026, 8, 3, 19, tzinfo=UTC)
    with closing(psycopg.connect(postgres_dsn)) as connection:
        instrument_id = connection.execute(
            "INSERT INTO catalog.instrument (symbol, asset_class) VALUES ('AAOI', 'equity') RETURNING id"
        ).fetchone()[0]
        contract_id = connection.execute(
            """
            INSERT INTO catalog.option_contract
                (underlying_instrument_id, expiration, strike, option_type, provider_symbols)
            VALUES (%s, '2026-08-21', 20, 'call', '{"robinhood":"AAOI-test"}')
            RETURNING id
            """,
            [instrument_id],
        ).fetchone()[0]
        event_id = connection.execute(
            """
            INSERT INTO analysis.option_event
                (instrument_id, detected_at, started_at, reference_price, event_low, severity_score)
            VALUES (%s, %s, %s, 25, 20, 30) RETURNING id
            """,
            [instrument_id, observed, observed],
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO analysis.option_event_signal
                (event_id, contract_id, strategy_key, signal_at, available_at)
            VALUES (%s, %s, 'shock_reversal_call_v1', %s, %s)
            """,
            [event_id, contract_id, observed, observed],
        )
        connection.execute(
            """
            INSERT INTO analysis.option_opportunity_observation
                (event_id, capture_generation_key, contract_id, strategy_key, observed_at,
                 available_at, expiration, quote, liquid)
            VALUES (%s, 'legacy-generation', %s, 'shock_reversal_call_v1', %s, %s,
                    '2026-08-21', '{}', true)
            """,
            [event_id, contract_id, observed, observed],
        )
        connection.execute(
            """
            INSERT INTO analysis.option_event_agent_batch
                (event_id, trigger, fingerprint_key, fingerprint, model, reasoning_effort)
            VALUES (%s, 'event_established', 'legacy-batch', '{}', 'test', 'low')
            """,
            [event_id],
        )
        connection.execute(
            """
            INSERT INTO app.option_history_policy
                (instrument_id, profile, requested_state, effective_state, collection_tier,
                 cadence_minutes, publication_cap, provider, normalized_retention_days,
                 derived_retention_days, provider_payload_retention_days, policy_revision,
                 event_id, expires_at)
            VALUES (%s, 'event_strip', 'on', 'active', 'event', 15, 'WATCH', 'robinhood',
                    365, 730, 30, 'test', %s, %s)
            """,
            [instrument_id, event_id, observed + timedelta(days=20)],
        )
        connection.execute(
            """
            INSERT INTO app.paper_order
                (instrument_id, side, quantity, status, event_id)
            VALUES (%s, 'buy', 1, 'staged', %s)
            """,
            [instrument_id, event_id],
        )
        connection.commit()

    with pytest.raises(Exception, match="staged or open recovery paper order exists"):
        upgrade_database(postgres_dsn)

    with closing(psycopg.connect(postgres_dsn)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20260803_0024"
        connection.execute("DELETE FROM app.paper_order WHERE event_id = %s", [event_id])
        connection.commit()

    upgrade_database(postgres_dsn)
    with closing(psycopg.connect(postgres_dsn)) as connection:
        event = connection.execute(
            """
            SELECT event.status, event.close_reason, event.data_quality_status,
                   cohort.objective_version, cohort.status AS cohort_status
            FROM analysis.option_event event
            JOIN analysis.option_recovery_cohort cohort ON cohort.id = event.cohort_id
            WHERE event.id = %s
            """,
            [event_id],
        ).fetchone()
        signal = connection.execute(
            "SELECT status, cohort_id IS NOT NULL AS has_cohort FROM analysis.option_event_signal WHERE event_id = %s",
            [event_id],
        ).fetchone()
        observation = connection.execute(
            """
            SELECT data_status, outcome_classification, cohort_id IS NOT NULL AS has_cohort
            FROM analysis.option_opportunity_observation WHERE event_id = %s
            """,
            [event_id],
        ).fetchone()
        policy = connection.execute(
            "SELECT requested_state, effective_state FROM app.option_history_policy WHERE event_id = %s",
            [event_id],
        ).fetchone()
        current = connection.execute(
            """
            SELECT objective_version, status, required_qualified_dates
            FROM analysis.option_recovery_cohort
            WHERE objective_version = 'short_horizon_convex_v2'
            """
        ).fetchone()
    assert event == ("invalidated", "invalid_reference_bar", "invalid_reference_bar", "short_horizon_convex_v1", "retired")
    assert signal == ("invalidated", True)
    assert observation == ("invalid_event_reference", "unmeasurable", True)
    assert policy == ("off", "disabled")
    assert current == ("short_horizon_convex_v2", "collecting", 5)

    downgrade_database(postgres_dsn, "20260803_0024")
    upgrade_database(postgres_dsn)
    with closing(psycopg.connect(postgres_dsn)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == HEAD_REVISION


def test_strategy_evaluation_availability_migration_does_not_invent_history(
    postgres_dsn: str,
) -> None:
    upgrade_database(postgres_dsn, "20260829_0056")
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
        historical_cutoff = connection.execute("SELECT clock_timestamp()").fetchone()[0]

    upgrade_database(postgres_dsn)
    with closing(psycopg.connect(postgres_dsn)) as connection:
        available_at = connection.execute(
            "SELECT available_at FROM analysis.strategy_evaluation WHERE id = %s",
            [evaluation_id],
        ).fetchone()[0]

    assert available_at > historical_cutoff


def test_strategy_authority_is_append_only_after_immutability_migration(
    postgres_dsn: str,
) -> None:
    upgrade_database(postgres_dsn, "20260830_0057")
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
    downgrade_database(migrated_postgres_dsn, "20260904_0077")
    try:
        panel_data = load_panel_data(typed_config(migrated_postgres_dsn), table_names=("source_health",))
        assert panel_data.status.ready is False
        assert panel_data.metadata["schema_revision"] == "20260904_0077"
        assert panel_data.metadata["expected_schema_revision"] == HEAD_REVISION
    finally:
        upgrade_database(migrated_postgres_dsn)


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
