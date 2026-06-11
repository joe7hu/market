from pathlib import Path

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
    diagnostic_rows = data_access.ticker_data_source_rows("CRWV", tables)
    assert diagnostic_rows
    assert {row["family"] for row in diagnostic_rows} >= {"decision", "quote", "fundamentals", "source_evidence"}
    for row in diagnostic_rows:
        assert row["symbol"] == "CRWV"
        assert row["label"]
        assert row["status"]
        assert row["source_tables"]
        assert row["shared_surfaces"]
        assert row["latest_at"]
        assert row["fields_loaded"]


def test_new_ia_panel_scopes_are_backend_owned() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "feed_signals": [{"id": "f1", "title": "Portfolio signal"}],
            "universe_screen": [{"symbol": "NVDA", "watch_state": "watched"}],
            "source_ticker_rankings": [{"symbol": "NVDA", "signal_count": 2}],
            "ticker_source_signals": [{"symbol": "NVDA", "source_name": "Birdclaw primary X/Twitter"}],
            "source_items": [{"id": "tweet-1", "source_name": "Birdclaw primary X/Twitter"}],
            "source_consensus": [{"source_name": "Arco / Birdclaw"}],
            "sources": [{"source_id": "birdclaw_primary_tweets", "source_name": "Birdclaw primary X/Twitter"}],
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
    source_tables = data_access.panel_snapshot_payload(panel_data, "sources")["tables"]
    assert list(source_tables) == [
        "source_ticker_rankings",
        "ticker_source_signals",
        "source_items",
        "source_consensus",
        "feed_signals",
        "opportunity_sources",
        "theses",
        "news",
        "sources",
    ]
    assert source_tables["source_ticker_rankings"]["count"] == 1
    assert source_tables["ticker_source_signals"]["count"] == 1
    assert source_tables["source_items"]["count"] == 1
    assert source_tables["source_consensus"]["count"] == 1
    assert source_tables["sources"]["count"] == 1
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


def test_scope_loader_materializes_only_requested_tables(tmp_path) -> None:
    config = {"database": {"duckdb_path": str(tmp_path / "scoped.duckdb")}}

    panel_data = data_access.load_panel_scope_data(config, "feed")

    assert set(panel_data.tables) == {"feed_signals"}
    assert panel_data.rows("source_freshness") == []


def test_source_table_loader_requests_source_repair_without_decision_repair(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_helper(config: dict[str, object], table_names: tuple[str, ...], ensure_decision_models: bool, ensure_source_models: bool) -> dict[str, object]:
        calls.append(
            {
                "table_names": table_names,
                "ensure_decision_models": ensure_decision_models,
                "ensure_source_models": ensure_source_models,
            }
        )
        return {"ready": True, "message": "ok", "source": "test", "tables": {"source_items": []}, "metadata": {}}

    monkeypatch.setattr(data_access, "_resolve_core_helper", lambda: fake_helper)

    data_access.load_table_panel_data({"database": {"duckdb_path": "/tmp/test.duckdb"}}, "source_items")

    assert calls == [
        {
            "table_names": ("source_items",),
            "ensure_decision_models": False,
            "ensure_source_models": True,
        }
    ]


def test_scoped_panel_status_is_ready_when_requested_table_has_rows(tmp_path) -> None:
    db_path = tmp_path / "scoped-ready.duckdb"
    config = {"database": {"duckdb_path": str(db_path)}}
    data_access.load_panel_data(config)
    from investment_panel.core.db import db

    with db(db_path) as con:
        con.execute(
            """
            INSERT INTO birdclaw_theses
            VALUES ('thesis-1', 'NVDA', 'tester', '2026-06-01T12:00:00Z', 'NVDA thesis', '{}', '{}', 'https://example.com')
            """
        )
        from investment_panel.core.decision import refresh_decision_read_models

        refresh_decision_read_models(con, [])

    panel_data = data_access.load_panel_scope_data(config, "feed")

    assert panel_data.status.ready is True
    assert panel_data.rows("feed_signals")


def test_panel_contract_lists_scope_and_ticker_tables() -> None:
    contract = data_access.panel_contract_payload()

    assert contract["scopes"]["feed"] == ["feed_signals"]
    assert "source_freshness" not in contract["scopes"]["watchlist"]
    assert "broker_positions" in contract["scopes"]["health"]
    assert "universe_screen" in contract["watchlist_section_tables"]
    assert "decision_queue" in contract["ticker_tables"]
    assert "ticker_data_sources" not in contract["ticker_tables"]
    assert contract["endpoint_tables"]["feed"] == "feed_signals"
    assert contract["endpoint_tables"]["watchlist/symbols"] == "manual_watchlist"


def test_watchlist_section_scopes_split_rows_and_support_tables() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "universe_screen": [
                {"symbol": "NVDA", "watch_state": "watched"},
                {"symbol": "AMD", "watch_state": "candidate"},
            ],
            "quotes": [{"symbol": "NVDA", "price": 100}, {"symbol": "AMD", "price": 50}],
            "fundamentals": [{"symbol": "NVDA", "metrics": {"revenue_growth": 0.12}}, {"symbol": "AMD", "metrics": {"revenue_growth": 0.2}}],
            "technicals": [{"symbol": "NVDA", "chart_1y": [1, 2]}, {"symbol": "AMD", "chart_1y": [2, 3]}],
            "valuations": [{"symbol": "NVDA", "upside_pct": 10}, {"symbol": "AMD", "upside_pct": 20}],
        },
    )

    watched = data_access.panel_snapshot_payload(panel_data, "watchlist-watched")
    unwatched = data_access.panel_snapshot_payload(panel_data, "watchlist-unwatched")

    assert watched["tables"]["watchlist_watched"]["rows"] == [{"symbol": "NVDA", "watch_state": "watched"}]
    assert unwatched["tables"]["watchlist_unwatched"]["rows"] == [{"symbol": "AMD", "watch_state": "candidate"}]
    assert watched["tables"]["watchlist_watched_fundamentals"]["rows"][0]["symbol"] == "NVDA"
    assert unwatched["tables"]["watchlist_unwatched_fundamentals"]["rows"][0]["symbol"] == "AMD"
    assert watched["tables"]["watchlist_watched_technicals"]["rows"][0]["symbol"] == "NVDA"
    assert unwatched["tables"]["watchlist_unwatched_technicals"]["rows"][0]["symbol"] == "AMD"


def test_watchlist_unwatched_scope_pages_rows_and_keeps_total_count() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "universe_screen": [
                {"symbol": "NVDA", "watch_state": "watched"},
                {"symbol": "AMD", "watch_state": "candidate"},
                {"symbol": "MSFT", "watch_state": "candidate"},
                {"symbol": "TSLA", "watch_state": "candidate"},
            ],
            "quotes": [{"symbol": "AMD", "price": 50}, {"symbol": "MSFT", "price": 100}, {"symbol": "TSLA", "price": 200}],
            "technicals": [{"symbol": "AMD", "chart_1y": [1, 2]}, {"symbol": "MSFT", "chart_1y": [2, 3]}, {"symbol": "TSLA", "chart_1y": [3, 4]}],
        },
    )

    page = data_access.panel_snapshot_payload(panel_data, "watchlist-unwatched", offset=1, limit=1)

    assert page["tables"]["watchlist_unwatched"]["count"] == 3
    assert page["tables"]["watchlist_unwatched"]["offset"] == 1
    assert page["tables"]["watchlist_unwatched"]["limit"] == 1
    assert page["tables"]["watchlist_unwatched"]["rows"] == [{"symbol": "MSFT", "watch_state": "candidate"}]
    assert page["tables"]["watchlist_unwatched_quotes"]["rows"] == [{"symbol": "MSFT", "price": 100}]


def test_watchlist_watched_scope_includes_unwatched_count_without_rows() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "universe_screen": [
                {"symbol": "NVDA", "watch_state": "watched"},
                {"symbol": "AMD", "watch_state": "candidate"},
            ],
        },
    )

    watched = data_access.panel_snapshot_payload(panel_data, "watchlist-watched")

    assert watched["tables"]["watchlist_watched"]["count"] == 1
    assert watched["tables"]["watchlist_unwatched"]["count"] == 1
    assert watched["tables"]["watchlist_unwatched"]["rows"] == []


def test_watchlist_section_includes_manual_symbol_before_read_model_refresh() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "universe_screen": [],
            "manual_watchlist": [{"symbol": "IBM", "name": "IBM", "asset_class": "equity", "watch_state": "watched"}],
        },
    )

    watched = data_access.panel_snapshot_payload(panel_data, "watchlist-watched")

    assert watched["tables"]["watchlist_watched"]["count"] == 1
    assert watched["tables"]["watchlist_watched"]["rows"][0]["symbol"] == "IBM"
    assert watched["tables"]["watchlist_watched"]["rows"][0]["watch_state"] == "watched"


def test_watchlist_section_manual_exclusion_removes_symbol_from_sections() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "universe_screen": [{"symbol": "AAPL", "watch_state": "watched"}],
            "manual_watchlist": [{"symbol": "AAPL", "name": "Apple", "asset_class": "equity", "watch_state": "excluded"}],
        },
    )

    watched = data_access.panel_snapshot_payload(panel_data, "watchlist-watched")
    unwatched = data_access.panel_snapshot_payload(panel_data, "watchlist-unwatched")

    assert watched["tables"]["watchlist_watched"]["rows"] == []
    assert watched["tables"]["watchlist_unwatched"]["count"] == 0
    assert unwatched["tables"]["watchlist_unwatched"]["rows"] == []


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
    assert "ticker_data_sources" not in payload["tables"]


def test_ticker_page_does_not_render_operational_data_coverage_panel() -> None:
    source = Path("frontend/src/views/ticker.tsx").read_text(encoding="utf-8")

    assert "Data Source Coverage" not in source
    assert "Shared Surfaces" not in source
    assert "Loaded Fields" not in source
    assert "Decision Snapshot" not in source


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


def test_status_payload_exposes_option_agent_runtime_metadata() -> None:
    config = {
        "agents": {
            "option_thesis": {"enabled": True, "command": "market-codex-option-thesis-agent", "limit": 20, "timeout_seconds": 180},
        },
    }
    panel_data = data_access.PanelData(status=data_access.DataStatus(True, "ok", "test"), tables={})
    panel_data.metadata.update(data_access._runtime_metadata(config))

    payload = data_access.status_payload(panel_data)

    option_thesis = payload["metadata"]["agents"]["option_thesis"]
    assert option_thesis["active"] is True
    assert option_thesis["configured"] is True
    assert option_thesis["status"] == "active"
    assert option_thesis["limit"] == 20
    assert option_thesis["timeout_seconds"] == 180
    assert option_thesis["request_cap"] == 12
    assert option_thesis["queue_policy"] == "current_top_ranked_candidates_only"


def test_status_payload_reports_unconfigured_option_agent_paused() -> None:
    config = {"agents": {"option_thesis": {"enabled": False, "command": ""}}}
    panel_data = data_access.PanelData(status=data_access.DataStatus(True, "ok", "test"), tables={})
    panel_data.metadata.update(data_access._runtime_metadata(config))

    option_thesis = data_access.status_payload(panel_data)["metadata"]["agents"]["option_thesis"]

    assert option_thesis["active"] is False
    assert option_thesis["configured"] is False
    assert option_thesis["status"] == "paused"


def test_fastapi_config_reports_runtime_duckdb_override(tmp_path, monkeypatch) -> None:
    runtime_path = tmp_path / "runtime.duckdb"
    monkeypatch.setenv("MARKET_DUCKDB_PATH", str(runtime_path))

    config = data_access.load_config(tmp_path / "missing.yaml")

    assert config["database"]["duckdb_path"] == str(runtime_path)
    assert config["runtime_overrides"]["MARKET_DUCKDB_PATH"] == str(runtime_path)


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


def test_populate_watchlist_symbol_data_runs_targeted_refresh(tmp_path, monkeypatch) -> None:
    import pandas as pd

    config = {
        "database": {"duckdb_path": str(tmp_path / "populate-watchlist.duckdb")},
        "market_data": {"lookback_days": 30, "mode": "online"},
        "data_sources": {"yfinance": {"enabled": False}},
        "scoring": {"weights": {"technical": 1.0}},
        "watchlist": [],
    }
    data_access.save_watchlist_symbol(config, {"symbol": "XYZ"})

    def fetch_prices(symbol: str, lookback_days: int, mode: str) -> pd.DataFrame:
        assert symbol == "XYZ"
        assert lookback_days == 30
        assert mode == "online"
        return pd.DataFrame(
            [
                {"symbol": "XYZ", "date": "2026-01-01", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100, "source": "test"},
                {"symbol": "XYZ", "date": "2026-01-02", "open": 10, "high": 12, "low": 10, "close": 12, "volume": 120, "source": "test"},
            ]
        )

    monkeypatch.setattr("investment_panel.core.prices.fetch_prices", fetch_prices)
    monkeypatch.setattr("investment_panel.core.prices.upsert_prices", lambda con, frame: len(frame))
    monkeypatch.setattr("investment_panel.core.technicals.compute_and_store", lambda con, symbol: symbol == "XYZ")
    monkeypatch.setattr("investment_panel.analysis.valuation.store_valuation_models", lambda con, symbols: len(symbols) * 2)
    monkeypatch.setattr("investment_panel.core.scoring.score_and_store", lambda con, symbols, weights: [{"symbol": symbol} for symbol in symbols])
    monkeypatch.setattr("investment_panel.core.decision.refresh_decision_read_models", lambda con, watchlist: {"status": "decision_models_refreshed"})

    result = data_access.populate_watchlist_symbol_data(config, "XYZ", "equity")

    assert result["status"] == "ok"
    assert result["price_rows"] == 2
    assert result["technical_rows"] == 1
    assert result["valuation_rows"] == 2
    assert result["scored"] == 1
    assert result["decision_models"] == {"status": "decision_models_refreshed"}
    assert result["errors"] == {}


def test_save_watchlist_symbol_rejects_malformed_ticker(tmp_path) -> None:
    config = {"database": {"duckdb_path": str(tmp_path / "bad-watchlist.duckdb")}}

    import pytest

    with pytest.raises(ValueError, match="valid ticker"):
        data_access.save_watchlist_symbol(config, {"symbol": "ABC!"})
