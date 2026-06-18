from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import time
from typing import Any

from fastapi.testclient import TestClient

from app import deps as api_deps
from app import main as api_main
from investment_panel.core.brokers import BrokerSnapshot, ProviderStatus, build_and_persist_agent_recommendations, ibkr_accept_account, ibkr_market_data_type_id, ibkr_market_snapshots, ibkr_missing_quote_symbols, ibkr_paper_account_mismatch, ibkr_position_symbol, ibkr_snapshot_status, persist_broker_snapshot, stage_paper_order, update_broker_sources
from investment_panel.core.config import IBKRConfig, load_config
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.panel import load_panel_data


class FakeProvider:
    def __init__(self, snapshot: BrokerSnapshot):
        self.snapshot = snapshot
        self.name = snapshot.status.provider

    def collect(self, symbols: list[str]) -> BrokerSnapshot:
        return self.snapshot


def test_fake_ibkr_and_moomoo_success_paths_materialize_broker_read_models(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path, read_only=False) as con:
        seed_decision_inputs(con)
        result = update_broker_sources(con, config, [FakeProvider(ibkr_success()), FakeProvider(moomoo_success())])

        assert result["status"] == "ok"
        assert query_rows(con, "SELECT status FROM broker_provider_status WHERE provider = 'ibkr'")[0]["status"] == "ok"
        assert query_rows(con, "SELECT count(*) AS count FROM broker_accounts")[0]["count"] == 1
        assert query_rows(con, "SELECT quantity FROM broker_positions WHERE symbol = 'NVDA'")[0]["quantity"] == 5
        assert query_rows(con, "SELECT count(*) AS count FROM broker_scanner_signals WHERE provider = 'moomoo'")[0]["count"] == 1
        assert query_rows(con, "SELECT count(*) AS count FROM broker_agent_recommendations")[0]["count"] >= 1


def test_ibkr_position_precedence_over_manual_portfolio_when_healthy(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path, read_only=False) as con:
        seed_decision_inputs(con)
        con.execute("INSERT OR REPLACE INTO portfolio_positions VALUES ('NVDA', 1, 100, current_date, 'manual')")
        update_broker_sources(con, config, [FakeProvider(ibkr_success())])

    panel = load_panel_data(config)
    nvda = next(row for row in panel["tables"]["portfolio"] if row["symbol"] == "NVDA")
    assert nvda["quantity"] == 5
    assert nvda["position_source"] == "ibkr"


def test_stale_ibkr_data_blocks_recommendations_without_silent_manual_fallback(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, require_account=True)
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    stale = ibkr_success(last_data_at=datetime.now(UTC) - timedelta(hours=1))
    with db(config.database.duckdb_path, read_only=False) as con:
        seed_decision_inputs(con)
        con.execute("INSERT OR REPLACE INTO portfolio_positions VALUES ('NVDA', 1, 100, current_date, 'manual')")
        update_broker_sources(con, config, [FakeProvider(stale)])

    panel = load_panel_data(config)
    nvda_portfolio = next(row for row in panel["tables"]["portfolio"] if row["symbol"] == "NVDA")
    assert nvda_portfolio["quantity"] == 5
    assert nvda_portfolio["position_source"] == "ibkr_stale"
    rec = next(row for row in panel["tables"]["agent_recommendations"] if row["symbol"] == "NVDA")
    assert "broker_account_sync_unhealthy" in rec["blockers"]
    assert rec["status"] == "blocked"


def test_provider_failure_statuses_are_persisted_and_block_agent_review(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, require_account=True)
    config = load_config(config_path)
    for status in ["gateway_offline", "quote_entitlement_failure", "rate_limited", "malformed_symbol", "session_failure"]:
        db_path = tmp_path / f"{status}.duckdb"
        config = replace(config, database=replace(config.database, duckdb_path=db_path))
        init_db(db_path)
        with db(db_path, read_only=False) as con:
            seed_decision_inputs(con)
            update_broker_sources(con, config, [FakeProvider(ibkr_failure(status))])
            row = query_rows(con, "SELECT status, detail FROM broker_provider_status WHERE provider = 'ibkr'")[0]
            assert row["status"] == status
            rec = query_rows(con, "SELECT blockers FROM broker_agent_recommendations WHERE symbol = 'NVDA'")[0]
            assert "broker_account_sync_unhealthy" in rec["blockers"]


def test_quote_only_ibkr_status_blocks_account_required_recommendations(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, require_account=True)
    config = load_config(config_path)
    observed = datetime.now(UTC)
    snapshot = BrokerSnapshot(
        status=ProviderStatus("ibkr", "quote_only", "quote-only IBKR sync", checked_at=observed, last_data_at=observed),
        market_snapshots=[{"symbol": "NVDA", "observed_at": observed, "bid": 99.9, "ask": 100.1, "last": 100, "entitlement_status": "ok", "data_status": "live"}],
    )
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path, read_only=False) as con:
        seed_decision_inputs(con)
        update_broker_sources(con, config, [FakeProvider(snapshot)])
        row = query_rows(con, "SELECT status FROM broker_provider_status WHERE provider = 'ibkr'")[0]
        rec = query_rows(con, "SELECT status, blockers FROM broker_agent_recommendations WHERE symbol = 'NVDA'")[0]
        broker_quotes = query_rows(con, "SELECT symbol, last FROM broker_market_snapshots WHERE provider = 'ibkr'")
        intraday_quotes = query_rows(con, "SELECT symbol, price, source FROM quotes_intraday WHERE source = 'broker:ibkr'")

    assert row["status"] == "quote_only"
    assert broker_quotes == [{"symbol": "NVDA", "last": 100.0}]
    assert intraday_quotes == [{"symbol": "NVDA", "price": 100.0, "source": "broker:ibkr"}]
    assert rec["status"] == "blocked"
    assert "broker_account_sync_unhealthy" in rec["blockers"]


def test_ibkr_paper_only_rejects_live_account_ids() -> None:
    class App:
        managed_accounts = ["U1234567"]
        account_values = {"U1234567": {"cash": 100.0}}
        observed_accounts = set()
        errors: list[dict[str, Any]] = []

    snapshot = ibkr_paper_account_mismatch(IBKRConfig(enabled=True, paper_only=True), App(), datetime.now(UTC), time.perf_counter())

    assert snapshot is not None
    assert snapshot.status.status == "account_mode_mismatch"
    assert snapshot.status.account_mode == "live"
    assert snapshot.accounts == []
    assert "U1234567" in (snapshot.status.account_id or "")


def test_ibkr_paper_only_rejects_live_account_ids_seen_only_in_late_callbacks() -> None:
    class App:
        managed_accounts: list[str] = []
        account_values: dict[str, dict[str, Any]] = {}
        observed_accounts = {"U1234567"}
        errors: list[dict[str, Any]] = []

    snapshot = ibkr_paper_account_mismatch(IBKRConfig(enabled=True, paper_only=True), App(), datetime.now(UTC), time.perf_counter())

    assert snapshot is not None
    assert snapshot.status.status == "account_mode_mismatch"
    assert "U1234567" in (snapshot.status.account_id or "")


def test_ibkr_paper_only_rejects_live_account_ids_even_when_account_id_scoped() -> None:
    class App:
        managed_accounts = ["DU123", "U1234567"]
        account_values = {"DU123": {"cash": 100.0}}
        observed_accounts = {"U1234567"}
        errors: list[dict[str, Any]] = []

    snapshot = ibkr_paper_account_mismatch(IBKRConfig(enabled=True, account_id="DU123", paper_only=True), App(), datetime.now(UTC), time.perf_counter())

    assert snapshot is not None
    assert snapshot.status.status == "account_mode_mismatch"
    assert snapshot.status.account_mode == "live"
    assert "U1234567" in (snapshot.status.account_id or "")


def test_account_mode_mismatch_clears_stale_account_read_models(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path, read_only=False) as con:
        seed_decision_inputs(con)
        update_broker_sources(con, config, [FakeProvider(ibkr_success())])
        assert query_rows(con, "SELECT count(*) AS count FROM broker_accounts WHERE provider = 'ibkr'")[0]["count"] == 1
        persist_broker_snapshot(
            con,
            BrokerSnapshot(
                ProviderStatus(
                    "ibkr",
                    "account_mode_mismatch",
                    "live account exposed while paper_only is enabled",
                    checked_at=datetime.now(UTC),
                    account_id="U1234567",
                    account_mode="live",
                )
            ),
        )

        assert query_rows(con, "SELECT count(*) AS count FROM broker_accounts WHERE provider = 'ibkr'")[0]["count"] == 0
        assert query_rows(con, "SELECT count(*) AS count FROM broker_positions WHERE provider = 'ibkr'")[0]["count"] == 0
        assert query_rows(con, "SELECT count(*) AS count FROM broker_orders WHERE provider = 'ibkr'")[0]["count"] == 0
        assert query_rows(con, "SELECT count(*) AS count FROM broker_fills WHERE provider = 'ibkr'")[0]["count"] == 0


def test_configured_ibkr_account_id_scopes_account_callbacks() -> None:
    config = IBKRConfig(enabled=True, account_id="DU123", paper_only=True)

    assert ibkr_accept_account(config, "DU123")
    assert not ibkr_accept_account(config, "DU999")
    mismatch = ibkr_paper_account_mismatch(
        config,
        type("App", (), {"managed_accounts": ["DU123", "U1234567"], "account_values": {}, "errors": []})(),
        datetime.now(UTC),
        time.perf_counter(),
    )
    assert mismatch is not None
    assert mismatch.status.status == "account_mode_mismatch"
    assert "U1234567" in (mismatch.status.account_id or "")


def test_quote_entitlement_failure_does_not_discard_valid_account_sync() -> None:
    status, detail = ibkr_snapshot_status(
        [{"account_id": "DU123"}],
        [],
        [],
        [],
        [],
        [{"code": 10167, "message": "Requested market data is not subscribed."}],
    )

    assert status == "ok"
    assert "entitlement" in detail


def test_quote_only_status_does_not_claim_account_sync() -> None:
    status, detail = ibkr_snapshot_status(
        [],
        [],
        [],
        [],
        [{"symbol": "NVDA", "last": 100}],
        [],
    )

    assert status == "quote_only"
    assert "no account" in detail


def test_ibkr_live_or_delayed_requests_live_market_data_type() -> None:
    assert ibkr_market_data_type_id("live_or_delayed") == 1
    assert ibkr_market_data_type_id("delayed") == 3


def test_ibkr_live_or_delayed_retries_only_symbols_without_snapshots() -> None:
    assert ibkr_missing_quote_symbols(
        ["AAPL", "NVDA", "MSFT"],
        [{"symbol": "AAPL", "last": 100}, {"symbol": "NVDA", "last": 200}],
    ) == ["MSFT"]


def test_ibkr_mark_price_only_quote_is_persistable() -> None:
    observed = datetime.now(UTC)
    rows = ibkr_market_snapshots(
        {9200: {"symbol": "AAPL", "observed_at": observed, "mark_price": 123.45, "market_data_type": 1, "raw": {"tick_price_37": 123.45}}},
        [],
        observed,
    )

    assert rows == [
        {
            "symbol": "AAPL",
            "observed_at": observed,
            "bid": None,
            "ask": None,
            "last": 123.45,
            "close": None,
            "volume": None,
            "entitlement_status": "ok",
            "data_status": "live",
            "raw": {"tick_price_37": 123.45, "market_data_status": "live"},
        }
    ]


def test_ibkr_derivative_positions_keep_contract_level_symbol() -> None:
    class StockContract:
        secType = "STK"
        symbol = "AAPL"
        localSymbol = ""
        conId = 123

    class OptionContract:
        secType = "OPT"
        symbol = "AAPL"
        localSymbol = "AAPL  260620C00100000"
        conId = 456

    class FutureContract:
        secType = "FUT"
        symbol = "ES"
        localSymbol = ""
        conId = 789

    assert ibkr_position_symbol(StockContract()) == "AAPL"
    assert ibkr_position_symbol(OptionContract()) == "AAPL_260620C00100000"
    assert ibkr_position_symbol(FutureContract()) == "ES:FUT:789"


def test_market_data_only_mode_does_not_require_broker_login(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path, read_only=False) as con:
        seed_decision_inputs(con)
        con.execute("INSERT OR REPLACE INTO portfolio_positions VALUES ('NVDA', 2, 75, current_date, 'manual')")
        update_broker_sources(con, config, [FakeProvider(ibkr_failure("disabled")), FakeProvider(ibkr_failure("disabled"))])

        rec = query_rows(con, "SELECT status, blockers FROM broker_agent_recommendations WHERE symbol = 'NVDA'")[0]
        assert rec["status"] == "paper_ready"
        assert "broker_account_sync_unhealthy" not in rec["blockers"]
        portfolio = query_rows(con, "SELECT symbol, quantity FROM portfolio_positions WHERE symbol = 'NVDA'")[0]
        assert portfolio["quantity"] == 2


def test_recommendation_safety_gates_and_paper_order_audit_trail(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path, read_only=False) as con:
        seed_decision_inputs(con, with_thin_symbol=True)
        update_broker_sources(con, config, [FakeProvider(ibkr_success())])
        recs = query_rows(con, "SELECT * FROM broker_agent_recommendations")
        nvda = next(row for row in recs if row["symbol"] == "NVDA")
        thin = next(row for row in recs if row["symbol"] == "THIN")
        assert nvda["status"] == "paper_ready"
        assert "required_evidence_missing" in thin["blockers"]

        staged = stage_paper_order(con, nvda["id"])
        assert staged["status"] == "staged"
        blocked = stage_paper_order(con, thin["id"])
        assert blocked["status"] == "blocked"
        orders = query_rows(con, "SELECT status, audit_trail FROM broker_paper_orders ORDER BY created_at")
        assert {row["status"] for row in orders} == {"staged", "blocked"}
        assert all("paper_order_stage_requested" in row["audit_trail"] for row in orders)


def test_recommendations_block_paper_staging_without_usable_quote(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    now = datetime.now(UTC)
    with db(config.database.duckdb_path, read_only=False) as con:
        con.execute(
            """
            INSERT OR REPLACE INTO decision_queue
            (symbol, as_of, rank, action_grade, decision_bucket, score,
             discovery_score, decision_score, action_score, freshness_status,
             quote_freshness, daily_analysis_freshness, filing_freshness,
             thesis_freshness, overall_decision_freshness, source_cluster,
             evidence_count, raw_source_rows, independent_source_count,
             evidence_items_count, primary_evidence_count, inclusion_reasons,
             blocking_gates, decision_basis, latest_quote, latest_quote_at,
             latest_observed_at, next_event_at, catalyst_window, liquidity_grade,
             portfolio_impact, invalidation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "NOQUOTE",
                now,
                1,
                "Act",
                "Act",
                95.0,
                90.0,
                95.0,
                95.0,
                "fresh",
                "fresh",
                "fresh",
                "fresh",
                "fresh",
                "fresh",
                "arco_thesis",
                3,
                3,
                2,
                3,
                1,
                '["source backed setup"]',
                "[]",
                '{"summary":"NOQUOTE has an actionable setup but no quote.","source_counts":{"arco_thesis":1,"sepa":1},"evidence_count":3,"primary_evidence_count":1,"asset_class":"equity","freshness":{"quote_freshness":"fresh","daily_analysis_freshness":"fresh"}}',
                None,
                None,
                now,
                None,
                "",
                "A",
                "{}",
                "",
            ],
        )
        build_and_persist_agent_recommendations(con, config.data_sources.brokers.policy)
        rec = query_rows(con, "SELECT status, action, blockers, paper_order_preview FROM broker_agent_recommendations WHERE symbol = 'NOQUOTE'")[0]

    assert rec["status"] == "blocked"
    assert rec["action"] == "block"
    assert "missing_usable_quote" in rec["blockers"]
    assert json.loads(rec["paper_order_preview"])["limit_price"] is None


def test_monitor_recommendations_without_quote_stay_monitor(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    now = datetime.now(UTC)
    with db(config.database.duckdb_path, read_only=False) as con:
        con.execute(
            """
            INSERT OR REPLACE INTO decision_queue
            (symbol, as_of, rank, action_grade, decision_bucket, score,
             discovery_score, decision_score, action_score, freshness_status,
             quote_freshness, daily_analysis_freshness, filing_freshness,
             thesis_freshness, overall_decision_freshness, source_cluster,
             evidence_count, raw_source_rows, independent_source_count,
             evidence_items_count, primary_evidence_count, inclusion_reasons,
             blocking_gates, decision_basis, latest_quote, latest_quote_at,
             latest_observed_at, next_event_at, catalyst_window, liquidity_grade,
             portfolio_impact, invalidation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "WATCH",
                now,
                1,
                "Watch",
                "Watch",
                55.0,
                55.0,
                55.0,
                55.0,
                "fresh",
                "fresh",
                "fresh",
                "fresh",
                "fresh",
                "fresh",
                "arco_thesis",
                3,
                3,
                2,
                3,
                1,
                '["source backed watch item"]',
                "[]",
                '{"summary":"WATCH is a monitor-only setup.","source_counts":{"arco_thesis":1,"sepa":1},"evidence_count":3,"primary_evidence_count":1,"asset_class":"equity","freshness":{"quote_freshness":"fresh","daily_analysis_freshness":"fresh"}}',
                None,
                None,
                now,
                None,
                "",
                "A",
                "{}",
                "",
            ],
        )
        build_and_persist_agent_recommendations(con, config.data_sources.brokers.policy)
        rec = query_rows(con, "SELECT status, action, blockers, paper_order_preview FROM broker_agent_recommendations WHERE symbol = 'WATCH'")[0]

    assert rec["status"] == "monitor"
    assert rec["action"] == "monitor"
    assert "missing_usable_quote" not in rec["blockers"]
    assert json.loads(rec["paper_order_preview"])["limit_price"] is None


def test_broker_api_routes_smoke(tmp_path: Path, monkeypatch: Any) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path, read_only=False) as con:
        seed_decision_inputs(con)
        update_broker_sources(con, config, [FakeProvider(ibkr_success())])

    monkeypatch.setattr(api_deps, "load_config", lambda: {"database": {"duckdb_path": str(config.database.duckdb_path)}})
    monkeypatch.setattr(api_deps, "load_core_config", lambda path="config.yaml": config)
    api_deps._invalidate_context_cache()
    client = TestClient(api_main.app)
    for path in ["/api/broker/status", "/api/broker/accounts", "/api/broker/positions", "/api/agent/recommendations", "/api/paper-orders"]:
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")


def seed_decision_inputs(con: Any, *, with_thin_symbol: bool = False) -> None:
    now = datetime.now(UTC)
    con.execute("INSERT OR REPLACE INTO instruments VALUES ('NVDA', 'Nvidia', 'equity', NULL, NULL, 'ai', 'test')")
    con.execute("INSERT OR REPLACE INTO quotes_intraday VALUES ('NVDA', ?, 100, 1.2, 1.2, 'USD', 'test', '{}')", [now])
    con.execute("INSERT OR REPLACE INTO technical_features VALUES ('NVDA', current_date, '{\"technical_score\":90}')")
    con.execute("INSERT OR REPLACE INTO sepa_analyses VALUES ('NVDA', current_date, 95, 'stage_2', 'strong', '{}', '{}')")
    con.execute("INSERT OR REPLACE INTO liquidity_metrics VALUES ('NVDA', current_date, 'A', 10000000, 1000000000, NULL, NULL, NULL, '{}')")
    con.execute("INSERT OR REPLACE INTO birdclaw_theses VALUES ('t1', 'NVDA', 'arco', ?, 'AI infra thesis', '[]', '{}', 'https://example.com')", [now])
    con.execute("INSERT OR REPLACE INTO candidates VALUES ('c1', current_date, 'NVDA', 98, '{\"components\":{\"technical\":90}}', '[{\"source\":\"test\"}]', 'research')")
    if with_thin_symbol:
        con.execute("INSERT OR REPLACE INTO instruments VALUES ('THIN', 'Thin Co', 'equity', NULL, NULL, 'test', 'test')")
        con.execute("INSERT OR REPLACE INTO quotes_intraday VALUES ('THIN', ?, 20, 0, 0, 'USD', 'test', '{}')", [now])
        con.execute("INSERT OR REPLACE INTO technical_features VALUES ('THIN', current_date, '{\"technical_score\":75}')")
        con.execute("INSERT OR REPLACE INTO liquidity_metrics VALUES ('THIN', current_date, 'B', 1000000, 50000000, NULL, NULL, NULL, '{}')")


def ibkr_success(last_data_at: datetime | None = None) -> BrokerSnapshot:
    now = datetime.now(UTC)
    observed = last_data_at or now
    return BrokerSnapshot(
        status=ProviderStatus(
            "ibkr",
            "ok",
            "fake IBKR sync ok",
            checked_at=now,
            account_id="DU123",
            account_mode="paper",
            last_data_at=observed,
            capabilities=["positions", "cash", "buying_power", "margin_risk", "pnl", "orders", "fills", "account_mode", "market_snapshots", "options", "scanner"],
        ),
        accounts=[{"account_id": "DU123", "account_mode": "paper", "currency": "USD", "cash": 50_000, "buying_power": 100_000, "net_liquidation": 200_000, "updated_at": observed}],
        positions=[{"account_id": "DU123", "symbol": "NVDA", "asset_class": "equity", "quantity": 5, "average_cost": 80, "market_price": 100, "market_value": 500, "unrealized_pnl": 100, "updated_at": observed}],
        market_snapshots=[{"symbol": "NVDA", "observed_at": observed, "bid": 99.9, "ask": 100.1, "last": 100, "entitlement_status": "ok", "data_status": "fresh"}],
    )


def moomoo_success() -> BrokerSnapshot:
    now = datetime.now(UTC)
    return BrokerSnapshot(
        status=ProviderStatus("moomoo", "ok", "fake moomoo OpenD sync ok", checked_at=now, account_mode="paper", last_data_at=now),
        market_snapshots=[{"symbol": "NVDA", "observed_at": now, "bid": 99.8, "ask": 100.2, "last": 100, "entitlement_status": "ok", "data_status": "fresh"}],
        scanner_signals=[{"symbol": "NVDA", "observed_at": now, "signal_type": "capital_flow", "rank": 1, "score": 88, "metrics": {"net_inflow": 12345}}],
    )


def ibkr_failure(status: str) -> BrokerSnapshot:
    return BrokerSnapshot(status=ProviderStatus("ibkr", status, f"fake {status}", checked_at=datetime.now(UTC), capabilities=["positions"]))


def write_config(tmp_path: Path, *, require_account: bool = False) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
database:
  duckdb_path: {tmp_path / "investment.duckdb"}
nas:
  status_dir: {tmp_path / "status"}
data_sources:
  brokers:
    enabled: true
    ibkr:
      enabled: true
    moomoo:
      enabled: true
    policy:
      require_account_for_recommendations: {str(require_account).lower()}
""",
        encoding="utf-8",
    )
    return config_path
