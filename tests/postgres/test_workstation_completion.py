"""Integrated PostgreSQL contracts for the completed workstation paths."""
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
from investment_panel.infrastructure.postgres.workstation import WorkstationRepository
from investment_panel.domain.market.phase2 import PITObservation


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
