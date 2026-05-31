from app import data_access


def test_empty_database_returns_duckdb_status(tmp_path) -> None:
    panel_data = data_access.load_panel_data({"database": {"duckdb_path": str(tmp_path / "missing.duckdb")}})

    assert panel_data.status.ready is False
    assert panel_data.status.source == "duckdb"
    assert "Database is initialized" in panel_data.status.message
    assert panel_data.rows("candidates") == []


def test_load_config_honors_market_duckdb_path_override(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "canonical.duckdb"
    monkeypatch.setenv("MARKET_DUCKDB_PATH", str(db_path))

    config = data_access.load_config(tmp_path / "missing-config.yaml")

    assert config["database"]["duckdb_path"] == str(db_path)


def test_table_payload_normalizes_rows() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={"candidates": [{"symbol": "ABC"}]},
    )

    payload = data_access.table_payload(panel_data, "candidates")

    assert payload["count"] == 1
    assert payload["rows"][0]["symbol"] == "ABC"


def test_ticker_payload_matches_symbol() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "candidates": [{"symbol": "ABC"}],
            "portfolio": [],
            "thesis_monitor": [{"symbol": "ABC", "needs_review": True}],
        },
    )

    payload = data_access.ticker_payload(panel_data, "abc")

    assert payload["found"] is True
    assert payload["tables"]["candidates"][0]["symbol"] == "ABC"
    assert payload["tables"]["thesis_monitor"][0]["needs_review"] is True


def test_ticker_payload_includes_quote_and_signal_context_for_deep_links() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "decision_queue": [{"symbol": "NVDA", "score": 91, "action_grade": "research"}],
            "quotes": [{"symbol": "NVDA", "price": 135.25, "change_pct": 1.4}],
            "technicals": [{"symbol": "NVDA", "technical_score": 82}],
            "opportunity_sources": [{"symbol": "NVDA", "source_key": "technicals", "title": "Technical setup"}],
        },
    )

    payload = data_access.ticker_payload(panel_data, "nvda")

    assert payload["found"] is True
    assert payload["tables"]["decision_queue"][0]["score"] == 91
    assert payload["tables"]["quotes"][0]["price"] == 135.25
    assert payload["tables"]["technicals"][0]["technical_score"] == 82
    assert payload["tables"]["opportunity_sources"][0]["source_key"] == "technicals"


def test_ticker_payload_guarantees_dossier_tab_coverage_from_read_models() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "discovered_universe": [{"symbol": "CRWV", "name": "CoreWeave", "source_counts": {"filing": 1}}],
            "universe_screen": [{"symbol": "CRWV", "watch_state": "candidate", "market_cap": 10_000_000_000, "forward_pe": 55, "roic": 9, "quality_score": 42, "value_signal": "expensive"}],
            "symbol_decision_snapshot": [{"symbol": "CRWV", "action_grade": "Watch", "freshness_status": "fresh", "decision_basis": {"summary": "AI infrastructure candidate", "source_counts": {"filing": 1}}, "invalidation": "Capacity demand slows"}],
        },
    )

    payload = data_access.ticker_payload(panel_data, "crwv")
    tables = payload["tables"]

    assert payload["found"] is True
    assert tables["quotes"][0]["source"] == "ticker_dossier_coverage"
    assert tables["fundamentals"][0]["source"] == "universe_screen"
    assert tables["source_consensus"][0]["source_name"] == "Ticker source coverage"
    assert tables["ownership_consensus"][0]["source_type"] == "coverage_gap"
    assert tables["feed_signals"][0]["source"] == "ticker_dossier_coverage"
    assert tables["thesis_monitor"][0]["needs_review"] is True


def test_new_ia_panel_scopes_are_backend_owned() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "feed_signals": [{"id": "f1", "title": "Portfolio signal"}],
            "universe_screen": [{"symbol": "NVDA", "watch_state": "watched"}],
            "source_consensus": [{"source_name": "Arco / Birdclaw"}],
            "ownership_consensus": [{"symbol": "NVDA", "holders": 2}],
            "market_context": [{"metric": "Position sizing posture"}],
            "market_valuation_reference_charts": [{"metric": "sp500_forward_pe"}],
            "market_valuation_charts": [{"symbol": "MARKET", "scope": "whole_market"}],
            "market_environment_assets": [{"symbol": "SPY", "group_name": "Market"}],
            "market_environment_model": [{"category": "Overall", "score": 55}],
        },
    )

    feed_payload = data_access.panel_snapshot_payload(panel_data, "feed")
    assert feed_payload["tables"]["feed_signals"]["count"] == 1
    assert list(feed_payload["tables"]) == ["feed_signals"]
    assert feed_payload["dashboard"] is None
    operational_tables = {
        "source_freshness",
        "source_health",
        "provider_runs",
        "source_runs",
        "source_items",
        "broker_status",
        "broker_accounts",
        "paper_orders",
        "decision_readiness",
    }
    for scope in ["feed", "today", "watchlist", "sources", "superinvestors", "market", "portfolio", "research", "filings", "calendar"]:
        payload = data_access.panel_snapshot_payload(panel_data, scope)
        assert operational_tables.isdisjoint(payload["tables"])
        assert payload["dashboard"] is None
    assert data_access.panel_snapshot_payload(panel_data, "watchlist")["tables"]["universe_screen"]["count"] == 1
    assert data_access.panel_snapshot_payload(panel_data, "sources")["tables"]["source_consensus"]["count"] == 1
    assert data_access.panel_snapshot_payload(panel_data, "superinvestors")["tables"]["ownership_consensus"]["count"] == 1
    market_tables = data_access.panel_snapshot_payload(panel_data, "market")["tables"]
    assert set(market_tables) == {
        "market_valuation_reference_charts",
        "market_environment_assets",
        "market_environment_model",
    }
    assert market_tables["market_valuation_reference_charts"]["count"] == 1
    assert market_tables["market_environment_assets"]["count"] == 1
    assert market_tables["market_environment_model"]["count"] == 1


def test_ticker_payload_excludes_health_only_operational_tables() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "decision_queue": [{"symbol": "NVDA", "score": 91}],
            "decision_readiness": [{"symbol": "NVDA", "status": "blocked"}],
            "broker_status": [{"provider": "ibkr", "status": "expected_login_required"}],
            "broker_accounts": [{"provider": "ibkr", "account_id": "demo"}],
            "paper_orders": [{"symbol": "NVDA", "status": "staged"}],
        },
    )

    payload = data_access.ticker_payload(panel_data, "nvda")

    assert "decision_readiness" not in payload["tables"]
    assert "broker_status" not in payload["tables"]
    assert "broker_accounts" not in payload["tables"]
    assert "paper_orders" not in payload["tables"]


def test_settings_payload_exposes_config_and_integration_metadata() -> None:
    config = {
        "database": {"duckdb_path": "/tmp/market.duckdb"},
        "arco": {"raw_dir": "/Volumes/agent/brain/raw/sources/arco"},
        "birdclaw": {"command": "birdclaw export"},
    }
    panel_data = data_access.PanelData(status=data_access.DataStatus(True, "ok", "test"), tables={})

    payload = data_access.settings_payload(config, panel_data)

    assert payload["status"]["ready"] is True
    assert payload["config"]["database"]["duckdb_path"] == "/tmp/market.duckdb"
    assert payload["integration"]["duckdb_path"] == "/tmp/market.duckdb"
    assert payload["integration"]["arco_raw_dir"] == "/Volumes/agent/brain/raw/sources/arco"
    assert payload["integration"]["birdclaw_command"] == "birdclaw export"


def test_save_and_delete_portfolio_position(tmp_path) -> None:
    config = {"database": {"duckdb_path": str(tmp_path / "portfolio.duckdb")}}

    saved = data_access.save_portfolio_position(
        config,
        {"symbol": "nvda", "quantity": 3, "avg_cost": 125.5, "purchase_date": "2024-01-15", "notes": "core"},
    )
    panel_data = data_access.load_panel_data(config)

    assert saved["symbol"] == "NVDA"
    assert saved["purchase_date"] == "2024-01-15"
    assert panel_data.rows("portfolio")[0]["symbol"] == "NVDA"
    assert panel_data.rows("portfolio")[0]["quantity"] == 3
    assert panel_data.rows("portfolio")[0]["purchase_date"] == "2024-01-15"
    assert panel_data.rows("portfolio")[0]["tax_lot_term"] == "long_term"
    assert panel_data.rows("discovered_universe")[0]["symbol"] == "NVDA"

    from investment_panel.core.db import db, query_rows

    with db(config["database"]["duckdb_path"]) as con:
        instruments = query_rows(con, "SELECT symbol, asset_class, category, source FROM instruments WHERE symbol = 'NVDA'")
    assert instruments == [{"symbol": "NVDA", "asset_class": "equity", "category": "owned-portfolio", "source": "portfolio"}]

    deleted = data_access.delete_portfolio_position(config, "NVDA")
    panel_data = data_access.load_panel_data(config)

    assert deleted == {"symbol": "NVDA", "deleted": True}
    assert panel_data.rows("portfolio") == []


def test_delete_config_watchlist_symbol_persists_unwatch_override(tmp_path) -> None:
    config = {
        "database": {"duckdb_path": str(tmp_path / "watchlist.duckdb")},
        "watchlist": [{"symbol": "NVDA", "name": "NVIDIA", "asset_class": "equity"}],
    }

    before = data_access.load_panel_data(config)
    assert next(row for row in before.rows("universe_screen") if row["symbol"] == "NVDA")["watch_state"] == "watched"

    deleted = data_access.delete_watchlist_symbol(config, "NVDA")
    after = data_access.load_panel_data(config)

    assert deleted == {"symbol": "NVDA", "deleted": True}
    assert next(row for row in after.rows("universe_screen") if row["symbol"] == "NVDA")["watch_state"] == "candidate"
    assert after.rows("manual_watchlist") == []

    from investment_panel.core.db import db, query_rows

    with db(config["database"]["duckdb_path"]) as con:
        overrides = query_rows(con, "SELECT symbol, watch_state FROM manual_watchlist WHERE symbol = 'NVDA'")
    assert overrides == [{"symbol": "NVDA", "watch_state": "excluded"}]


def test_delete_source_watchlist_symbol_persists_unwatch_override(tmp_path) -> None:
    config = {"database": {"duckdb_path": str(tmp_path / "source-watchlist.duckdb")}, "watchlist": []}

    from investment_panel.core.db import db, init_db, json_dumps, query_rows

    init_db(config["database"]["duckdb_path"])
    with db(config["database"]["duckdb_path"]) as con:
        con.execute(
            """
            INSERT INTO tradingview_watchlists (id, observed_at, name, color, symbol_count, symbols, source, raw)
            VALUES ('tv-ai', now(), 'AI', NULL, 1, ?, 'tradingview', '{}')
            """,
            [json_dumps(["PLTR"])],
        )

    before = data_access.load_panel_data(config)
    assert next(row for row in before.rows("universe_screen") if row["symbol"] == "PLTR")["watch_state"] == "watched"

    deleted = data_access.delete_watchlist_symbol(config, "PLTR")
    after = data_access.load_panel_data(config)

    assert deleted == {"symbol": "PLTR", "deleted": True}
    assert next(row for row in after.rows("universe_screen") if row["symbol"] == "PLTR")["watch_state"] == "candidate"

    with db(config["database"]["duckdb_path"]) as con:
        overrides = query_rows(con, "SELECT symbol, watch_state FROM manual_watchlist WHERE symbol = 'PLTR'")
    assert overrides == [{"symbol": "PLTR", "watch_state": "excluded"}]


def test_save_watchlist_crypto_alias_uses_crypto_asset_class(tmp_path) -> None:
    config = {"database": {"duckdb_path": str(tmp_path / "crypto-watchlist.duckdb")}}

    saved = data_access.save_watchlist_symbol(config, {"symbol": "btc", "asset_class": "equity"})
    panel_data = data_access.load_panel_data(config)

    assert saved["symbol"] == "BTC-USD"
    assert saved["asset_class"] == "crypto"
    assert next(row for row in panel_data.rows("discovered_universe") if row["symbol"] == "BTC-USD")["asset_class"] == "crypto"

    from investment_panel.core.db import db, query_rows

    with db(config["database"]["duckdb_path"]) as con:
        instruments = query_rows(con, "SELECT symbol, asset_class FROM instruments WHERE symbol = 'BTC-USD'")
    assert instruments == [{"symbol": "BTC-USD", "asset_class": "crypto"}]


def test_save_watchlist_symbol_rejects_malformed_ticker(tmp_path) -> None:
    config = {"database": {"duckdb_path": str(tmp_path / "bad-watchlist.duckdb")}}

    import pytest

    with pytest.raises(ValueError, match="valid ticker"):
        data_access.save_watchlist_symbol(config, {"symbol": "ABC!"})
