from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.data_access import loaders as loaders_owner
from app.data_access import mutations as mutations_owner
from app.data_access import payloads as payloads_owner
from app.data_access import settings as settings_owner
from app.data_access.types import DataStatus, PanelData
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.portfolio_ledger import record_portfolio_transaction
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.thesis import thesis_history, thesis_monitor_rows
from investment_panel.database.user_state import portfolio_rows, watchlist_rows
from investment_panel.core.panel import panel_contract_payload
from investment_panel.core.config import load_config
from conftest import typed_config


def test_unavailable_postgresql_returns_explicit_status() -> None:
    panel_data = loaders_owner.load_panel_data(typed_config("postgresql://127.0.0.1:1/missing"))

    assert panel_data.status.ready is False
    assert panel_data.status.source == "postgresql-error"
    assert "PostgreSQL read models unavailable" in panel_data.status.message
    assert panel_data.rows("candidates") == []


def test_postgresql_technicals_model_is_supported_when_empty(migrated_postgres_dsn: str) -> None:
    panel_data = loaders_owner.load_table_panel_data(
        typed_config(migrated_postgres_dsn), "technicals"
    )

    assert panel_data.status.ready is True
    assert panel_data.status.source == "postgresql"
    assert panel_data.metadata["unavailable_models"] == []
    assert panel_data.rows("technicals") == []


def test_complete_contract_has_no_unavailable_postgresql_models(migrated_postgres_dsn: str) -> None:
    panel_data = loaders_owner.load_panel_data(typed_config(migrated_postgres_dsn))

    assert panel_data.status.ready is True
    assert panel_data.metadata["unavailable_models"] == []


def test_load_config_honors_market_database_url_override(tmp_path, monkeypatch) -> None:
    url = "postgresql://localhost/market-test"
    monkeypatch.setenv("MARKET_DATABASE_URL", url)

    config = load_config(tmp_path / "missing-config.yaml")

    assert config.database.url == url


def test_table_payload_normalizes_rows() -> None:
    panel_data = PanelData(
        status=DataStatus(True, "ok", "test"),
        tables={"candidates": [{"symbol": "ABC"}]},
    )

    payload = payloads_owner.table_payload(panel_data, "candidates")

    assert payload["count"] == 1
    assert payload["rows"][0]["symbol"] == "ABC"


def test_ticker_payload_matches_symbol() -> None:
    panel_data = PanelData(
        status=DataStatus(True, "ok", "test"),
        tables={
            "candidates": [{"symbol": "ABC", "name": "Alpha"}],
            "portfolio": [],
            "thesis_monitor": [{
                "symbol": "ABC", "needs_review": True, "thesis": "watch",
                "source_names": ["Wire A", "Wire B"], "source_count": 2,
                "source_evidence_count": 3, "evidence_newer_than_review": True,
                "latest_source_evidence_at": "2026-07-13T12:00:00Z",
                "source_evidence": [{"source_name": "Wire A", "title": "ABC update"}],
            }],
        },
    )

    payload = payloads_owner.ticker_payload(panel_data, "abc")
    dossier = payload["dossier"]

    assert payload["symbol"] == "ABC"
    assert payload["found"] is True
    assert dossier["identity"]["name"] == "Alpha"
    assert dossier["thesis"]["state"]["needs_review"] is True
    assert dossier["thesis"]["state"]["source_names"] == ["Wire A", "Wire B"]
    assert dossier["thesis"]["state"]["source_evidence_count"] == 3
    assert dossier["thesis"]["state"]["source_evidence"][0]["title"] == "ABC update"
    assert dossier["thesis"]["coverage"]["status"] == "live"


def test_ticker_payload_prefers_persisted_market_identity() -> None:
    panel_data = PanelData(
        status=DataStatus(True, "ok", "test"),
        tables={
            "universe_screen": [{"symbol": "BFLY", "name": "Butterfly Network", "asset_class": "equity"}],
            "instrument_market_identity": [{"symbol": "BFLY", "primary_exchange": "NYSE", "tradingview_symbol": "NYSE:BFLY"}],
        },
    )

    payload = payloads_owner.ticker_payload(panel_data, "bfly")

    assert payload["dossier"]["identity"]["exchange"] == "NYSE"
    assert payload["dossier"]["identity"]["tradingview_symbol"] == "NYSE:BFLY"


def test_ticker_payload_does_not_guess_nasdaq_without_exchange_data() -> None:
    panel_data = PanelData(
        status=DataStatus(True, "ok", "test"),
        tables={"universe_screen": [{"symbol": "BFLY", "name": "Butterfly Network", "asset_class": "equity"}]},
    )

    payload = payloads_owner.ticker_payload(panel_data, "bfly")

    assert payload["dossier"]["identity"]["tradingview_symbol"] == "BFLY"


def test_ticker_payload_organizes_sections_for_deep_links() -> None:
    panel_data = PanelData(
        status=DataStatus(True, "ok", "test"),
        tables={
            "decision_queue": [{"symbol": "NVDA", "score": 91, "action_grade": "research"}],
            "quotes": [{"symbol": "NVDA", "price": 135.25, "change_pct": 1.4, "observed_at": "2026-06-12T20:00:00"}],
            "technicals": [{"symbol": "NVDA", "technical_score": 82, "ma50": 130.0, "date": "2026-06-11"}],
            "liquidity": [{"symbol": "NVDA", "grade": "very_high", "avg_dollar_volume": 3.3e10}],
            "disclosures": [{"symbol": "NVDA", "filer_name": "Pelosi", "action": "SELL", "amount": "$1M", "filed_date": "2026-01-23"}],
        },
    )

    payload = payloads_owner.ticker_payload(panel_data, "nvda")
    dossier = payload["dossier"]

    assert payload["found"] is True
    assert dossier["quote"]["price"] == 135.25
    assert dossier["quote"]["coverage"]["status"] == "live"
    assert dossier["technicals"]["momentum"]["technical_score"] == 82
    assert dossier["technicals"]["liquidity"]["grade"] == "very_high"
    assert dossier["ownership"]["filings"][0]["filer_name"] == "Pelosi"
    assert dossier["ownership"]["filings"][0]["action"] == "SELL"


def test_ticker_payload_reports_missing_coverage_without_fabricating_rows() -> None:
    panel_data = PanelData(
        status=DataStatus(True, "ok", "test"),
        tables={
            "discovered_universe": [{"symbol": "CRWV", "name": "CoreWeave", "source_counts": {"filing": 1}}],
            "universe_screen": [{"symbol": "CRWV", "name": "CoreWeave", "watch_state": "candidate", "market_cap": 10_000_000_000, "forward_pe": 55, "roic": 9, "quality_score": 42, "value_signal": "expensive"}],
            "symbol_decision_snapshot": [{"symbol": "CRWV", "action_grade": "Watch", "freshness_status": "fresh", "decision_basis": {"summary": "AI infrastructure candidate", "source_counts": {"filing": 1}}, "invalidation": "Capacity demand slows"}],
        },
    )

    payload = payloads_owner.ticker_payload(panel_data, "crwv")
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
    panel_data = PanelData(
        status=DataStatus(True, "ok", "test"),
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

    feed_payload = payloads_owner.panel_snapshot_payload(panel_data, "feed")
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
        payload = payloads_owner.panel_snapshot_payload(panel_data, scope)
        assert operational_tables.isdisjoint(payload["tables"])
        assert payload["dashboard"] is None
    assert payloads_owner.panel_snapshot_payload(panel_data, "watchlist")["tables"]["universe_screen"]["count"] == 1
    source_tables = payloads_owner.panel_snapshot_payload(panel_data, "sources")["tables"]
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
    assert payloads_owner.panel_snapshot_payload(panel_data, "superinvestors")["tables"]["ownership_consensus"]["count"] == 1
    market_tables = payloads_owner.panel_snapshot_payload(panel_data, "market")["tables"]
    assert set(market_tables) == {
        "market_state_snapshot",
        "coverage_matrix",
        "market_valuation_reference_charts",
        "market_environment_assets",
        "market_environment_model",
    }
    assert market_tables["market_valuation_reference_charts"]["count"] == 1
    assert market_tables["market_environment_assets"]["count"] == 1
    assert market_tables["market_environment_model"]["count"] == 1


def test_today_scope_contains_only_canonical_ticker_actions_and_ownership() -> None:
    panel_data = PanelData(
        status=DataStatus(True, "ok", "test"),
        tables={
            "portfolio": [{"symbol": "TSLA", "market_value": 100}],
            "ticker_decisions": [{"ticker": "TSLA", "capital_action": {"action": "HOLD"}}],
            "daily_brief": [{"category": "catalysts", "symbol": "TSLA"}],
            "option_radar_opportunity": [{"decision_id": "legacy", "symbol": "TSLA"}],
        },
    )

    tables = payloads_owner.panel_snapshot_payload(panel_data, "today")["tables"]

    assert list(tables) == [
        "ticker_decisions",
        "opportunity_rank",
        "trade_plan",
        "portfolio",
        "preopen_daily_brief",
        "daily_brief",
        "portfolio_risk_cards",
        "feed_signals",
    ]
    assert tables["ticker_decisions"]["count"] == 1
    assert tables["portfolio"]["count"] == 1


def test_scope_loader_materializes_only_requested_tables(migrated_postgres_dsn: str) -> None:
    config = typed_config(migrated_postgres_dsn)

    panel_data = loaders_owner.load_panel_scope_data(config, "feed")

    assert set(panel_data.tables) == {"feed_signals"}
    assert panel_data.rows("source_freshness") == []


def test_source_table_loader_uses_requested_postgresql_model(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_helper(config: dict[str, object], table_names: tuple[str, ...]):
        calls.append(table_names)
        return {"source_items": []}, {"database": "postgresql"}

    monkeypatch.setattr(loaders_owner, "load_postgres_tables", fake_helper)

    loaders_owner.load_table_panel_data(typed_config("postgresql:///test"), "source_items")

    assert calls == [("source_items",)]


def test_daily_research_loader_bounds_detail_to_active_seed_symbols(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_helper(config: dict[str, object], table_names: tuple[str, ...], **kwargs):
        calls.append({"table_names": table_names, **kwargs})
        if "portfolio" in table_names:
            return {
                "portfolio": [{"symbol": "MSFT"}],
                "manual_watchlist": [{"symbol": "ETH-USD", "watch_state": "watched"}],
                "universe_screen": [
                    {"symbol": "NVDA", "watch_state": "watched"},
                    {"symbol": "NOISE", "watch_state": "unwatched"},
                ],
                "option_radar_opportunity": [{"ticker": "AAOI"}],
            }, {"database": "postgresql", "available_model_count": 4, "unavailable_models": []}
        return {name: [] for name in table_names}, {"database": "postgresql", "available_model_count": len(table_names), "unavailable_models": []}

    monkeypatch.setattr(loaders_owner, "load_postgres_tables", fake_helper)

    panel = loaders_owner.load_daily_research_panel_data(typed_config("postgresql:///test"))

    assert panel.status.ready is True
    assert {"AAOI", "ETH-USD", "MSFT", "NVDA", "SPY", "QQQ", "TLT", "BTC-USD"} <= calls[1]["query_symbol_filter"]
    assert "NOISE" not in calls[1]["query_symbol_filter"]
    assert calls[1]["query_row_limits"]
    assert "quotes" not in calls[1]["table_names"]
    assert calls[1]["portfolio_summary_include_performance"] is False


def test_portfolio_scope_bounds_quotes_to_current_positions(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_helper(config: dict[str, object], table_names: tuple[str, ...], **kwargs):
        calls.append({"table_names": table_names, **kwargs})
        if table_names == ("portfolio",):
            return {
                "portfolio": [{"symbol": "TSLA"}, {"ticker": "MSFT"}],
            }, {"database": "postgresql", "available_model_count": 1, "unavailable_models": []}
        return {name: [] for name in table_names}, {"database": "postgresql", "available_model_count": len(table_names), "unavailable_models": []}

    monkeypatch.setattr(loaders_owner, "load_postgres_tables", fake_helper)

    panel = loaders_owner.load_panel_scope_data(typed_config("postgresql:///test"), "portfolio")

    assert panel.status.ready is True
    assert panel.metadata["portfolio_bounded"] is True
    assert panel.metadata["portfolio_symbol_count"] == 2
    assert calls[0]["table_names"] == ("portfolio",)
    assert "portfolio" not in calls[1]["table_names"]
    assert calls[1]["query_symbol_filter"] == {"TSLA", "MSFT"}
    assert calls[1]["query_row_limits"] == {"quotes": 24}


def test_panel_loader_preserves_explicit_empty_symbol_filter(monkeypatch) -> None:
    received: dict[str, object] = {}

    def fake_helper(config: dict[str, object], table_names: tuple[str, ...], **kwargs):
        received.update(kwargs)
        return {name: [] for name in table_names}, {"database": "postgresql", "available_model_count": len(table_names), "unavailable_models": []}

    monkeypatch.setattr(loaders_owner, "load_postgres_tables", fake_helper)

    loaders_owner.load_panel_data(
        typed_config("postgresql:///test"),
        table_names=("fundamentals",),
        query_symbol_filter=set(),
    )

    assert received["query_symbol_filter"] == set()


def test_default_panel_loader_requests_complete_contract(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_helper(config: dict[str, object], table_names: tuple[str, ...]):
        calls.append(table_names)
        return {name: [] for name in table_names}, {"database": "postgresql"}

    monkeypatch.setattr(loaders_owner, "load_postgres_tables", fake_helper)

    panel_data = loaders_owner.load_panel_data(typed_config("postgresql:///test"))

    assert panel_data.status.ready is True
    assert len(calls) == 1
    assert "signals" in calls[0]
    assert "option_radar_opportunity" in calls[0]


def test_empty_settings_scope_does_not_touch_missing_database() -> None:
    panel_data = loaders_owner.load_panel_scope_data(typed_config("postgresql://127.0.0.1:1/missing"), "settings")

    assert panel_data.status.ready is True
    assert panel_data.status.source == "postgresql"
    assert panel_data.tables == {}


def test_market_panel_loader_handles_empty_postgresql(migrated_postgres_dsn: str) -> None:
    panel_data = loaders_owner.load_market_panel_data(typed_config(migrated_postgres_dsn))

    assert panel_data.status.ready is True
    assert panel_data.status.source == "postgresql"
    assert panel_data.metadata["unavailable_models"] == []
    assert panel_data.rows("market_valuation_reference_charts") == []
    assert panel_data.rows("market_environment_assets") == []
    assert panel_data.rows("market_environment_model") == []


def test_pure_scoped_postgresql_read_is_empty_when_unpublished(migrated_postgres_dsn: str) -> None:
    panel_data = loaders_owner.load_table_panel_data(typed_config(migrated_postgres_dsn), "source_health")

    assert panel_data.status.source == "postgresql"
    assert panel_data.rows("source_health") == []


def test_scoped_panel_status_is_ready_when_publication_has_rows(migrated_postgres_dsn: str) -> None:
    config = typed_config(migrated_postgres_dsn)
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = AnalysisRepository(runtime)
    run_id = repository.start_run(
        "feed", input_cutoff=datetime.now(UTC), code_version="test", inputs={"feed": 1}
    )
    repository.finish_run(run_id, "succeeded")
    repository.publish(run_id, "feed", {"feed_signals": [{"symbol": "NVDA", "summary": "NVDA thesis"}]})
    runtime.close()
    panel_data = loaders_owner.load_panel_scope_data(config, "feed")

    assert panel_data.status.ready is True
    assert panel_data.rows("feed_signals")


def test_panel_contract_lists_scope_and_ticker_tables() -> None:
    contract = panel_contract_payload()

    assert contract["scopes"]["feed"] == ["feed_signals"]
    assert "source_freshness" not in contract["scopes"]["watchlist"]
    assert contract["scopes"]["health"] == [
        "source_catalog",
        "source_freshness",
        "source_health",
        "source_runs",
        "provider_runs",
        "broker_status",
        "option_recovery_funnel",
        "option_recovery_event",
        "option_recovery_opportunity",
        "option_recovery_family_performance",
        "option_recovery_agent_provenance",
        "option_recovery_health",
    ]
    assert "universe_screen" in contract["watchlist_section_tables"]
    assert "decision_queue" in contract["ticker_tables"]
    assert "ticker_data_sources" not in contract["ticker_tables"]


def test_options_radar_scope_compacts_heavy_learning_tables() -> None:
    panel_data = PanelData(
        status=DataStatus(True, "ok", "test"),
        tables={
            "option_radar_summary": [{"strategy_version": "v1"}],
            "option_radar_opportunity": [{"opportunity_id": "opp-1"}],
            "candidate_event": [{"event_id": "event-1"}],
            "missed_winner_event": [
                {
                    "missed_id": f"missed-{index}",
                    "ticker": "NVDA",
                    "raw": {"sample_path": ["x" * 1000]},
                }
                for index in range(120)
            ],
            "strategy_backtest_result": [
                {
                    "backtest_id": f"backtest-{index}",
                    "proposal_id": f"proposal-{index}",
                    "metrics": {"sample_outcomes": ["x" * 1000]},
                    "raw": {"debug": "x" * 1000},
                }
                for index in range(140)
            ],
            "strategy_forward_test_result": [
                {
                    "forward_test_id": f"forward-{index}",
                    "proposal_id": f"proposal-{index}",
                    "metrics": {"sample_outcomes": ["x" * 1000]},
                    "raw": {"min_forward_test_days": 5, "debug": "x" * 1000},
                }
                for index in range(140)
            ],
        },
    )

    payload = payloads_owner.panel_snapshot_payload(panel_data, "options-radar")
    tables = payload["tables"]

    assert "missed_winner_event" not in tables
    assert "strategy_backtest_result" not in tables
    assert "strategy_forward_test_result" not in tables
    assert "candidate_event" not in tables

    research_payload = payloads_owner.panel_snapshot_payload(panel_data, "research")

    assert "raw" in research_payload["tables"]["missed_winner_event"]["rows"][0]
    assert "metrics" in research_payload["tables"]["strategy_backtest_result"]["rows"][0]
    assert "raw" in research_payload["tables"]["strategy_backtest_result"]["rows"][0]


def test_watchlist_section_scopes_split_rows_and_support_tables() -> None:
    panel_data = PanelData(
        status=DataStatus(True, "ok", "test"),
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

    watched = payloads_owner.panel_snapshot_payload(panel_data, "watchlist-watched")
    unwatched = payloads_owner.panel_snapshot_payload(panel_data, "watchlist-unwatched")

    assert watched["tables"]["watchlist_watched"]["rows"] == [{"symbol": "NVDA", "watch_state": "watched"}]
    assert unwatched["tables"]["watchlist_unwatched"]["rows"] == [{"symbol": "AMD", "watch_state": "candidate"}]
    assert watched["tables"]["watchlist_watched_fundamentals"]["rows"][0]["symbol"] == "NVDA"
    assert unwatched["tables"]["watchlist_unwatched_fundamentals"]["rows"][0]["symbol"] == "AMD"
    assert watched["tables"]["watchlist_watched_technicals"]["rows"][0]["symbol"] == "NVDA"
    assert unwatched["tables"]["watchlist_unwatched_technicals"]["rows"][0]["symbol"] == "AMD"


def test_watchlist_unwatched_scope_pages_rows_and_keeps_total_count() -> None:
    panel_data = PanelData(
        status=DataStatus(True, "ok", "test"),
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

    page = payloads_owner.panel_snapshot_payload(panel_data, "watchlist-unwatched", offset=1, limit=1)

    assert page["tables"]["watchlist_unwatched"]["count"] == 3
    assert page["tables"]["watchlist_unwatched"]["offset"] == 1
    assert page["tables"]["watchlist_unwatched"]["limit"] == 1
    assert page["tables"]["watchlist_unwatched"]["rows"] == [{"symbol": "MSFT", "watch_state": "candidate"}]
    assert page["tables"]["watchlist_unwatched_quotes"]["rows"] == [{"symbol": "MSFT", "price": 100}]


def test_watchlist_watched_scope_includes_unwatched_count_without_rows() -> None:
    panel_data = PanelData(
        status=DataStatus(True, "ok", "test"),
        tables={
            "universe_screen": [
                {"symbol": "NVDA", "watch_state": "watched"},
                {"symbol": "AMD", "watch_state": "candidate"},
            ],
        },
    )

    watched = payloads_owner.panel_snapshot_payload(panel_data, "watchlist-watched")

    assert watched["tables"]["watchlist_watched"]["count"] == 1
    assert watched["tables"]["watchlist_unwatched"]["count"] == 1
    assert watched["tables"]["watchlist_unwatched"]["rows"] == []


def test_watchlist_section_includes_manual_symbol_before_read_model_refresh() -> None:
    panel_data = PanelData(
        status=DataStatus(True, "ok", "test"),
        tables={
            "universe_screen": [],
            "manual_watchlist": [{"symbol": "IBM", "name": "IBM", "asset_class": "equity", "watch_state": "watched"}],
        },
    )

    watched = payloads_owner.panel_snapshot_payload(panel_data, "watchlist-watched")

    assert watched["tables"]["watchlist_watched"]["count"] == 1
    assert watched["tables"]["watchlist_watched"]["rows"][0]["symbol"] == "IBM"
    assert watched["tables"]["watchlist_watched"]["rows"][0]["watch_state"] == "watched"


def test_watchlist_section_manual_exclusion_removes_symbol_from_sections() -> None:
    panel_data = PanelData(
        status=DataStatus(True, "ok", "test"),
        tables={
            "universe_screen": [{"symbol": "AAPL", "watch_state": "watched"}],
            "manual_watchlist": [{"symbol": "AAPL", "name": "Apple", "asset_class": "equity", "watch_state": "excluded"}],
        },
    )

    watched = payloads_owner.panel_snapshot_payload(panel_data, "watchlist-watched")
    unwatched = payloads_owner.panel_snapshot_payload(panel_data, "watchlist-unwatched")

    assert watched["tables"]["watchlist_watched"]["rows"] == []
    assert watched["tables"]["watchlist_unwatched"]["count"] == 0
    assert unwatched["tables"]["watchlist_unwatched"]["rows"] == []


def test_ticker_payload_excludes_health_only_operational_tables() -> None:
    panel_data = PanelData(
        status=DataStatus(True, "ok", "test"),
        tables={
            "decision_queue": [{"symbol": "NVDA", "score": 91}],
            "decision_readiness": [{"symbol": "NVDA", "status": "blocked"}],
            "broker_status": [{"provider": "ibkr", "status": "expected_login_required"}],
            "broker_accounts": [{"provider": "ibkr", "account_id": "demo"}],
            "paper_orders": [{"symbol": "NVDA", "status": "staged"}],
        },
    )

    payload = payloads_owner.ticker_payload(panel_data, "nvda")

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
    config = typed_config()
    panel_data = PanelData(status=DataStatus(True, "ok", "test"), tables={})

    payload = settings_owner.settings_payload(config, panel_data)

    assert payload["status"]["ready"] is True
    assert payload["config"]["database"]["url"] == "postgresql:///market"
    assert payload["integration"]["database_url"] == "postgresql:///market"
    assert payload["integration"]["arco_raw_dir"] == "/Volumes/agent/brain/raw/sources/arco"
    assert payload["integration"]["birdclaw_command"] == "Not configured"


def test_settings_payload_redacts_database_credentials() -> None:
    config = typed_config(
        "postgresql://market:secret@db.internal:5433/market?sslmode=require",
    )
    panel_data = PanelData(status=DataStatus(True, "ok", "test"), tables={})

    payload = settings_owner.settings_payload(config, panel_data)

    assert payload["config"]["database"]["url"] == "postgresql://db.internal:5433/market"
    assert payload["integration"]["database_url"] == "postgresql://db.internal:5433/market"
    assert "runtime_overrides" not in payload["config"]
    assert "provider" not in payload["config"]
    assert "secret" not in str(payload)


def test_status_payload_exposes_option_agent_runtime_metadata() -> None:
    config = typed_config(
        raw={
            "agents": {
                "option_agent": {
                    "enabled": True,
                    "command": "market-run-option-agent",
                    "thesis_limit": 16,
                    "postmortem_limit": 4,
                    "timeout_seconds": 180,
                    "provider": "codex",
                },
            },
        },
    )
    panel_data = PanelData(status=DataStatus(True, "ok", "test"), tables={})
    panel_data.metadata.update(payloads_owner.runtime_metadata(config))

    payload = payloads_owner.status_payload(panel_data)

    option_agent = payload["metadata"]["agents"]["option_agent"]
    assert option_agent["active"] is True
    assert option_agent["configured"] is True
    assert option_agent["status"] == "active"
    assert option_agent["limit"] == 20
    assert option_agent["timeout_seconds"] == 180
    assert option_agent["request_cap"] == 12
    assert option_agent["queue_policy"] == "current_ranked_candidates_plus_ondemand"


def test_status_payload_reports_disabled_option_agent_paused() -> None:
    config = typed_config(raw={"agents": {"option_agent": {"enabled": False}}})
    panel_data = PanelData(status=DataStatus(True, "ok", "test"), tables={})
    panel_data.metadata.update(payloads_owner.runtime_metadata(config))

    option_agent = payloads_owner.status_payload(panel_data)["metadata"]["agents"]["option_agent"]

    assert option_agent["active"] is False
    assert option_agent["configured"] is True
    assert option_agent["status"] == "paused"


def test_fastapi_config_reports_runtime_database_override(tmp_path, monkeypatch) -> None:
    runtime_url = "postgresql://localhost/runtime"
    monkeypatch.setenv("MARKET_DATABASE_URL", runtime_url)

    config = load_config(tmp_path / "missing.yaml")

    assert config.database.url == runtime_url


def test_portfolio_position_projection_is_owned_by_transaction_ledger(migrated_postgres_dsn: str) -> None:
    config = typed_config(migrated_postgres_dsn)

    saved = record_portfolio_transaction(
        config,
        {
            "symbol": "nvda",
            "transaction_type": "opening_balance",
            "quantity": 3,
            "price": 125.5,
            "executed_at": "2024-01-15T00:00:00Z",
            "idempotency_key": "test-opening-nvda",
            "notes": "core",
        },
    )
    rows = portfolio_rows(config)

    assert saved["symbol"] == "NVDA"
    assert saved["transaction_type"] == "opening_balance"
    assert rows[0]["symbol"] == "NVDA"
    assert rows[0]["quantity"] == 3
    assert str(rows[0]["purchase_date"]) == "2024-01-14"


def test_save_thesis_records_content_and_clears_stale(migrated_postgres_dsn: str) -> None:
    config = typed_config(migrated_postgres_dsn, raw={"watchlist": [{"symbol": "NVDA"}]})

    saved = mutations_owner.save_thesis(
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

    rows = thesis_monitor_rows(config)
    nvda = next(row for row in rows if row["symbol"] == "NVDA")
    assert nvda["source"] == "theses"
    assert nvda["stale_thesis"] is False
    assert nvda.get("needs_review", False) is False
    assert nvda["invalidation_price"] == 95


def test_save_thesis_requires_thesis_text(migrated_postgres_dsn: str) -> None:
    config = typed_config(migrated_postgres_dsn)
    with pytest.raises(ValueError):
        mutations_owner.save_thesis(config, "ZZZT", {"thesis": "   "})


def test_mark_thesis_reviewed_stamps_review_date(migrated_postgres_dsn: str) -> None:
    config = typed_config(migrated_postgres_dsn, raw={"watchlist": [{"symbol": "MU"}]})

    mutations_owner.save_thesis(config, "MU", {"thesis": "Memory upcycle.", "invalidation": "below $80"})
    reviewed = mutations_owner.mark_thesis_reviewed(config, "mu")

    assert reviewed["symbol"] == "MU"
    assert reviewed["last_reviewed"]


def test_thesis_v3_bearish_price_rule_and_history(migrated_postgres_dsn: str) -> None:
    config = typed_config(migrated_postgres_dsn, raw={"watchlist": [{"symbol": "TSLA"}]})

    first = mutations_owner.save_thesis(
        config,
        "TSLA",
        {
            "thesis": "Multiple compression continues while estimate revisions fall.",
            "why": "Watched as a short-side risk case.",
            "direction": "bearish",
            "confidence": "medium",
            "invalidation": "Invalidated above $310.",
            "invalidation_price": 310,
            "change_rationale": "Initial bearish monitor.",
        },
    )
    second = mutations_owner.save_thesis(
        config,
        "TSLA",
        {
            "thesis": "Multiple compression continues while deliveries disappoint.",
            "why": "Watched as a short-side risk case.",
            "direction": "bearish",
            "confidence": "medium",
            "invalidation": "Invalidated above $310.",
            "invalidation_price": 310,
            "change_rationale": "Updated delivery pillar.",
        },
    )
    history = thesis_history(config, "tsla")
    row = next(row for row in thesis_monitor_rows(config) if row["symbol"] == "TSLA")

    assert first["revision"] == 1
    assert second["revision"] == 2
    assert row["invalidation_operator"] == ">="
    assert row["schema_version"] == 3
    assert history["revisions"][0]["diff"]["changed_keys"]
    assert len(history["review_events"]) == 2


def test_thesis_review_rejects_empty_legacy_acknowledgement(migrated_postgres_dsn: str) -> None:
    from investment_panel.database.authority import runtime_for_config
    from investment_panel.database.instruments import reconcile_instrument
    from psycopg.types.json import Jsonb

    config = typed_config(migrated_postgres_dsn)
    runtime = runtime_for_config(config)
    with runtime.transaction() as connection:
        instrument_id = reconcile_instrument(connection, "BLNK", name="Blank", category="thesis")
        connection.execute(
            "INSERT INTO app.thesis (instrument_id, revision, status, thesis) VALUES (%s, 1, 'current', %s)",
            [instrument_id, Jsonb({"schema_version": 3, "last_reviewed": "2026-07-01T00:00:00Z"})],
        )

    with pytest.raises(ValueError, match="empty-thesis"):
        mutations_owner.mark_thesis_reviewed(config, "BLNK")


def test_delete_config_watchlist_symbol_persists_unwatch_override(migrated_postgres_dsn: str) -> None:
    config = typed_config(
        migrated_postgres_dsn,
        raw={"watchlist": [{"symbol": "NVDA", "name": "NVIDIA", "asset_class": "equity"}]},
    )
    mutations_owner.save_watchlist_symbol(config, config.watchlist[0])

    deleted = mutations_owner.delete_watchlist_symbol(config, "NVDA")
    assert deleted == {"symbol": "NVDA", "deleted": True}
    assert watchlist_rows(config) == []
    assert watchlist_rows(config, include_excluded=True)[0]["watch_state"] == "excluded"


def test_delete_source_watchlist_symbol_persists_unwatch_override(migrated_postgres_dsn: str) -> None:
    config = typed_config(migrated_postgres_dsn, raw={"watchlist": []})
    mutations_owner.save_watchlist_symbol(config, {"symbol": "PLTR", "name": "Palantir"})

    deleted = mutations_owner.delete_watchlist_symbol(config, "PLTR")
    assert deleted == {"symbol": "PLTR", "deleted": True}
    assert watchlist_rows(config) == []
    assert watchlist_rows(config, include_excluded=True)[0]["watch_state"] == "excluded"


def test_save_watchlist_crypto_alias_uses_crypto_asset_class(migrated_postgres_dsn: str) -> None:
    config = typed_config(migrated_postgres_dsn)

    saved = mutations_owner.save_watchlist_symbol(config, {"symbol": "btc", "asset_class": "equity"})
    assert saved["symbol"] == "BTC-USD"
    assert saved["asset_class"] == "crypto"
    assert watchlist_rows(config)[0]["asset_class"] == "crypto"


def test_populate_watchlist_symbol_data_runs_targeted_refresh(tmp_path, monkeypatch, migrated_postgres_dsn: str) -> None:
    import pandas as pd
    from investment_panel.jobs import update_market_data

    config = typed_config(
        migrated_postgres_dsn,
        raw={
            "market_data": {"lookback_days": 30, "mode": "online"},
            "data_sources": {
                "opencli": {"enabled": True, "command": "opencli", "timeout_seconds": 25},
                "tradingview": {"enabled": True},
                "yfinance": {"enabled": False},
            },
            "scoring": {"weights": {"technical": 1.0}},
            "watchlist": [],
        },
    )
    mutations_owner.save_watchlist_symbol(config, {"symbol": "XYZ"})

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

    monkeypatch.setattr(update_market_data, "fetch_prices", fetch_prices)
    result = mutations_owner.populate_watchlist_symbol_data(config, "XYZ", "equity")

    assert result["status"] == "ok"
    assert result["quote_rows"] == 2
    assert result["provider_rows_received"] == 2
    assert result["history_policy"] == "full_refresh"
    rows = loaders_owner.load_table_panel_data(config, "quotes").rows("quotes")
    assert rows[0]["symbol"] == "XYZ"
    assert float(rows[0]["price"]) == 12


def test_populate_watchlist_symbol_data_marks_failed_ingest_run(
    monkeypatch, migrated_postgres_dsn: str
) -> None:
    from investment_panel.jobs import update_market_data

    config = typed_config(
        migrated_postgres_dsn,
        raw={"market_data": {"mode": "online"}, "data_sources": {"yfinance": {"enabled": False}}},
    )
    mutations_owner.save_watchlist_symbol(config, {"symbol": "XYZ"})
    monkeypatch.setattr(update_market_data, "fetch_prices", lambda *_args: (_ for _ in ()).throw(RuntimeError("provider failed")))

    result = mutations_owner.populate_watchlist_symbol_data(config, "XYZ", "equity")

    assert result["status"] == "error"
    assert "provider failed" in result["error"]


def test_save_watchlist_symbol_rejects_malformed_ticker(migrated_postgres_dsn: str) -> None:
    config = typed_config(migrated_postgres_dsn)

    with pytest.raises(ValueError, match="valid ticker"):
        mutations_owner.save_watchlist_symbol(config, {"symbol": "ABC!"})
