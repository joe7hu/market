from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from app import data_access
from investment_panel.core.panel import market_freshness
from investment_panel.core.db import db, init_db


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
            "candidates": [{"symbol": "ABC", "name": "Alpha"}],
            "portfolio": [],
            "thesis_monitor": [{"symbol": "ABC", "needs_review": True, "thesis": "watch"}],
        },
    )

    payload = data_access.ticker_payload(panel_data, "abc")
    dossier = payload["dossier"]

    assert payload["symbol"] == "ABC"
    assert payload["found"] is True
    assert dossier["identity"]["name"] == "Alpha"
    assert dossier["thesis"]["state"]["needs_review"] is True
    assert dossier["thesis"]["coverage"]["status"] == "live"


def test_ticker_payload_organizes_sections_for_deep_links() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "decision_queue": [{"symbol": "NVDA", "score": 91, "action_grade": "research"}],
            "quotes": [{"symbol": "NVDA", "price": 135.25, "change_pct": 1.4, "observed_at": "2026-06-12T20:00:00"}],
            "technicals": [{"symbol": "NVDA", "technical_score": 82, "ma50": 130.0, "date": "2026-06-11"}],
            "liquidity": [{"symbol": "NVDA", "grade": "very_high", "avg_dollar_volume": 3.3e10}],
            "disclosures": [{"symbol": "NVDA", "filer_name": "Pelosi", "action": "SELL", "amount": "$1M", "filed_date": "2026-01-23"}],
        },
    )

    payload = data_access.ticker_payload(panel_data, "nvda")
    dossier = payload["dossier"]

    assert payload["found"] is True
    assert dossier["quote"]["price"] == 135.25
    assert dossier["quote"]["coverage"]["status"] == "live"
    assert dossier["technicals"]["momentum"]["technical_score"] == 82
    assert dossier["technicals"]["liquidity"]["grade"] == "very_high"
    assert dossier["ownership"]["filings"][0]["filer_name"] == "Pelosi"
    assert dossier["ownership"]["filings"][0]["action"] == "SELL"


def test_ticker_payload_reports_missing_coverage_without_fabricating_rows() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "discovered_universe": [{"symbol": "CRWV", "name": "CoreWeave", "source_counts": {"filing": 1}}],
            "universe_screen": [{"symbol": "CRWV", "name": "CoreWeave", "watch_state": "candidate", "market_cap": 10_000_000_000, "forward_pe": 55, "roic": 9, "quality_score": 42, "value_signal": "expensive"}],
            "symbol_decision_snapshot": [{"symbol": "CRWV", "action_grade": "Watch", "freshness_status": "fresh", "decision_basis": {"summary": "AI infrastructure candidate", "source_counts": {"filing": 1}}, "invalidation": "Capacity demand slows"}],
        },
    )

    payload = data_access.ticker_payload(panel_data, "crwv")
    dossier = payload["dossier"]
    coverage = dossier["coverage"]

    assert payload["found"] is True
    assert dossier["identity"]["name"] == "CoreWeave"
    # Decision is live from the decision row; fundamentals is only screen-data
    # (universe_screen) with no authoritative sec_companyfacts row, so it is
    # reported "partial" (present, not fully live) rather than overstated.
    assert dossier["fundamentals"]["market"]["forward_pe"] == 55
    assert coverage["families"]["fundamentals"]["status"] == "partial"
    assert "fundamentals" in coverage["present"]
    assert "fundamentals" not in coverage["live"]
    assert "decision" in coverage["live"]
    # ...but families with no loaded rows are reported missing, not fabricated.
    assert dossier["ownership"]["coverage"]["status"] == "missing"
    assert dossier["ownership"]["filings"] == []
    assert dossier["quote"]["coverage"]["status"] == "missing"
    assert {"ownership", "quote", "options"} <= set(coverage["missing"])


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


def test_market_panel_status_reports_stale_broad_market_inputs(tmp_path) -> None:
    db_path = tmp_path / "market-stale.duckdb"
    stale_date = date.today() - timedelta(days=3)
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            """
            INSERT INTO market_valuation_metric_points
            (metric, as_of, label, value, suffix, higher_is_better, source, source_url)
            VALUES ('sp500_forward_pe', ?, 'S&P 500 Forward P/E', 21.5, 'x', false, 'test', 'https://example.com')
            """,
            [stale_date],
        )
        con.execute(
            """
            INSERT INTO market_environment_asset_snapshots
            (symbol, as_of, group_name, name, price, return_1d, return_ytd, return_1w,
             return_1m, return_1y, pct_from_52w_high, sma_10_up, sma_20_up, sma_50_up,
             sma_200_up, sma_20_gt_50, sma_50_gt_200, range_ratio_52w, color, source, raw)
            VALUES ('SPY', ?, 'Market', 'S&P 500 ETF', 600, 0.1, 5, 1, 2, 12, 1, true, true, true, true, true, true, 90, 'green', 'test', '{}')
            """,
            [stale_date],
        )

    panel_data = data_access.load_market_panel_data({"database": {"duckdb_path": str(db_path)}})

    assert panel_data.status.ready is True
    assert panel_data.status.source == "duckdb-stale"
    assert panel_data.metadata["market_freshness"]["status"] == "stale"
    assert panel_data.metadata["market_freshness"]["checks"]["asset_matrix"]["latest_date"] == stale_date.isoformat()
    assert panel_data.metadata["market_freshness"]["checks"]["valuation_reference"]["series"]["sp500_forward_pe"]["status"] == "stale"


def test_market_freshness_distinguishes_off_market_hours_from_stale() -> None:
    tables = {
        "market_valuation_reference_charts": [{"metric": "sp500_forward_pe", "latest_date": "2026-06-12"}],
        "market_environment_assets": [{"symbol": "SPY", "as_of": "2026-06-12"}],
    }

    freshness = market_freshness(tables, now=datetime(2026, 6, 15, 12, 0, tzinfo=UTC))

    assert freshness["status"] == "off_market_hours"
    assert freshness["market_phase"] == "premarket"
    assert freshness["expected_date"] == "2026-06-12"
    assert freshness["checks"]["valuation_reference"]["series"]["sp500_forward_pe"]["status"] == "off_market_hours"


def test_market_freshness_marks_missed_completed_session_stale() -> None:
    tables = {
        "market_valuation_reference_charts": [{"metric": "equity_risk_premium", "latest_date": "2026-06-12"}],
        "market_environment_assets": [{"symbol": "SPY", "as_of": "2026-06-14"}],
    }

    freshness = market_freshness(tables, now=datetime(2026, 6, 16, 12, 0, tzinfo=UTC))

    assert freshness["status"] == "stale"
    assert freshness["market_phase"] == "premarket"
    assert freshness["expected_date"] == "2026-06-15"
    assert freshness["checks"]["valuation_reference"]["series"]["equity_risk_premium"]["status"] == "stale"
    assert freshness["checks"]["asset_matrix"]["status"] == "stale"


def test_scope_loader_materializes_only_requested_tables(tmp_path) -> None:
    config = {"database": {"duckdb_path": str(tmp_path / "scoped.duckdb")}}

    panel_data = data_access.load_panel_scope_data(config, "feed")

    assert set(panel_data.tables) == {"feed_signals"}
    assert panel_data.rows("source_freshness") == []


def test_source_table_loader_uses_read_only_panel_load(monkeypatch) -> None:
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

    monkeypatch.setattr(data_access.loaders, "core_load_panel_data", fake_helper)

    data_access.load_table_panel_data({"database": {"duckdb_path": "/tmp/test.duckdb"}}, "source_items")

    assert calls == [
        {
            "table_names": ("source_items",),
            "ensure_decision_models": False,
            "ensure_source_models": False,
        }
    ]


def test_default_panel_loader_preserves_full_load_sentinel(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_helper(
        config: dict[str, object],
        table_names: tuple[str, ...] | None,
        ensure_decision_models: bool | None,
        ensure_source_models: bool | None,
    ) -> dict[str, object]:
        calls.append(
            {
                "table_names": table_names,
                "ensure_decision_models": ensure_decision_models,
                "ensure_source_models": ensure_source_models,
            }
        )
        return {"ready": True, "message": "ok", "source": "test", "tables": {"signals": [{"symbol": "NVDA"}]}, "metadata": {}}

    monkeypatch.setattr(data_access.loaders, "core_load_panel_data", fake_helper)

    panel_data = data_access.load_panel_data({"database": {"duckdb_path": "/tmp/test.duckdb"}})

    assert panel_data.status.ready is True
    assert calls == [
        {
            "table_names": None,
            "ensure_decision_models": None,
            "ensure_source_models": None,
        }
    ]


def test_empty_settings_scope_does_not_touch_missing_database(tmp_path) -> None:
    db_path = tmp_path / "missing-settings.duckdb"

    panel_data = data_access.load_panel_scope_data({"database": {"duckdb_path": str(db_path)}}, "settings")

    assert panel_data.status.ready is True
    assert panel_data.status.source == "duckdb"
    assert panel_data.tables == {}
    assert not db_path.exists()


def test_market_panel_loader_handles_unmigrated_existing_database(tmp_path) -> None:
    db_path = tmp_path / "unmigrated.duckdb"
    duckdb.connect(str(db_path)).close()

    panel_data = data_access.load_market_panel_data({"database": {"duckdb_path": str(db_path)}})

    assert panel_data.status.ready is True
    assert panel_data.status.source != "core-error"
    assert panel_data.rows("market_valuation_reference_charts") == []
    assert panel_data.rows("market_environment_assets") == []
    assert panel_data.rows("market_environment_model")


def test_pure_scoped_read_migrates_stale_database_before_reading(tmp_path) -> None:
    db_path = tmp_path / "stale.duckdb"
    with duckdb.connect(str(db_path)) as con:
        con.execute("CREATE TABLE source_health (source TEXT PRIMARY KEY, checked_at TIMESTAMP, status TEXT, detail TEXT)")

    panel_data = data_access.load_table_panel_data({"database": {"duckdb_path": str(db_path)}}, "source_health")

    assert panel_data.status.source != "core-error"
    assert panel_data.rows("source_health") == []
    with duckdb.connect(str(db_path), read_only=True) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info('source_health')").fetchall()}
    assert "source_url" in columns


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
    assert "sources" in contract["scopes"]["health"]
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

    # The dossier is section-organized; there is no raw table bag to leak
    # operational/health tables into.
    assert "tables" not in payload
    assert set(payload["dossier"]) == {
        "identity", "quote", "decision", "fundamentals", "estimates",
        "technicals", "options", "ownership", "sources", "thesis",
        "portfolio", "coverage",
    }
    serialized = repr(payload["dossier"])
    for operational in ("decision_readiness", "broker_status", "broker_accounts", "paper_orders", "ticker_data_sources"):
        assert operational not in serialized


def test_ticker_page_does_not_render_operational_data_coverage_panel() -> None:
    ticker_dir = Path("frontend/src/views/ticker")
    source = "\n".join(path.read_text(encoding="utf-8") for path in sorted(ticker_dir.glob("*.ts*")))

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


def test_save_thesis_records_content_and_clears_stale(tmp_path) -> None:
    config = {"database": {"duckdb_path": str(tmp_path / "thesis.duckdb")}, "watchlist": [{"symbol": "NVDA"}]}

    saved = data_access.save_thesis(
        config,
        "nvda",
        {
            "thesis": "AI accelerator leader with durable datacenter demand.",
            "why": "Owned for AI infrastructure exposure.",
            "invalidation": "Below $95 the setup breaks.",
            "invalidation_price": 95,
            "evidence_links": ["https://example.com/nvda"],
        },
    )

    assert saved["symbol"] == "NVDA"
    assert saved["thesis"]["core_thesis"].startswith("AI accelerator")
    assert saved["thesis"]["last_reviewed"]

    from investment_panel.core.db import db
    from investment_panel.core.thesis_monitor import thesis_monitor_rows

    with db(config["database"]["duckdb_path"]) as con:
        rows = thesis_monitor_rows(con, [{"symbol": "NVDA"}])
    nvda = next(row for row in rows if row["symbol"] == "NVDA")
    assert nvda["source"] == "theses"
    assert nvda["stale_thesis"] is False
    assert nvda.get("needs_review", False) is False
    assert nvda["invalidation_price"] == 95


def test_save_thesis_requires_thesis_text(tmp_path) -> None:
    config = {"database": {"duckdb_path": str(tmp_path / "thesis-empty.duckdb")}}
    with pytest.raises(ValueError):
        data_access.save_thesis(config, "NVDA", {"thesis": "   "})


def test_mark_thesis_reviewed_stamps_review_date(tmp_path) -> None:
    config = {"database": {"duckdb_path": str(tmp_path / "review.duckdb")}, "watchlist": [{"symbol": "MU"}]}

    data_access.save_thesis(config, "MU", {"thesis": "Memory upcycle.", "invalidation": "below $80"})
    reviewed = data_access.mark_thesis_reviewed(config, "mu")

    assert reviewed["symbol"] == "MU"
    assert reviewed["last_reviewed"]


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
