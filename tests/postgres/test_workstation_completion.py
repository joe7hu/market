"""Integrated PostgreSQL contracts for the completed workstation paths."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from investment_panel.settings import AppConfig
from investment_panel.infrastructure.postgres.analysis import AnalysisRepository
from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.instruments import reconcile_instrument
from investment_panel.infrastructure.postgres.market_analysis import load_market_inputs
from investment_panel.infrastructure.postgres.options_paper_execution import OptionsPaperExecutionRepository
from investment_panel.infrastructure.postgres.panel_publications import published_tables
from investment_panel.infrastructure.postgres.paper_workbench import PaperWorkbenchRepository
from investment_panel.infrastructure.postgres.phase2 import Phase2Repository
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.infrastructure.postgres.ticker_decisions import TickerDecisionRepository
from investment_panel.infrastructure.postgres.workstation import (
    WorkstationRepository,
    service_blocking_experiment_incidents,
)
from investment_panel.domain.market.phase2 import PITObservation
from investment_panel.domain.decision import build_ticker_decision


@pytest.fixture
def runtime(migrated_postgres_dsn):
    value = DatabaseRuntime(migrated_postgres_dsn)
    value.open()
    yield value
    value.close()


def test_new_empty_market_model_cannot_resurrect_an_older_publication(runtime):
    repo = AnalysisRepository(runtime)
    def publish(version, models):
        run = repo.start_run("market", input_cutoff=datetime.now(UTC), code_version="test.coherent", inputs={"version": version})
        return repo.publish(run, "market", models, complete_run_summary={"version": version})
    first = publish(1, {"market_environment_assets": [{"symbol": "SPY", "price": 100}],
                        "market_environment_model": [{"category": "Price Trend", "score": 50}]})
    counts = {}
    old = published_tables(runtime, ("market_environment_assets", "market_environment_model"), total_counts=counts)
    assert old["market_environment_assets"][0]["publication_id"] == str(first)
    second = publish(2, {"market_environment_assets": [], "market_environment_model": [{"category": "Price Trend", "score": 60}]})
    current = published_tables(runtime, ("market_environment_assets", "market_environment_model"), row_limits={"market_environment_model": 1}, total_counts=counts)
    assert current["market_environment_assets"] == [] and counts["market_environment_assets"] == 0
    assert current["market_environment_model"][0]["publication_id"] == str(second)


def test_status_uses_real_queries_and_distinguishes_no_data_from_failed_read(runtime):
    result = WorkstationRepository(runtime).status(AppConfig())
    assert result["failed_reads"] == []
    assert result["market"]["status"] == "not_published"
    assert result["paper"]["status"] == "available" and result["paper"]["counts"] == {}


def test_status_reads_latest_normalized_decision_with_history(runtime):
    symbol = "STATUSREF"
    with runtime.transaction() as connection:
        reconcile_instrument(connection, symbol)
    repository = TickerDecisionRepository(runtime)
    as_of = datetime.now(UTC) - timedelta(hours=1)
    latest_id = None
    for offset in range(3):
        reference = as_of + timedelta(minutes=offset)
        latest_id = repository.publish(build_ticker_decision(symbol, {
            "decision_queue": [{"symbol": symbol, "stance": "NEUTRAL",
                                "available_at": reference.isoformat()}],
        }, as_of=reference))["ticker_decision_id"]
    status = WorkstationRepository(runtime).status(
        AppConfig(watchlist=[{"symbol": symbol}]))
    assert "decision_service" not in status["failed_reads"]
    item = next(row for row in status["decision_service"]["instruments"]
                if row["symbol"] == symbol)
    assert datetime.fromisoformat(item["decision_as_of"]) == as_of + timedelta(minutes=2)
    with runtime.transaction() as connection:
        connection.execute(
            "ALTER TABLE analysis.ticker_decision DISABLE TRIGGER ticker_decision_input_refs_valid")
        connection.execute("""
            UPDATE analysis.ticker_decision
            SET input_payload_refs = jsonb_build_object('unshown', %s::text)
            WHERE id = %s::uuid
        """, ["0" * 64, latest_id])
        connection.execute(
            "ALTER TABLE analysis.ticker_decision ENABLE TRIGGER ticker_decision_input_refs_valid")
    failed = WorkstationRepository(runtime).status(
        AppConfig(watchlist=[{"symbol": symbol}]))
    assert "decision_service" in failed["failed_reads"]


def test_funnel_expands_current_evidence_once_per_decision(runtime, monkeypatch):
    """Count actual PostgreSQL function calls, including nested expansion."""
    symbol = "EXPANDREF"
    with runtime.transaction() as connection:
        reconcile_instrument(connection, symbol)
    repository = TickerDecisionRepository(runtime)
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    for offset in range(3):
        reference = cutoff + timedelta(minutes=offset)
        repository.publish(build_ticker_decision(symbol, {
            "decision_queue": [{"symbol": symbol, "stance": "NEUTRAL",
                                "available_at": reference.isoformat()}],
        }, as_of=reference))
    original_read = runtime.read
    calls = {}

    @contextmanager
    def counted_read(*args, **kwargs):
        with original_read(*args, **kwargs) as connection:
            connection.execute("SET LOCAL track_functions = 'all'")
            yield connection
            calls.update({row["funcname"]: row["calls"] for row in connection.execute(
                "SELECT funcname, calls FROM pg_stat_xact_user_functions WHERE schemaname = 'analysis'"
            )})

    monkeypatch.setattr(runtime, "read", counted_read)
    rows = repository._current_funnel_rows(reference=datetime.now(UTC), symbols=[symbol])
    assert len(rows) == 1
    assert rows[0]["as_of"] == cutoff + timedelta(minutes=2)
    assert 0 < calls.get("expand_decision_resolution", 0) <= 2


def test_compact_inbox_expands_only_current_decision_evidence(runtime):
    from investment_panel.infrastructure.postgres.panel_models import COMPACT_TICKER_DECISIONS_QUERY

    symbol = "INBOXREF"
    with runtime.transaction() as connection:
        reconcile_instrument(connection, symbol)
    repository = TickerDecisionRepository(runtime)
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    for offset in range(3):
        reference = cutoff + timedelta(minutes=offset)
        repository.publish(build_ticker_decision(symbol, {
            "decision_queue": [{"symbol": symbol, "stance": "NEUTRAL",
                                "available_at": reference.isoformat()}],
        }, as_of=reference))
    with runtime.read() as connection:
        connection.execute("SET LOCAL track_functions = 'all'")
        rows = connection.execute(COMPACT_TICKER_DECISIONS_QUERY).fetchall()
        calls = {row["funcname"]: row["calls"] for row in connection.execute(
            "SELECT funcname, calls FROM pg_stat_xact_user_functions WHERE schemaname = 'analysis'"
        )}
    assert len(rows) == 1
    assert rows[0]["as_of"] == cutoff + timedelta(minutes=2)
    assert rows[0]["input_manifest"]["opportunity_rank"] is None
    assert 0 < calls.get("expand_decision_manifest", 0) <= 4


def test_health_does_not_read_unused_instrument_snapshot(runtime, monkeypatch):
    symbol = "HEALTHCOMPACT"
    with runtime.transaction() as connection:
        reconcile_instrument(connection, symbol)
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    decision = TickerDecisionRepository(runtime).publish(build_ticker_decision(symbol, {
        "decision_queue": [{"symbol": symbol, "stance": "NEUTRAL",
                            "available_at": cutoff.isoformat()}],
    }, as_of=cutoff))
    with runtime.transaction() as connection:
        connection.execute("""
            UPDATE analysis.ticker_decision SET input_manifest = input_manifest - 'instrument_state_snapshot',
              evidence_refs = jsonb_set(evidence_refs,
              '{manifest}', COALESCE(evidence_refs->'manifest', '{}') || jsonb_build_object(
                'instrument_state_snapshot', analysis.intern_decision_payload('{"unused":true}')))
            WHERE id = %s::uuid
        """, [decision["ticker_decision_id"]])
    original_snapshot = runtime.snapshot
    calls = {}

    @contextmanager
    def counted_snapshot(*args, **kwargs):
        with original_snapshot(*args, **kwargs) as connection:
            connection.execute("SET LOCAL track_functions = 'all'")
            yield connection
            calls.update({row["funcname"]: row["calls"] for row in connection.execute(
                "SELECT funcname, calls FROM pg_stat_xact_user_functions WHERE schemaname = 'analysis'"
            )})

    monkeypatch.setattr(runtime, "snapshot", counted_snapshot)
    result = WorkstationRepository(runtime).status(AppConfig(watchlist=[{"symbol": symbol}]))
    assert result["failed_reads"] == []
    assert calls.get("decision_payload", 0) == 1  # reference signal; this decision has no trade plan


def test_attribution_does_not_expand_planless_history(runtime):
    from investment_panel.infrastructure.postgres.ticker_decisions import OUTCOME_ATTRIBUTION_DECISION_QUERY

    symbol = "PLANLESSREF"
    with runtime.transaction() as connection:
        reconcile_instrument(connection, symbol)
    repository = TickerDecisionRepository(runtime)
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    for offset in range(3):
        reference = cutoff + timedelta(minutes=offset)
        repository.publish(build_ticker_decision(symbol, {
            "decision_queue": [{"symbol": symbol, "stance": "NEUTRAL",
                                "available_at": reference.isoformat()}],
        }, as_of=reference))
    with runtime.read() as connection:
        connection.execute("SET LOCAL track_functions = 'all'")
        rows = connection.execute(OUTCOME_ATTRIBUTION_DECISION_QUERY, [datetime.now(UTC)]).fetchall()
        calls = {row["funcname"]: row["calls"] for row in connection.execute(
            "SELECT funcname, calls FROM pg_stat_xact_user_functions WHERE schemaname = 'analysis'"
        )}
    assert rows == []
    assert calls.get("expand_decision_manifest", 0) == 0


def test_attribution_does_not_read_unused_snapshot_payloads(runtime):
    from investment_panel.infrastructure.postgres.ticker_decisions import OUTCOME_ATTRIBUTION_DECISION_QUERY

    with runtime.transaction() as connection:
        instrument = reconcile_instrument(connection, "ATTRCOMPACT")
        connection.execute("""
            INSERT INTO analysis.ticker_decision (instrument_id, decision_revision,
              contract_version, as_of, input_hash, code_version, experiment_id,
              tactical, fundamental, capital_action, risk_policy, input_manifest, evidence_refs)
            VALUES (%s, 'compact', 'test', now() - interval '1 hour', %s,
              'test', 'test', '{}', '{}', '{}', '{}', '{}',
              jsonb_build_object('resolution_plan_fields', jsonb_build_array('trade_plan_id'),
                'manifest', jsonb_build_object(
                'trade_plan', analysis.intern_decision_payload('{"id":"compact","trade_plan_id":"compact"}'),
                'instrument_state_snapshot', analysis.intern_decision_payload('{"unused":true}'),
                'reference_signal', analysis.intern_decision_payload('{"unused":true}'))))
        """, [instrument, "f" * 64])
    with runtime.read() as connection:
        connection.execute("SET LOCAL track_functions = 'all'")
        rows = connection.execute(OUTCOME_ATTRIBUTION_DECISION_QUERY, [datetime.now(UTC)]).fetchall()
        calls = {row["funcname"]: row["calls"] for row in connection.execute(
            "SELECT funcname, calls FROM pg_stat_xact_user_functions WHERE schemaname = 'analysis'"
        )}
    assert len(rows) == 1
    assert rows[0]["input_manifest"]["trade_plan"] == {"id": "compact", "trade_plan_id": "compact"}
    assert rows[0]["resolution"]["trade_plan_id"] == "compact"
    assert calls.get("decision_payload", 0) == 2
    with runtime.read() as connection:
        plan = connection.execute("EXPLAIN (VERBOSE, FORMAT JSON) " + OUTCOME_ATTRIBUTION_DECISION_QUERY,
                                  [datetime.now(UTC)]).fetchone()["QUERY PLAN"][0]["Plan"]
    def assert_sort_before_expansion(node):
        if node["Node Type"] == "Sort":
            assert "expand_decision" not in str(node.get("Output", []))
        for child in node.get("Plans", []):
            assert_sort_before_expansion(child)
    assert_sort_before_expansion(plan)


def test_unpriceable_experiment_mark_does_not_call_a_healthy_worker_down():
    incidents = [
        {"reason": "experiment_quote_overdue", "job": "refresh_paper_quotes"},
        {"reason": "experiment_management_overdue", "job": "process_options_paper_orders"},
    ]

    assert service_blocking_experiment_incidents(incidents) == [incidents[1]]


def test_funded_nav_is_observed_once_not_synthesized_from_research(runtime):
    repo = PaperWorkbenchRepository(runtime)
    assert repo.capture_nav()["status"] == "unfunded"
    repo.initialize_account(100000, authorization="test simulated account")
    recorded = repo.capture_nav()
    assert recorded["status"] == "complete"
    assert repo.capture_nav(now=recorded["observed_at"])["status"] == "already_recorded"
    history = repo.account_history()
    assert len(history["points"]) == 1
    assert history["points"][0]["nav"] == 100000 and history["points"][0]["net_pnl"] == 0
    with runtime.read() as connection:
        permissions = connection.execute("SELECT has_table_privilege('market_app', 'app.paper_nav_observation', 'INSERT') AS insert_ok, has_table_privilege('market_app', 'app.paper_nav_observation', 'UPDATE') AS update_ok, has_table_privilege('market_app', 'app.paper_nav_observation', 'DELETE') AS delete_ok").fetchone()
    assert permissions["insert_ok"] and not permissions["update_ok"] and not permissions["delete_ok"]


def test_incomplete_nav_creates_a_gap_never_zero(runtime, monkeypatch):
    repo = PaperWorkbenchRepository(runtime)
    repo.initialize_account(100000, authorization="test")
    monkeypatch.setattr(repo, "account", lambda **kw: {"status": "incomplete", "blockers": ["paper_position_mark_unavailable"]})
    assert repo.capture_nav()["status"] == "incomplete"
    point = repo.account_history()["points"][0]
    assert point["status"] == "incomplete" and point["nav"] is None


def test_management_claims_rotate_without_rewriting_accounting_clock(runtime, monkeypatch):
    with runtime.transaction() as connection:
        instrument = reconcile_instrument(connection, "FAIR" + uuid4().hex[:6])
        for _ in range(5):
            connection.execute("INSERT INTO app.paper_order (instrument_id, side, quantity, status, lane, paper_only) VALUES (%s, 'buy', 1, 'staged', 'radar', true)", [instrument])
        original = {str(row["id"]): row["updated_at"] for row in connection.execute("SELECT id, updated_at FROM app.paper_order").fetchall()}
    repo = OptionsPaperExecutionRepository(runtime)
    checked = []
    def check(order_id, now):
        checked.append(order_id)
        return {"paper_order_id": order_id, "status": "staged"}
    monkeypatch.setattr(repo, "_manage_one", check)
    for _ in range(3):
        repo.manage_orders(lanes=["radar"], decision_inbox_enabled=False, now=datetime.now(UTC), limit=2)
    assert len(set(checked[:5])) == 5
    with runtime.read() as connection:
        for row in connection.execute("SELECT id, updated_at, execution_quote FROM app.paper_order").fetchall():
            assert row["updated_at"] == original[str(row["id"])]
            assert row["execution_quote"]["management"]["last_checked_at"]


def test_advanced_observations_keep_recent_each_series_beyond_global_500(runtime):
    ingestion, phase2 = IngestionRepository(runtime), Phase2Repository(runtime)
    ingestion.register_source("treasury", name="Treasury test", family="phase2", kind="test", operational_state="active")
    before = datetime.now(UTC) - timedelta(days=1)
    observations = [PITObservation(observation_id=f"window-{dimension}-{i}", field_name="rates.nominal_yield",
        dimension=dimension, asset_class="equity", source_id="treasury", source_version="test.v1", value=float(i),
        observed_at=before + timedelta(seconds=i), available_at=before + timedelta(seconds=i), content_hash="a" * 64)
        for dimension in ("a-dimension", "z-dimension") for i in range(350)]
    with ingestion.run("treasury", "test.observations") as run:
        payload = ingestion.record_payload(run.id, "fixture.json", sha256="b" * 64, byte_count=1, schema_version="test.v1")
        phase2.record_observations(observations, ingest_run_id=str(run.id), payload_id=payload)
        run.finish("succeeded")
    data = load_market_inputs(runtime, as_of=datetime.now(UTC), benchmark_symbols=[])
    assert "advanced_observations" not in data["optional_errors"]
    rows = data["phase2_rows"]
    assert len(rows) == 256
    assert {row["dimension"] for row in rows} == {"a-dimension", "z-dimension"}
    assert all(row["value"] >= 222 for row in rows)
    assert all(row["source_id"] == "treasury" for row in rows)


def test_standby_phase2_sources_skip_the_observation_scan(runtime):
    """Standby integrations describe absence without spending the API read budget."""

    class ConnectionSpy:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, *args, **kwargs):
            assert "FROM raw.market_observation observation" not in str(query)
            return self.connection.execute(query, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    class RuntimeSpy:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def read(self):
            wrapped = self.wrapped

            class ReadContext:
                def __enter__(self):
                    self.context = wrapped.read()
                    return ConnectionSpy(self.context.__enter__())

                def __exit__(self, *args):
                    return self.context.__exit__(*args)

            return ReadContext()

    ingestion = IngestionRepository(runtime)
    ingestion.register_source("standby-phase2", name="Standby phase 2", family="phase2", kind="test", operational_state="standby")

    data = load_market_inputs(RuntimeSpy(runtime), as_of=datetime.now(UTC), benchmark_symbols=[])

    assert data["phase2_rows"] == []
    assert "advanced_observations" not in data["optional_errors"]


def test_budget_limited_manager_reaches_unchecked_tail_of_the_same_batch(runtime, monkeypatch):
    """Batch claims are not evidence of checks, even if every row fits the batch."""
    from investment_panel.infrastructure.postgres import options_paper_execution as owner
    from types import SimpleNamespace
    with runtime.transaction() as connection:
        instrument = reconcile_instrument(connection, "BUDGET" + uuid4().hex[:6])
        for _ in range(5):
            connection.execute("INSERT INTO app.paper_order (instrument_id, side, quantity, status, lane, paper_only) VALUES (%s, 'buy', 1, 'staged', 'radar', true)", [instrument])
    checked = []
    repo = OptionsPaperExecutionRepository(runtime)
    monkeypatch.setattr(repo, "_manage_one", lambda order_id, now: checked.append(order_id) or {"paper_order_id": order_id, "status": "staged"})
    start = datetime.now(UTC)
    for tick in range(5):
        times = iter([0, 0, 9])
        monkeypatch.setattr(owner, "time", SimpleNamespace(monotonic=lambda: next(times)))
        result = repo.manage_orders(lanes=["radar"], decision_inbox_enabled=False,
            now=start + timedelta(seconds=15 * tick), limit=5, max_work_seconds=8)
        assert result[-1]["reason"] == "management_budget_exhausted"
    assert len(checked) == len(set(checked)) == 5


def test_failed_targeted_quote_symbols_do_not_starve_other_active_contracts(runtime, monkeypatch):
    from psycopg.types.json import Jsonb
    from investment_panel.infrastructure.postgres import options
    now = datetime.now(UTC)
    with runtime.transaction() as connection:
        for symbol in ("FIRST", "SECOND"):
            instrument = reconcile_instrument(connection, symbol)
            contract = connection.execute("INSERT INTO catalog.option_contract (underlying_instrument_id, expiration, strike, option_type, multiplier, deliverable_key) VALUES (%s, %s, 100, 'call', 100, %s) RETURNING id",
                [instrument, now.date() + timedelta(days=30), symbol]).fetchone()["id"]
            order = connection.execute("INSERT INTO app.paper_order (instrument_id, side, quantity, status, lane, paper_only) VALUES (%s, 'buy', 1, 'staged', 'radar', true) RETURNING id", [instrument]).fetchone()["id"]
            connection.execute("INSERT INTO app.paper_order_leg (paper_order_id, leg_index, contract_id, option_type, side, strike, bid, ask, bid_size, ask_size, quote_time) VALUES (%s, 0, %s, 'call', 'buy', 100, 1, 1.1, 10, 10, %s)", [order, contract, now])
        connection.execute("INSERT INTO ops.job_run (job_name, status, started_at, finished_at, summary) VALUES ('refresh_paper_quotes', 'failed', %s, %s, %s)",
            [now - timedelta(minutes=1), now, Jsonb({"source_id": "robinhood", "symbols_attempted": ["FIRST"]})])
    monkeypatch.setattr(options, "runtime_for_config", lambda _: runtime)
    selected = options.active_paper_contracts(AppConfig(), "robinhood")
    assert [row["symbol"] for row in selected] == ["SECOND", "FIRST"]
