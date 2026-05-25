from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app import main as api_main
from investment_panel.core.brokers import BrokerSnapshot, ProviderStatus, build_and_persist_agent_recommendations, stage_paper_order, update_broker_sources
from investment_panel.core.config import load_config
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


def test_broker_api_routes_smoke(tmp_path: Path, monkeypatch: Any) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path, read_only=False) as con:
        seed_decision_inputs(con)
        update_broker_sources(con, config, [FakeProvider(ibkr_success())])

    monkeypatch.setattr(api_main, "load_config", lambda: {"database": {"duckdb_path": str(config.database.duckdb_path)}})
    monkeypatch.setattr(api_main, "load_core_config", lambda path="config.yaml": config)
    api_main._CONTEXT_CACHE.update({"expires_at": 0.0, "config_key": None, "value": None})
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
