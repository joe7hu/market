from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import os
from urllib.parse import quote

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.sql import Identifier, Literal, SQL
from psycopg.types.json import Jsonb

from app.data_access.loaders import load_daily_research_panel_data, load_panel_data
from conftest import typed_config
from investment_panel.analysis.stock_alpha import FEATURE_VERSION, TARGET_HORIZON_SESSIONS, TARGET_VERSION, independent_observations, research_score
from investment_panel.core.decision import Horizon, MARKET_TZ, is_us_market_day, market_session_bounds
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.confirmed_daily_prices import confirmed_forward_bars, forward_trading_dates
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.migrations import downgrade_database, upgrade_database
from investment_panel.database.runtime import DatabaseRuntime, activate_application_role
from investment_panel.database.ticker_decisions import TickerDecisionRepository
from investment_panel.jobs.stock_alpha_walk_forward import load_observations, load_universe_members, run


def _window_end(as_of: datetime) -> datetime:
    day = as_of.astimezone(MARKET_TZ).date()
    for _ in range(TARGET_HORIZON_SESSIONS):
        day += timedelta(days=1)
        while not is_us_market_day(day):
            day += timedelta(days=1)
    return market_session_bounds(day)[1].astimezone(UTC)


def _seed_stock_bars(
    runtime: DatabaseRuntime, ticker: str, source: str, bars: list[tuple[date, float]], *, confirmed_at: datetime,
    fact_available_at: datetime | None = None,
) -> None:
    ingestion = IngestionRepository(runtime)
    ingestion.register_source(
        source, name=source, family="test", kind="daily_bars",
        operational_state="active", health_owner="test", freshness_seconds=86400,
    )
    run_id = ingestion.start_run(source, "price_bars", started_at=confirmed_at - timedelta(minutes=1))
    ingestion.store_price_bars(
        run_id, source, [{"symbol": ticker, "date": day.isoformat(), "close": close} for day, close in bars],
        asset_classes={ticker: "equity"},
    )
    ingestion.finish_run(run_id, "succeeded")
    with runtime.transaction() as connection:
        connection.execute("UPDATE ingest.run SET finished_at = %s WHERE id = %s", [confirmed_at, run_id])
        facts = connection.execute(
            "SELECT id, trading_date FROM raw.price_bar WHERE ingest_run_id = %s", [run_id],
        ).fetchall()
        for fact in facts:
            available = fact_available_at or (
                market_session_bounds(fact["trading_date"])[1].astimezone(UTC) + timedelta(minutes=1)
                if is_us_market_day(fact["trading_date"]) else confirmed_at
            )
            connection.execute("UPDATE raw.price_bar SET available_at = %s WHERE id = %s", [available, fact["id"]])
            for table in ("price_bar_confirmation", "price_bar_fact_availability"):
                connection.execute(
                    SQL("UPDATE raw.{} SET fact_available_at = %s WHERE fact_id = %s AND ingest_run_id = %s").format(Identifier(table)),
                    [available, fact["id"], run_id],
                )


def _observations(count: int, cutoff: datetime) -> list[dict[str, object]]:
    start = cutoff - timedelta(days=40 * (count + 1))
    rows = []
    for index in range(count):
        as_of = start + timedelta(days=index * 40)
        while not is_us_market_day(as_of.astimezone(MARKET_TZ).date()):
            as_of += timedelta(days=1)
        measured_through = _window_end(as_of)
        rows.append({
            "ticker": f"S{index:02d}",
            "opportunity_episode_id": f"stock-episode-{index}",
            "horizon": "TACTICAL",
            "horizon_sessions": TARGET_HORIZON_SESSIONS,
            "target_version": TARGET_VERSION,
            "cohort_id": "large-liquid",
            "as_of": as_of,
            "outcome_measured_through": measured_through,
            "outcome_available_at": measured_through + timedelta(hours=1),
            "feature_available_at": as_of - timedelta(minutes=30),
            "outcome": 1.0,
            "realized_return": 0.05,
            "modeled_cost": 0.001,
            "features": {
                "feature_version": FEATURE_VERSION,
                "momentum_5d": 0.02,
                "momentum_20d": 0.04,
                "relative_strength_20d": 0.03,
                "relative_strength_60d": 0.06,
                "kaufman_er_20d": 0.5,
            },
        })
    return rows


def _controls() -> dict[str, list[float]]:
    return {"randomized_label_returns": [0.0, 0.0], "white_noise_market_returns": [0.0, 0.0]}


def _paper_run(runtime, observations, **parameters):
    observations = list(observations)
    assert parameters.pop("promote") is True
    assert parameters.pop("authorization_mode") == "PAPER"
    run(runtime, observations, **parameters)
    return run(
        runtime, observations, **parameters, promote=True,
        authorization_mode="PAPER", promotion_cutoff=datetime.now(UTC),
    )


@pytest.fixture(autouse=True)
def _configured_evaluator_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKET_RESEARCH_EVALUATOR_SIGNING_KEY", "phase2-test-signing-key")


@pytest.fixture
def migrated_postgres_dsn(postgres_dsn: str, _configured_evaluator_signing_key) -> str:
    # These role/signing-key tests change migration inputs per test. Keep them
    # on the raw process, separate from the fixed session template.
    upgrade_database(postgres_dsn)
    return postgres_dsn


def _seed_universe_tape(runtime: DatabaseRuntime, cutoff: datetime, symbols: list[str], *, as_of: datetime | None = None) -> None:
    tape_as_of = as_of or cutoff
    with runtime.transaction() as connection:
        connection.execute(
            """INSERT INTO analysis.ticker_benchmark_snapshot
               (benchmark_key, as_of, available_at, membership_hash, member_count, source_id, exact_membership)
               VALUES ('market-equity-etf', %s, %s, %s, %s, 'phase1-test', %s)""",
            [tape_as_of, tape_as_of - timedelta(seconds=1), "phase1-membership", len(symbols), Jsonb({symbol: True for symbol in symbols})],
        )


def _application_dsn(postgres_dsn: str, login: str, password: str) -> str:
    connection_info = conninfo_to_dict(postgres_dsn)
    connection_info.update(user=login, password=password)
    return make_conninfo(**connection_info)


def _application_url(postgres_dsn: str, login: str, password: str) -> str:
    connection_info = conninfo_to_dict(postgres_dsn)
    host = quote(str(connection_info["host"]), safe="[]:")
    database = quote(str(connection_info["dbname"]), safe="")
    return f"postgresql://{quote(login, safe='')}:{quote(password, safe='')}@{host}:{connection_info['port']}/{database}"


def _configured_application_dsn(postgres_dsn: str) -> str:
    return _application_dsn(
        postgres_dsn,
        os.environ["MARKET_APP_LOGIN_ROLE"],
        os.environ["MARKET_APP_DATABASE_PASSWORD"],
    )


def _production_runtime(postgres_dsn: str) -> DatabaseRuntime:
    return DatabaseRuntime(_configured_application_dsn(postgres_dsn))


def test_walk_forward_registry_is_append_only_idempotent_and_paper_promoted(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _production_runtime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC)
        observations = _observations(16, cutoff)
        _seed_universe_tape(runtime, cutoff, [f"S{index:02d}" for index in range(16)], as_of=cutoff - timedelta(microseconds=2))
        first = _paper_run(
            runtime, observations, cutoff=cutoff, promote=True, authorization_mode="PAPER",
            min_train=4, fold_size=2, min_cohort=4, universe_members=[f"S{index:02d}" for index in range(16)], control_results=_controls(),
        )
        second = _paper_run(
            runtime, reversed(observations), cutoff=cutoff, promote=True, authorization_mode="PAPER",
            min_train=4, fold_size=2, min_cohort=4, universe_members=[f"S{index:02d}" for index in range(16)], control_results=_controls(),
        )

        assert first["complete"] is True
        assert first["promotion_stage"] == "paper"
        assert second["strategy_revision_id"] == first["strategy_revision_id"]
        assert second["strategy_evaluation_id"] == first["strategy_evaluation_id"]
        assert second["promotion_evaluation_id"] == first["promotion_evaluation_id"]
        with runtime.read() as connection:
            counts = connection.execute(
                """
                SELECT count(*) AS revisions,
                       (SELECT count(*) FROM analysis.strategy_evaluation
                        WHERE strategy_revision_id = %s) AS evaluations
                FROM analysis.strategy_revision
                WHERE strategy_key = 'ticker-stock-alpha'
                """,
                [first["strategy_revision_id"]],
            ).fetchone()
        assert counts == {"revisions": 1, "evaluations": 2}
        with runtime.read() as connection:
            research = connection.execute(
                """
                SELECT trial.status, result.outcome->'gates' AS gates,
                       result.metrics->'multiple_testing'->>'dsr' AS dsr,
                       (SELECT count(*) FROM analysis.validation_gate_result gate
                        JOIN analysis.validation_dossier dossier ON dossier.id = gate.dossier_id
                        WHERE dossier.strategy_revision_id = trial_revision.id) AS gate_count
                FROM analysis.research_trial trial
                JOIN analysis.validation_dossier dossier ON dossier.research_trial_id = trial.id
                JOIN analysis.strategy_revision trial_revision ON trial_revision.id = dossier.strategy_revision_id
                JOIN analysis.trial_result result ON result.research_trial_id = trial.id
                WHERE trial_revision.id = %s AND result.result_kind = 'validation'
                """,
                [first["strategy_revision_id"]],
            ).fetchone()
        assert research["status"] == "succeeded"
        assert all(value["passed"] for value in research["gates"].values())
        assert research["dsr"] is not None
        assert research["gate_count"] == 5
        with psycopg.connect(migrated_postgres_dsn) as connection:
            evidence = connection.execute(
                """SELECT count(*) AS source_count,
                          count(*) FILTER (WHERE manifest.evaluator_output_id IS NOT NULL
                                           AND manifest.evidence_hash = source.output_hash) AS bound_count
                   FROM analysis.research_evidence_manifest manifest
                   JOIN analysis.research_evaluator_output source ON source.id = manifest.evaluator_output_id
                   JOIN analysis.trial_result result ON result.id = manifest.trial_result_id
                   JOIN analysis.validation_dossier dossier ON dossier.research_trial_id = result.research_trial_id
                   WHERE dossier.strategy_revision_id = %s""",
                [first["strategy_revision_id"]],
            ).fetchone()
        assert evidence == (6, 6)
        with runtime.read() as connection:
            lineage = connection.execute(
                """
                SELECT hypothesis_id, experiment_family_id, research_trial_id,
                       validation_dossier_id, artifact_id, artifact_hash, input_hash,
                       metrics->'sample_windows' AS sample_windows
                FROM analysis.strategy_evaluation
                WHERE id = %s::uuid
                """,
                [first["strategy_evaluation_id"]],
            ).fetchone()
        assert lineage["hypothesis_id"] is not None
        assert lineage["experiment_family_id"] is not None
        assert lineage["research_trial_id"] is not None
        assert lineage["validation_dossier_id"] is not None
        assert lineage["artifact_id"].startswith("ticker-stock-alpha:")
        assert len(lineage["artifact_hash"]) == 64
        assert len(lineage["input_hash"]) == 64
        assert lineage["sample_windows"] == first["artifact"]["sample_windows"]
        assert len(lineage["sample_windows"]) == 16

        cutoff = datetime.now(UTC)
        artifact = AnalysisRepository(runtime).qualified_stock_alpha_artifact(
            cutoff=cutoff, horizon="TACTICAL",
        )
        assert artifact["availability_status"] == "available"
        assert artifact["promotion_stage"] == "paper"
        assert artifact["cohort_path"]
        assert artifact["effective_sample_size"] >= 4
        assert artifact["calibration_metrics"]["brier_score"] is not None
        assert artifact["lower_confidence_net_utility_after_costs"] > 0
        with runtime.read() as connection:
            forecast = connection.execute(
                """SELECT id, forecast_distribution, generated_at, available_at
                   FROM analysis.strategy_forecast
                   WHERE strategy_revision_id = %s AND horizon = 'TACTICAL'
                   ORDER BY available_at DESC, id DESC LIMIT 1""",
                [first["strategy_revision_id"]],
            ).fetchone()
        assert forecast["id"] == artifact["strategy_forecast_id"]
        assert forecast["forecast_distribution"] == artifact["forecast"]["forecast_distribution"]
        assert forecast["generated_at"] <= cutoff
        assert forecast["available_at"] <= cutoff
        import investment_panel.database.analysis as reader

        for field in ("STOCK_ALPHA_TARGET_VERSION", "STOCK_ALPHA_MODEL_VERSION"):
            with monkeypatch.context() as context:
                context.setattr(reader, field, "next-corrected-version")
                incompatible = AnalysisRepository(runtime).qualified_stock_alpha_artifact(
                    cutoff=cutoff, horizon="TACTICAL",
                )
                assert incompatible["availability_status"] == "policy_blocked"
                assert incompatible["blockers"] == ["alpha_target_version_incompatible"]
    finally:
        runtime.close()


def test_production_run_uses_configured_application_login_for_evaluator_writer(
    migrated_postgres_dsn: str,
) -> None:
    owner_runtime = DatabaseRuntime(migrated_postgres_dsn)
    owner_runtime.open()
    app_runtime = DatabaseRuntime(_configured_application_dsn(migrated_postgres_dsn))
    app_runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(seconds=5)
        symbols = [f"S{index:02d}" for index in range(16)]
        observations = _observations(16, cutoff)
        _seed_universe_tape(owner_runtime, cutoff, symbols, as_of=cutoff - timedelta(microseconds=2))

        with app_runtime.read() as connection:
            identity = connection.execute(
                "SELECT current_user, session_user, rolinherit FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
        assert identity["current_user"] == "market_app"
        assert identity["session_user"] == os.environ["MARKET_APP_LOGIN_ROLE"]
        assert identity["rolinherit"] is False

        result = run(
            app_runtime, observations, cutoff=cutoff, promote=False,
            authorization_mode="PAPER", min_train=4, fold_size=2, min_cohort=4,
            universe_members=symbols, control_results=_controls(),
        )
        assert result["complete"] is True
        with owner_runtime.read() as connection:
            source = connection.execute(
                """SELECT count(*) AS count
                   FROM analysis.research_evaluator_output output
                   JOIN analysis.research_trial trial ON trial.id = output.research_trial_id
                   WHERE trial.id = (
                       SELECT research_trial_id FROM analysis.validation_dossier
                       WHERE strategy_revision_id = %s
                   )""",
                [result["strategy_revision_id"]],
            ).fetchone()
        assert source["count"] == 6
    finally:
        app_runtime.close()
        owner_runtime.close()


def test_configured_login_read_context_activates_market_app_for_pit_and_research_loaders(
    migrated_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_login = "phase1_read_runtime_login"
    safe_password = "phase1-read-runtime-password"
    owner_runtime = DatabaseRuntime(migrated_postgres_dsn)
    owner_runtime.open()
    with psycopg.connect(migrated_postgres_dsn) as connection:
        connection.execute(
            SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOINHERIT").format(
                Identifier(safe_login), Literal(safe_password)
            ),
        )
        connection.execute(SQL("GRANT market_app TO {}").format(Identifier(safe_login)))
    monkeypatch.setenv("MARKET_APP_LOGIN_ROLE", safe_login)
    monkeypatch.setenv("MARKET_APP_DATABASE_PASSWORD", safe_password)
    cutoff = datetime.now(UTC) + timedelta(seconds=5)
    symbols = [f"R{index:02d}" for index in range(4)]
    try:
        _seed_universe_tape(owner_runtime, cutoff, symbols, as_of=cutoff - timedelta(microseconds=2))
        app_dsn = _application_dsn(migrated_postgres_dsn, safe_login, safe_password)
        app_runtime = DatabaseRuntime(app_dsn)
        app_runtime.open()
        try:
            with app_runtime.read() as connection:
                identity = connection.execute(
                    "SELECT current_user, session_user, rolinherit "
                    "FROM pg_roles WHERE rolname = current_user"
                ).fetchone()
            assert identity == {
                "current_user": "market_app",
                "session_user": safe_login,
                "rolinherit": False,
            }
            with app_runtime.snapshot() as connection:
                assert connection.execute(
                    "SELECT current_user"
                ).fetchone()["current_user"] == "market_app"
            assert load_universe_members(app_runtime, cutoff=cutoff) == symbols
            assert load_observations(app_runtime, cutoff=cutoff) == []
            config = typed_config(_application_url(migrated_postgres_dsn, safe_login, safe_password))
            seed = load_panel_data(
                config,
                table_names=("portfolio", "manual_watchlist", "option_radar_opportunity"),
            )
            assert seed.status.ready is True, seed.metadata
            panel = load_daily_research_panel_data(config)
            assert panel.status.ready is True, panel.metadata
            assert "research_trials" in panel.tables
            assert "research_validation_dossiers" in panel.tables
            with app_runtime.transaction() as connection:
                connection.execute("SELECT set_config('app.research_evaluator_signing_key', 'attacker-key', true)")
                connection.execute("SAVEPOINT protected_read_writer")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute("SELECT secret FROM analysis.research_evaluator_signing_secret")
                connection.execute("ROLLBACK TO SAVEPOINT protected_read_writer")
                connection.execute("SAVEPOINT protected_direct_insert")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(
                        "INSERT INTO analysis.research_evaluator_output (research_trial_id) "
                        "VALUES (gen_random_uuid())"
                    )
                connection.execute("ROLLBACK TO SAVEPOINT protected_direct_insert")
        finally:
            app_runtime.close()
    finally:
        with psycopg.connect(migrated_postgres_dsn) as connection:
            connection.execute(SQL("REVOKE market_app FROM {}").format(Identifier(safe_login)))
            connection.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(safe_login)))
        owner_runtime.close()


def test_distinct_noinherit_login_is_the_only_runtime_activation_boundary(
    migrated_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_login = "phase1_safe_runtime_login"
    safe_password = "phase1-safe-runtime-password"
    rejected_roles = {
        "phase1_superuser_login": "SUPERUSER NOINHERIT",
        "phase1_bypassrls_login": "NOSUPERUSER BYPASSRLS NOINHERIT",
        "phase1_inherit_login": "NOSUPERUSER NOBYPASSRLS INHERIT",
        "phase1_createrole_login": "NOSUPERUSER NOBYPASSRLS NOINHERIT CREATEROLE",
        "phase1_createdb_login": "NOSUPERUSER NOBYPASSRLS NOINHERIT CREATEDB",
        "phase1_replication_login": "NOSUPERUSER NOBYPASSRLS NOINHERIT REPLICATION",
    }
    direct_signer_login = "phase1_direct_signer_login"
    recursive_parent = "phase1_recursive_parent"
    recursive_migrator_login = "phase1_recursive_migrator_login"
    owner_runtime = DatabaseRuntime(migrated_postgres_dsn)
    owner_runtime.open()
    try:
        with psycopg.connect(migrated_postgres_dsn) as connection:
            connection.execute(
                SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOINHERIT").format(
                    Identifier(safe_login), Literal(safe_password)
                ),
            )
            connection.execute(SQL("GRANT market_app TO {}").format(Identifier(safe_login)))
            for role, attributes in rejected_roles.items():
                connection.execute(
                    SQL("CREATE ROLE {} LOGIN PASSWORD {} " + attributes).format(
                        Identifier(role), Literal(f"{role}-password")
                    ),
                )
            connection.execute(
                SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOINHERIT").format(
                    Identifier(direct_signer_login), Literal(f"{direct_signer_login}-password")
                ),
            )
            connection.execute(
                SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT").format(
                    Identifier(recursive_parent)
                ),
            )
            connection.execute(
                SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOINHERIT").format(
                    Identifier(recursive_migrator_login), Literal(f"{recursive_migrator_login}-password")
                ),
            )
            connection.execute(SQL("GRANT market_research_signer TO {}").format(Identifier(direct_signer_login)))
            connection.execute(SQL("GRANT market_migrator TO {}").format(Identifier(recursive_parent)))
            connection.execute(SQL("GRANT {} TO {}").format(Identifier(recursive_parent), Identifier(recursive_migrator_login)))

        monkeypatch.setenv("MARKET_APP_LOGIN_ROLE", safe_login)
        monkeypatch.setenv("MARKET_APP_DATABASE_PASSWORD", safe_password)
        cutoff = datetime.now(UTC) + timedelta(seconds=5)
        symbols = [f"S{index:02d}" for index in range(16)]
        _seed_universe_tape(owner_runtime, cutoff, symbols, as_of=cutoff - timedelta(microseconds=2))
        app_runtime = DatabaseRuntime(_application_dsn(migrated_postgres_dsn, safe_login, safe_password))
        app_runtime.open()
        try:
            with app_runtime.read() as connection:
                identity = connection.execute(
                    """SELECT current_user, session_user, role.rolsuper, role.rolbypassrls, role.rolinherit,
                              role.rolcreaterole, role.rolcreatedb, role.rolreplication
                       FROM pg_roles role WHERE role.rolname = current_user"""
                ).fetchone()
            with owner_runtime.read() as connection:
                privileges = connection.execute(
                    """SELECT has_table_privilege(%s, 'analysis.research_evaluator_output', 'INSERT') AS output_insert,
                              has_table_privilege(%s, 'analysis.research_evaluator_signing_secret', 'SELECT') AS key_select""",
                    [safe_login, safe_login],
                ).fetchone()
            identity.update(privileges)
            assert identity == {
                "current_user": "market_app",
                "session_user": safe_login,
                "rolsuper": False,
                "rolbypassrls": False,
                "rolinherit": False,
                "rolcreaterole": False,
                "rolcreatedb": False,
                "rolreplication": False,
                "output_insert": False,
                "key_select": False,
            }
            result = run(
                app_runtime, _observations(16, cutoff), cutoff=cutoff,
                promote=False, authorization_mode="PAPER", min_train=4,
                fold_size=2, min_cohort=4, universe_members=symbols,
                control_results=_controls(),
            )
            assert result["complete"] is True
            with app_runtime.transaction() as connection:
                activate_application_role(connection)
                assert connection.execute("SELECT current_user").fetchone()["current_user"] == "market_app"
                connection.execute("SELECT set_config('app.research_evaluator_signing_key', 'attacker-key', true)")
                for statement in (
                    "SELECT secret FROM analysis.research_evaluator_signing_secret",
                    "INSERT INTO analysis.research_evaluator_output (research_trial_id) VALUES (gen_random_uuid())",
                    "SET ROLE market_research_signer",
                    "SET ROLE market_migrator",
                ):
                    connection.execute("SAVEPOINT protected_role_boundary")
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        connection.execute(statement)
                    connection.execute("ROLLBACK TO SAVEPOINT protected_role_boundary")
        finally:
            app_runtime.close()

        monkeypatch.setenv("MARKET_APP_LOGIN_ROLE", "postgres")
        with owner_runtime.transaction() as connection:
            with pytest.raises(RuntimeError, match="unsafe attributes|cannot activate"):
                activate_application_role(connection)
        for role, attributes in rejected_roles.items():
            monkeypatch.setenv("MARKET_APP_LOGIN_ROLE", role)
            with psycopg.connect(
                _application_dsn(migrated_postgres_dsn, role, f"{role}-password"),
                row_factory=dict_row,
            ) as connection:
                with connection.transaction(), pytest.raises(
                    RuntimeError, match="unsafe attributes|cannot activate"
                ):
                    activate_application_role(connection)
        for role in (direct_signer_login, recursive_migrator_login):
            monkeypatch.setenv("MARKET_APP_LOGIN_ROLE", role)
            with psycopg.connect(
                _application_dsn(migrated_postgres_dsn, role, f"{role}-password"),
                row_factory=dict_row,
            ) as connection:
                with connection.transaction(), pytest.raises(
                    RuntimeError, match="unsafe role membership path|protected evaluator role|cannot activate"
                ):
                    activate_application_role(connection)
    finally:
        with psycopg.connect(migrated_postgres_dsn) as connection:
            connection.execute(SQL("REVOKE market_research_signer FROM {}").format(Identifier(direct_signer_login)))
            connection.execute(SQL("REVOKE market_migrator FROM {}").format(Identifier(recursive_parent)))
            connection.execute(SQL("REVOKE {} FROM {}").format(Identifier(recursive_parent), Identifier(recursive_migrator_login)))
            connection.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(safe_login)))
            for role in rejected_roles:
                connection.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(role)))
            for role in (direct_signer_login, recursive_migrator_login, recursive_parent):
                connection.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(role)))
        owner_runtime.close()


def test_incomplete_challenger_cannot_promote(migrated_postgres_dsn: str) -> None:
    runtime = _production_runtime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(seconds=5)
        _seed_universe_tape(runtime, cutoff, [f"S{index:02d}" for index in range(3)])
        result = run(
            runtime, _observations(3, cutoff), cutoff=cutoff,
            promote=True, authorization_mode="ADVISORY", min_train=4, min_cohort=4,
            universe_members=[f"S{index:02d}" for index in range(3)], control_results=_controls(),
        )
        assert result["complete"] is False
        assert result["promotion_evaluation_id"] is None
        assert result["promotion_stage"] == "challenger"
        with runtime.read() as connection:
            status = connection.execute(
                "SELECT status FROM analysis.strategy_revision WHERE id = %s",
                [result["strategy_revision_id"]],
            ).fetchone()["status"]
        assert status == "candidate"
    finally:
        runtime.close()


def test_production_path_missing_controls_is_visible_and_non_promotable(migrated_postgres_dsn: str) -> None:
    runtime = _production_runtime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(seconds=5)
        symbols = [f"S{index:02d}" for index in range(16)]
        _seed_universe_tape(runtime, cutoff, symbols, as_of=cutoff - timedelta(microseconds=2))
        result = run(
            runtime, _observations(16, cutoff), cutoff=cutoff,
            promote=True, authorization_mode="PAPER", min_train=4, fold_size=2,
            min_cohort=4, universe_members=symbols,
        )
        assert result["complete"] is False
        assert result["promotion_evaluation_id"] is None
        with runtime.read() as connection:
            control = connection.execute(
                """SELECT outcome FROM analysis.trial_result
                   WHERE research_trial_id = (
                       SELECT research_trial_id FROM analysis.validation_dossier
                       WHERE strategy_revision_id = %s
                   ) AND result_kind = 'negative_controls'""",
                [result["strategy_revision_id"]],
            ).fetchone()
        assert control["outcome"]["passed"] is False
    finally:
        runtime.close()


def test_exact_current_cutoff_retains_wall_clock_forecast_and_fails_closed(migrated_postgres_dsn: str) -> None:
    runtime = _production_runtime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC)
        symbols = [f"S{index:02d}" for index in range(16)]
        _seed_universe_tape(runtime, cutoff, symbols, as_of=cutoff - timedelta(microseconds=2))
        result = run(
            runtime, _observations(16, cutoff), cutoff=cutoff,
            promote=True, authorization_mode="PAPER", min_train=4, fold_size=2,
            min_cohort=4, universe_members=symbols, control_results=_controls(),
        )
        assert result["complete"] is True
        assert result["promotion_evaluation_id"] is None
        assert result["promotion_reason"] == "forecast_evidence_not_available_at_cutoff"
        with runtime.read() as connection:
            availability = connection.execute(
                "SELECT available_at FROM analysis.strategy_forecast WHERE strategy_revision_id = %s ORDER BY id LIMIT 1",
                [result["strategy_revision_id"]],
            ).fetchone()["available_at"]
        assert availability > cutoff
        with pytest.raises(psycopg.errors.RaiseException, match="cannot be future-dated"):
            with runtime.transaction() as connection:
                connection.execute(
                    """INSERT INTO analysis.strategy_evaluation (
                           strategy_revision_id, hypothesis_id, experiment_family_id,
                           research_trial_id, validation_dossier_id, artifact_id,
                           artifact_hash, input_hash, evaluation_type, evaluated_at,
                           period_start, period_end, verdict, metrics, evidence)
                       SELECT strategy_revision_id, hypothesis_id, experiment_family_id,
                              research_trial_id, validation_dossier_id, artifact_id,
                              artifact_hash, input_hash, 'paper_advisory_promotion', clock_timestamp(),
                              period_start, period_end, 'pass', jsonb_build_object(
                                  'artifact_hash', artifact_hash, 'input_hash', input_hash,
                                  'authorization_mode', 'PAPER', 'promotion_cutoff', %s::text), '{}'
                       FROM analysis.strategy_evaluation WHERE id = %s""",
                    [(datetime.now(UTC) + timedelta(days=1)).isoformat(), result["strategy_evaluation_id"]],
                )
                connection.execute(
                    "UPDATE analysis.strategy_revision SET status = 'active' WHERE id = %s",
                    [result["strategy_revision_id"]],
                )
        promotion_cutoff = datetime.now(UTC)
        promoted = run(
            runtime, _observations(16, cutoff), cutoff=cutoff,
            promote=True, authorization_mode="PAPER", promotion_cutoff=promotion_cutoff,
            min_train=4, fold_size=2, min_cohort=4,
            universe_members=symbols, control_results=_controls(),
        )
        assert promoted["promotion_stage"] == "paper"
        assert promoted["strategy_revision_id"] == result["strategy_revision_id"]
        assert promoted["strategy_evaluation_id"] == result["strategy_evaluation_id"]
        assert availability <= promotion_cutoff
    finally:
        runtime.close()


def test_historical_cutoff_keeps_actual_forecast_availability_and_blocks_promotion(
    migrated_postgres_dsn: str,
) -> None:
    runtime = _production_runtime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) - timedelta(days=1)
        symbols = [f"S{index:02d}" for index in range(16)]
        _seed_universe_tape(runtime, cutoff, symbols)
        result = run(
            runtime, _observations(16, cutoff), cutoff=cutoff,
            promote=True, authorization_mode="PAPER", min_train=4, fold_size=2,
            min_cohort=4, universe_members=symbols, control_results=_controls(),
        )
        assert result["complete"] is True
        assert result["promotion_evaluation_id"] is None
        assert result["promotion_reason"] == "forecast_evidence_not_available_at_cutoff"
        with runtime.read() as connection:
            persisted = connection.execute(
                """SELECT forecast.available_at, dossier.status
                   FROM analysis.strategy_forecast forecast
                   JOIN analysis.strategy_evaluation evaluation ON evaluation.id = forecast.strategy_evaluation_id
                   JOIN analysis.validation_dossier dossier ON dossier.id = evaluation.validation_dossier_id
                   WHERE forecast.strategy_revision_id = %s
                   ORDER BY forecast.id LIMIT 1""",
                [result["strategy_revision_id"]],
            ).fetchone()
        assert persisted["available_at"] > cutoff
        assert persisted["status"] == "sealed"
    finally:
        runtime.close()


@pytest.mark.parametrize("measurement_source", ["column", "metadata"])
def test_canonical_pit_trend_feature_loads_for_training_and_live_inference(
    migrated_postgres_dsn: str, measurement_source: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        decision_at = datetime(2026, 1, 2, 21, 10, tzinfo=UTC)
        measured_through = _window_end(decision_at)
        next_window_end = _window_end(measured_through)
        cutoff = next_window_end + timedelta(hours=2)
        repository = AnalysisRepository(runtime)
        run_id = repository.start_run(
            "daily-trend", input_cutoff=decision_at - timedelta(minutes=1),
            code_version="test", inputs={"symbol": "PIT"},
            feature_versions={"daily_trend": FEATURE_VERSION},
        )
        with runtime.transaction() as connection:
            instrument_id = connection.execute(
                """
                INSERT INTO catalog.instrument (symbol, name, asset_class)
                VALUES ('PIT', 'PIT', 'equity') RETURNING id
                """
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO analysis.symbol_feature (
                    run_id, instrument_id, as_of, feature_set, feature_version,
                    momentum_5d, momentum_20d, relative_strength_20d,
                    relative_strength_60d, kaufman_er_20d,
                    trend_state, trend_confidence, volatility_state,
                    data_quality_status, reason_codes
                ) VALUES (%s, %s, %s, 'daily_trend', %s, 0.02, 0.04, 0.03,
                          0.06, 0.5, 'trend_up', 0.8, 'normal', 'complete', '{}')
                """,
                [run_id, instrument_id, decision_at - timedelta(minutes=2), FEATURE_VERSION],
            )
            connection.execute(
                """
                INSERT INTO analysis.ticker_benchmark_snapshot (
                    benchmark_key, as_of, available_at, membership_hash,
                    member_count, source_id, exact_membership
                ) VALUES ('market-equity-etf', %s, %s, %s, 1, 'test', %s)
                """,
                [decision_at, decision_at, "b" * 64, Jsonb({"PIT": True})],
            )
            decision_ids = []
            for index, start in enumerate((
                decision_at, decision_at + timedelta(hours=2),
                measured_through, measured_through + timedelta(hours=1),
            )):
                decision_id = connection.execute(
                    """
                    INSERT INTO analysis.ticker_decision (
                        instrument_id, decision_revision, contract_version, as_of,
                        published_at, input_hash, code_version, experiment_id,
                        tactical, fundamental, capital_action, risk_policy
                    ) VALUES (%s, %s, 'test', %s, %s, %s, 'test', 'test',
                              '{}', '{}', '{}', '{}') RETURNING id
                    """,
                    [instrument_id, f"pit-decision-{index}", start, start, "a" * 64],
                ).fetchone()["id"]
                decision_ids.append(decision_id)
                end = _window_end(start)
                available_at = end + (timedelta(days=7) if index < 2 else timedelta(hours=1))
                connection.execute(
                    """
                    INSERT INTO analysis.ticker_outcome (
                        ticker_decision_id, horizon, horizon_sessions, state,
                        selected_return, stock_counterfactual_return, measured_through,
                        available_at, metadata
                    ) VALUES (%s, 'TACTICAL', 20, 'resolved', 0.0, 0.05, %s, %s, %s)
                    """,
                    [
                        decision_id, end if measurement_source == "column" else None, available_at,
                        Jsonb({
                            "cost_adjusted_selected_return": 0.0,
                            "cost_adjusted_stock_counterfactual_return": 0.049,
                            "observed_through": end.isoformat(),
                        }),
                    ],
                )
        repository.finish_run(run_id, "succeeded")
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE analysis.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [decision_at - timedelta(minutes=2), decision_at - timedelta(minutes=1), run_id],
            )
        for start, end, price in ((decision_at, measured_through, 105.0), (measured_through, next_window_end, 110.25)):
            day = start.astimezone(MARKET_TZ).date() + timedelta(days=1)
            bars = []
            while day <= end.astimezone(MARKET_TZ).date():
                if is_us_market_day(day):
                    bars.append((day, price))
                day += timedelta(days=1)
            _seed_stock_bars(runtime, "PIT", "polygon", bars, confirmed_at=end + timedelta(minutes=1))

        observations = load_observations(runtime, cutoff=cutoff)
        assert len(observations) == 2
        assert observations[0]["opportunity_episode_id"] == observations[1]["opportunity_episode_id"]
        assert [row["sample_window_start"] for row in observations] == [decision_at, measured_through]
        assert [row["sample_window_end"] for row in observations] == [measured_through, next_window_end]
        assert observations[1]["as_of"] < observations[0]["outcome_available_at"]
        assert observations[0]["features"]["feature_version"] == FEATURE_VERSION
        assert observations[0]["realized_return"] == 0.05
        assert observations[0]["modeled_cost"] == pytest.approx(0.001)
        assert observations[0]["outcome"] == 1.0
        assert observations[0]["target_version"] == TARGET_VERSION
        with runtime.transaction() as connection:
            connection.execute(
                """UPDATE analysis.ticker_outcome SET selected_return = -0.8,
                   metadata = metadata || '{"cost_adjusted_selected_return": -0.9}'::jsonb
                   WHERE ticker_decision_id = ANY(%s)""",
                [decision_ids],
            )
        assert load_observations(runtime, cutoff=cutoff) == observations
        assert research_score(observations[0]["features"]) is not None
        feature = repository.stock_alpha_feature(
            "PIT", cutoff=decision_at, feature_version=FEATURE_VERSION,
        )
        assert feature is not None
        assert research_score(feature) == research_score(observations[0]["features"])
    finally:
        runtime.close()


@pytest.mark.parametrize("missing_session", [False, True])
def test_stock_maturity_requires_twenty_canonical_sessions_known_at_the_outcome_clock(
    migrated_postgres_dsn: str, missing_session: bool,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ticker = "MATURITY"
        as_of = datetime(2026, 1, 2, 21, 10, tzinfo=UTC)
        dates = [date(2026, 1, day) for day in (
            5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 20, 21, 22, 23, 26, 27, 28, 29, 30,
        )] + [date(2026, 2, 2)]
        tenth_close = market_session_bounds(dates[9])[1].astimezone(UTC)
        twentieth_close = market_session_bounds(dates[-1])[1].astimezone(UTC)
        _seed_stock_bars(runtime, ticker, "polygon", [(as_of.date(), 100.0)], confirmed_at=as_of - timedelta(minutes=1))
        for source, premium in (("polygon", 0), ("test-secondary-bars", 100)):
            _seed_stock_bars(
                runtime, ticker, source, [(day, 101.0 + index + premium) for index, day in enumerate(dates[:10])],
                confirmed_at=tenth_close + timedelta(minutes=2),
            )
        # Saturday and the MLK holiday are not additional market sessions.
        _seed_stock_bars(runtime, ticker, "calendar-noise", [
            (date(2026, 1, 10), 999.0), (date(2026, 1, 19), 999.0),
        ], confirmed_at=twentieth_close + timedelta(minutes=1))
        with runtime.transaction() as connection:
            instrument_id = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = %s", [ticker]).fetchone()["id"]
            run_id = connection.execute(
                """INSERT INTO analysis.run
                   (run_type, input_cutoff, code_version, input_hash, started_at, finished_at, status)
                   VALUES ('daily-trend', %s, 'test', %s, %s, %s, 'succeeded') RETURNING id""",
                [as_of - timedelta(minutes=2), "c" * 64, as_of - timedelta(minutes=3), as_of - timedelta(minutes=1)],
            ).fetchone()["id"]
            connection.execute(
                """INSERT INTO analysis.symbol_feature
                   (run_id, instrument_id, as_of, feature_set, feature_version, momentum_5d, momentum_20d,
                    relative_strength_20d, relative_strength_60d, kaufman_er_20d,
                    trend_state, trend_confidence, volatility_state, data_quality_status, reason_codes)
                   VALUES (%s, %s, %s, 'daily_trend', %s, .02, .04, .03, .06, .5,
                           'trend_up', .8, 'normal', 'complete', '{}')""",
                [run_id, instrument_id, as_of - timedelta(minutes=2), FEATURE_VERSION],
            )
            decision_id = connection.execute(
                """INSERT INTO analysis.ticker_decision
                   (instrument_id, decision_revision, contract_version, as_of, published_at, input_hash,
                    code_version, experiment_id, tactical, fundamental, capital_action, risk_policy)
                   VALUES (%s, 'maturity', 'test', %s, %s, %s, 'test', 'test', '{}', '{}', '{}', '{}') RETURNING id""",
                [instrument_id, as_of, as_of, "d" * 64],
            ).fetchone()["id"]
        _seed_universe_tape(runtime, as_of, [ticker])
        decision = {
            "instrument_id": instrument_id, "as_of": as_of, "tactical": {}, "fundamental": {},
            "capital_action": {}, "selected_expression": {"kind": "STOCK"}, "expressions": {},
        }
        repository = TickerDecisionRepository(runtime)
        partial = repository._evaluate(decision, Horizon.TACTICAL, 20, tenth_close + timedelta(minutes=3))
        assert partial["state"] == "observing"
        assert partial["observed_through"] == tenth_close
        assert partial["stock_return"] == pytest.approx(.10)
        repository._store_outcome(str(decision_id), Horizon.TACTICAL, 20, {**partial, "state": "resolved"}, selected_expression="STOCK")
        assert load_observations(runtime, cutoff=twentieth_close + timedelta(minutes=3)) == []

        later_confirmation = twentieth_close + timedelta(days=2)
        later_bars = [(day, 111.0 + index) for index, day in enumerate(dates[10:]) if not missing_session or index != 4]
        if missing_session:
            later_bars.append((date(2026, 2, 3), 121.0))
        _seed_stock_bars(runtime, ticker, "polygon", later_bars, confirmed_at=later_confirmation)
        before_confirmation = twentieth_close + timedelta(minutes=3)
        assert repository._evaluate(decision, Horizon.TACTICAL, 20, before_confirmation)["state"] == "observing"
        repository._store_outcome(str(decision_id), Horizon.TACTICAL, 20, {
            **partial, "state": "resolved", "observed_through": twentieth_close, "available_at": before_confirmation,
        }, selected_expression="STOCK")
        assert load_observations(runtime, cutoff=later_confirmation + timedelta(minutes=1)) == []

        final = repository._evaluate(decision, Horizon.TACTICAL, 20, later_confirmation + timedelta(minutes=1))
        assert final["state"] == ("observing" if missing_session else "resolved")
        repository._store_outcome(str(decision_id), Horizon.TACTICAL, 20, final, selected_expression="STOCK")
        observations = load_observations(runtime, cutoff=later_confirmation + timedelta(minutes=1))
        if missing_session:
            assert observations == []
        else:
            assert final["stock_return"] == pytest.approx(.20)
            assert final["observed_through"] == twentieth_close
            assert final["available_at"] == later_confirmation
            assert len(observations) == 1
            assert observations[0]["target_version"] == TARGET_VERSION
            assert observations[0]["sample_window_end"] == twentieth_close
            assert observations[0]["outcome_available_at"] == later_confirmation

            # A later lifecycle event must not replace any completed fixed
            # tactical return. It remains terminal evidence for longer targets.
            delisted_at = twentieth_close + timedelta(days=3)
            delisting_available = delisted_at + timedelta(minutes=1)
            with runtime.transaction() as connection:
                connection.execute(
                    """UPDATE catalog.instrument
                       SET delisted_at = %s, delisting_price = 10.0,
                           delisting_available_at = %s, delisting_source = 'verified-terminal-value'
                       WHERE id = %s""",
                    [delisted_at, delisting_available, instrument_id],
                )
            refresh_cutoff = delisting_available + timedelta(minutes=1)
            assert repository.refresh_outcomes(now=refresh_cutoff, limit=1, symbols={ticker}) == {
                "evaluated": 1, "updated": 6, "resolved": 6,
            }
            assert load_observations(runtime, cutoff=refresh_cutoff) == observations
            with runtime.read() as connection:
                refreshed = connection.execute(
                    """SELECT horizon, horizon_sessions, stock_counterfactual_return,
                              measured_through, metadata
                       FROM analysis.ticker_outcome WHERE ticker_decision_id = %s""",
                    [decision_id],
                ).fetchall()
            for outcome in refreshed:
                sessions = outcome["horizon_sessions"]
                if outcome["horizon"] == "TACTICAL":
                    assert outcome["stock_counterfactual_return"] == pytest.approx(sessions / 100.0)
                    assert outcome["measured_through"] == market_session_bounds(dates[sessions - 1])[1]
                    assert outcome["metadata"]["delisting_status"] == "active"
                    assert outcome["metadata"]["expression_marks"]["STOCK"]["status"] == "estimated"
                else:
                    assert outcome["stock_counterfactual_return"] == pytest.approx(-.9)
                    assert outcome["measured_through"] == delisted_at
                    assert outcome["metadata"]["delisting_status"] == "delisted_terminal"
    finally:
        runtime.close()


def test_stock_session_needs_a_real_post_close_confirmation_even_when_price_is_unchanged(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        as_of = datetime(2026, 1, 2, 21, 10, tzinfo=UTC)
        dates = forward_trading_dates(as_of, count=20)
        close_at = market_session_bounds(dates[-1])[1].astimezone(UTC)
        _seed_stock_bars(runtime, "CLOSE", "polygon", [(day, 100.0) for day in dates[:-1]], confirmed_at=close_at - timedelta(days=1))
        _seed_stock_bars(
            runtime, "CLOSE", "polygon", [(dates[-1], 100.0)],
            confirmed_at=close_at - timedelta(minutes=29), fact_available_at=close_at - timedelta(minutes=30),
        )
        with runtime.read() as connection:
            instrument_id = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = 'CLOSE'").fetchone()["id"]
            assert len(confirmed_forward_bars(connection, instrument_id, as_of=as_of, cutoff=close_at - timedelta(minutes=1), sessions=20)) == 19
            assert len(confirmed_forward_bars(connection, instrument_id, as_of=as_of, cutoff=close_at + timedelta(minutes=1), sessions=20)) == 19
        _seed_stock_bars(runtime, "CLOSE", "polygon", [(dates[-1], 100.0)], confirmed_at=close_at + timedelta(minutes=5))
        with runtime.read() as connection:
            assert len(confirmed_forward_bars(connection, instrument_id, as_of=as_of, cutoff=close_at + timedelta(minutes=4), sessions=20)) == 19
            bars = confirmed_forward_bars(connection, instrument_id, as_of=as_of, cutoff=close_at + timedelta(minutes=6), sessions=20)
        assert len(bars) == 20
        assert bars[-1]["observed_at"] == close_at
        assert bars[-1]["available_at"] == close_at + timedelta(minutes=5)
    finally:
        runtime.close()


@pytest.mark.parametrize("defect", [None, "missing_source", "future_evidence", "different_terminal_price", "different_terminal_clock"])
def test_verified_terminal_stock_loss_is_retained_only_at_its_fixed_horizon(
    migrated_postgres_dsn: str, defect: str | None,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ticker = "TERMINAL"
        as_of = datetime(2026, 1, 2, 21, 10, tzinfo=UTC)
        delisted_at = datetime(2026, 1, 12, 21, tzinfo=UTC)
        available_at = delisted_at + timedelta(minutes=5)
        horizon_end = _window_end(as_of)
        _seed_stock_bars(runtime, ticker, "polygon", [(as_of.date(), 100.0)], confirmed_at=as_of - timedelta(minutes=1))
        with runtime.transaction() as connection:
            instrument_id = connection.execute(
                """UPDATE catalog.instrument
                   SET delisted_at = %s, delisting_price = 10.0, delisting_available_at = %s,
                       delisting_source = 'recorded-terminal-cash-value'
                   WHERE symbol = %s RETURNING id""",
                [delisted_at, available_at, ticker],
            ).fetchone()["id"]
            run_id = connection.execute(
                """INSERT INTO analysis.run
                   (run_type, input_cutoff, code_version, input_hash, started_at, finished_at, status)
                   VALUES ('daily-trend', %s, 'test', %s, %s, %s, 'succeeded') RETURNING id""",
                [as_of - timedelta(minutes=2), "e" * 64, as_of - timedelta(minutes=3), as_of - timedelta(minutes=1)],
            ).fetchone()["id"]
            connection.execute(
                """INSERT INTO analysis.symbol_feature
                   (run_id, instrument_id, as_of, feature_set, feature_version, momentum_5d, momentum_20d,
                    relative_strength_20d, relative_strength_60d, kaufman_er_20d,
                    trend_state, trend_confidence, volatility_state, data_quality_status, reason_codes)
                   VALUES (%s, %s, %s, 'daily_trend', %s, .02, .04, .03, .06, .5,
                           'trend_up', .8, 'normal', 'complete', '{}')""",
                [run_id, instrument_id, as_of - timedelta(minutes=2), FEATURE_VERSION],
            )
            decision_id = connection.execute(
                """INSERT INTO analysis.ticker_decision
                   (instrument_id, decision_revision, contract_version, as_of, published_at, input_hash,
                    code_version, experiment_id, tactical, fundamental, capital_action, risk_policy)
                   VALUES (%s, 'terminal-loss', 'test', %s, %s, %s, 'test', 'test', '{}', '{}', '{}', '{}') RETURNING id""",
                [instrument_id, as_of, as_of, "f" * 64],
            ).fetchone()["id"]
        _seed_universe_tape(runtime, as_of, [ticker])
        repository = TickerDecisionRepository(runtime)
        outcome = repository._evaluate({
            "instrument_id": instrument_id, "as_of": as_of, "tactical": {}, "fundamental": {},
            "capital_action": {}, "selected_expression": {"kind": "STOCK"}, "expressions": {},
        }, Horizon.TACTICAL, 20, available_at)
        assert outcome["state"] == "resolved"
        assert outcome["stock_return"] == pytest.approx(-.9)
        repository._store_outcome(str(decision_id), Horizon.TACTICAL, 20, outcome, selected_expression="STOCK")
        assert load_observations(runtime, cutoff=available_at) == []
        assert load_observations(runtime, cutoff=horizon_end - timedelta(microseconds=1)) == []
        if defect:
            column, value = {
                "missing_source": ("delisting_source", None),
                "future_evidence": ("delisting_available_at", horizon_end + timedelta(days=1)),
                "different_terminal_price": ("delisting_price", 11.0),
                "different_terminal_clock": ("delisted_at", delisted_at + timedelta(days=1)),
            }[defect]
            with runtime.transaction() as connection:
                connection.execute(
                    SQL("UPDATE catalog.instrument SET {} = %s WHERE id = %s").format(Identifier(column)),
                    [value, instrument_id],
                )
        observations = load_observations(runtime, cutoff=horizon_end)
        if defect:
            assert observations == []
            return
        assert len(observations) == 1
        row = observations[0]
        assert row["realized_return"] == pytest.approx(-.9)
        assert row["modeled_cost"] > 0
        assert row["outcome"] == 0
        assert row["sample_window_end"] == horizon_end
        assert row["outcome_available_at"] == horizon_end
        assert row["terminal_evidence"]["observed_at"] == delisted_at
        assert row["terminal_evidence"]["available_at"] == available_at
        assert row["terminal_evidence"]["source_id"] == "recorded-terminal-cash-value"
        with runtime.read() as connection:
            persisted = connection.execute(
                "SELECT measured_through, available_at FROM analysis.ticker_outcome WHERE ticker_decision_id = %s",
                [decision_id],
            ).fetchone()
        assert persisted["measured_through"] == delisted_at
        assert persisted["available_at"] == available_at
    finally:
        runtime.close()


def test_latest_oos_input_hash_mismatch_fails_closed(migrated_postgres_dsn: str) -> None:
    runtime = _production_runtime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(seconds=5)
        evaluation_cutoff = cutoff - timedelta(seconds=5)
        _seed_universe_tape(runtime, evaluation_cutoff, [f"S{index:02d}" for index in range(16)])
        result = _paper_run(
            runtime, _observations(16, evaluation_cutoff), cutoff=evaluation_cutoff,
            promote=True, authorization_mode="PAPER",
            min_train=4, fold_size=2, min_cohort=4,
            universe_members=[f"S{index:02d}" for index in range(16)], control_results=_controls(),
        )
        with runtime.transaction() as connection:
            connection.execute(
                """
                INSERT INTO analysis.strategy_evaluation (
                    strategy_revision_id, evaluation_type, evaluated_at,
                    period_start, period_end, verdict, metrics, evidence
                )
                SELECT strategy_revision_id, evaluation_type, %s,
                       period_start, period_end, verdict,
                       metrics || jsonb_build_object('input_hash', 'mismatch'), evidence
                FROM analysis.strategy_evaluation WHERE id = %s::uuid
                """,
                [cutoff, result["strategy_evaluation_id"]],
            )
        artifact = AnalysisRepository(runtime).qualified_stock_alpha_artifact(
            cutoff=cutoff, horizon="TACTICAL",
        )
        assert artifact["availability_status"] == "error"
        assert artifact["blockers"] == ["alpha_evaluation_lineage_mismatch"]
    finally:
        runtime.close()


def test_superseded_revision_replay_cannot_deactivate_current_champion(
    migrated_postgres_dsn: str,
) -> None:
    runtime = _production_runtime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC)
        observations_a = _observations(16, cutoff)
        observations_b = _observations(17, cutoff)
        _seed_universe_tape(runtime, cutoff, [f"S{index:02d}" for index in range(16)], as_of=cutoff - timedelta(microseconds=2))
        first = _paper_run(
            runtime, observations_a, cutoff=cutoff, promote=True, authorization_mode="PAPER",
            min_train=4, fold_size=2, min_cohort=4,
            universe_members=[f"S{index:02d}" for index in range(16)], control_results=_controls(),
        )
        _seed_universe_tape(runtime, cutoff, [f"S{index:02d}" for index in range(17)], as_of=cutoff - timedelta(microseconds=1))
        second = _paper_run(
            runtime, observations_b, cutoff=cutoff, promote=True, authorization_mode="PAPER",
            min_train=4, fold_size=2, min_cohort=4,
            universe_members=[f"S{index:02d}" for index in range(17)], control_results=_controls(),
        )
        with pytest.raises(ValueError, match="submitted universe|superseded stock-alpha"):
            run(
                runtime, observations_a, cutoff=cutoff, promote=True, authorization_mode="PAPER",
                min_train=4, fold_size=2, min_cohort=4,
                universe_members=[f"S{index:02d}" for index in range(16)], control_results=_controls(),
            )
        with runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT id, status FROM analysis.strategy_revision
                WHERE strategy_key = 'ticker-stock-alpha' ORDER BY revision
                """
            ).fetchall()
        assert [(row["id"], row["status"]) for row in rows] == [
            (first["strategy_revision_id"], "superseded"),
            (second["strategy_revision_id"], "active"),
        ]
    finally:
        runtime.close()


def test_scheduled_stock_experiment_skips_unchanged_evidence(
    migrated_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_panel.jobs import stock_alpha_walk_forward as job

    runtime = _production_runtime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC)
        symbols = [f"S{index:02d}" for index in range(16)]
        _seed_universe_tape(runtime, cutoff, symbols)
        observations = independent_observations(_observations(16, cutoff), cutoff=cutoff)
        monkeypatch.setattr(job, "load_config", lambda _path: typed_config(migrated_postgres_dsn))
        monkeypatch.setattr(job, "runtime_for_config", lambda _config: runtime)
        monkeypatch.setattr(job, "load_observations", lambda _runtime, **_kwargs: observations)
        first = job.scheduled()
        assert first["complete"] is False

        def should_not_rerun(*_args, **_kwargs):
            raise AssertionError("unchanged stock observations must not rerun controls")

        monkeypatch.setattr(job, "build_control_results", should_not_rerun)
        repeated = job.scheduled()
        assert repeated["skipped"] is True
        assert repeated["reason"] == "no_new_stock_alpha_evidence"
        assert repeated["strategy_revision_id"] == first["strategy_revision_id"]
        assert repeated["strategy_evaluation_id"] == first["strategy_evaluation_id"]
        with runtime.read() as connection:
            assert connection.execute(
                "SELECT count(*) AS count FROM analysis.strategy_revision WHERE strategy_key = 'ticker-stock-alpha'",
            ).fetchone()["count"] == 1
            assert connection.execute("SELECT count(*) AS count FROM analysis.research_trial").fetchone()["count"] == 3
    finally:
        runtime.close()


def test_scheduled_stock_qualifies_with_its_real_prediction_controls(
    migrated_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from statistics import fmean, stdev

    from investment_panel.database.research_summary import research_summary
    from investment_panel.jobs import stock_alpha_walk_forward as job

    runtime = _production_runtime(migrated_postgres_dsn)
    runtime.open()
    try:
        # Fixed historical observations keep the deterministic control draws
        # independent of when the test runner starts.
        cutoff = datetime(2026, 9, 1, 22, tzinfo=UTC)
        corpus = []
        for index, row in enumerate(_observations(160, cutoff)):
            positive = index % 40 < 30
            corpus.append(dict(
                row, outcome=float(positive), realized_return=.05 if positive else -.005,
                features={**row["features"], "relative_strength_20d": 1.0 if positive else -1.0},
            ))
        observations = independent_observations(corpus, cutoff=cutoff)
        symbols = [row["ticker"] for row in observations]
        _seed_universe_tape(runtime, cutoff, symbols)
        config = typed_config(migrated_postgres_dsn, raw={"analysis": {
            "options_decision_system": {"strategy_auto_promotion_enabled": True},
        }})
        monkeypatch.setattr(job, "load_config", lambda _path: config)
        monkeypatch.setattr(job, "runtime_for_config", lambda _config: runtime)
        monkeypatch.setattr(job, "load_observations", lambda _runtime, **_kwargs: observations)

        result = job.scheduled()
        assert result["complete"] is True
        assert result["status"] == "ok"
        with runtime.read() as connection:
            persisted = connection.execute(
                """SELECT strategy.status, evaluation.metrics
                   FROM analysis.strategy_revision strategy
                   JOIN analysis.strategy_evaluation evaluation ON evaluation.strategy_revision_id = strategy.id
                   WHERE strategy.id = %s AND evaluation.evaluation_type = 'out_of_sample'""",
                [result["strategy_revision_id"]],
            ).fetchone()
        assert persisted["status"] == "active"
        metrics = persisted["metrics"]
        assert metrics["lower_confidence_net_utility_after_costs"] > 0
        control = metrics["validation"]["checks"]["negative_controls"]
        assert control["passed"] is True
        assert control["tolerance"] == 0.0
        assert control["randomized_edge"] < 0
        assert control["white_noise_edge"] <= 0
        assert control["control_metadata"]["randomized_label"]["runs"] == 8
        assert all(gate["passed"] for gate in metrics["validation"]["gates"].values())

        predictions = result["artifact"]["predictions"]
        test_count = len(predictions)
        training_count = min(row["effective_sample_size"] for row in predictions)
        assert test_count != training_count
        assert metrics["effective_sample_size"] == training_count
        assert metrics["oos_sample_size"] == test_count
        summary = research_summary(runtime, config)
        strategy = next(row for row in summary["strategies"] if row["strategy_revision_id"] == result["strategy_revision_id"])
        displayed = next(row for row in strategy["evaluations"] if row["stage"] == "out_of_sample")
        assert displayed["independent_sample_count"] == test_count
        assert displayed["brier_score"] == pytest.approx(round(sum(
            (row["calibrated_probability"] - row["outcome"]) ** 2 for row in predictions
        ) / test_count, 6))
        net_returns = [row["net_utility_after_costs"] for row in predictions]
        assert stdev(net_returns) > 0
        expected_bound = fmean(net_returns) - 1.96 * stdev(net_returns) / test_count ** .5
        assert displayed["net_return_lower_bound"] == pytest.approx(round(expected_bound, 8))
    finally:
        runtime.close()


def test_stock_promotion_clock_migration_round_trip(migrated_postgres_dsn: str) -> None:
    def definitions():
        with psycopg.connect(migrated_postgres_dsn) as connection:
            return connection.execute(
                """SELECT proname, pg_get_functiondef(oid) FROM pg_proc
                   WHERE pronamespace = 'analysis'::regnamespace
                     AND proname IN ('enforce_research_gate_promotion_clock',
                                     'enforce_research_revision_promotion',
                                     'enforce_research_revision_promotion_hardened')
                   ORDER BY proname""",
            ).fetchall()

    upgraded = definitions()
    assert all("promotion_decision_cutoff" in definition for _name, definition in upgraded)
    downgrade_database(migrated_postgres_dsn, "20260906_0001")
    assert all("promotion_decision_cutoff" not in definition for _name, definition in definitions())
    upgrade_database(migrated_postgres_dsn)
    assert definitions() == upgraded
