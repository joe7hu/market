import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from app import data_access
from investment_panel.core.panel import market_freshness
from investment_panel.core.db import db, init_db
from investment_panel.core.panel import read_session
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import DatabaseRuntime


def test_unavailable_postgresql_returns_explicit_status() -> None:
    panel_data = data_access.load_panel_data({"database": {"url": "postgresql://127.0.0.1:1/missing"}})

    assert panel_data.status.ready is False
    assert panel_data.status.source == "postgresql-error"
    assert "PostgreSQL read models unavailable" in panel_data.status.message
    assert panel_data.rows("candidates") == []


def test_unported_postgresql_model_is_explicitly_unavailable(migrated_postgres_dsn: str) -> None:
    panel_data = data_access.load_table_panel_data(
        {"database": {"url": migrated_postgres_dsn}}, "technicals"
    )

    assert panel_data.status.ready is False
    assert panel_data.status.source == "postgresql-partial"
    assert panel_data.metadata["unavailable_models"] == ["technicals"]


def test_load_config_honors_market_database_url_override(tmp_path, monkeypatch) -> None:
    url = "postgresql://localhost/market-test"
    monkeypatch.setenv("MARKET_DATABASE_URL", url)

    config = data_access.load_config(tmp_path / "missing-config.yaml")

    assert config["database"]["url"] == url


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


def test_ticker_payload_resolves_tradingview_exchange_from_search_rows() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "universe_screen": [{"symbol": "BFLY", "name": "Butterfly Network", "asset_class": "equity"}],
            "tradingview_symbol_search": [
                {"query": "BFLY", "symbol": "BFLY", "description": "Butterfly Network", "instrument_type": "stock", "exchange": "BOATS"},
                {"query": "BFLY", "symbol": "BFLY", "description": "CBOE S&P 500 Iron Butterfly Index", "instrument_type": "index", "exchange": "CBOE"},
                {"query": "BFLY", "symbol": "BFLY", "description": "Butterfly Network", "instrument_type": "stock", "exchange": "NYSE"},
            ],
        },
    )

    payload = data_access.ticker_payload(panel_data, "bfly")

    assert payload["dossier"]["identity"]["tradingview_symbol"] == "NYSE:BFLY"


def test_ticker_payload_prefers_persisted_market_identity() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={
            "universe_screen": [{"symbol": "BFLY", "name": "Butterfly Network", "asset_class": "equity"}],
            "instrument_market_identity": [{"symbol": "BFLY", "primary_exchange": "NYSE", "tradingview_symbol": "NYSE:BFLY"}],
            "tradingview_symbol_search": [
                {"query": "BFLY", "symbol": "BFLY", "instrument_type": "stock", "exchange": "BOATS"},
            ],
        },
    )

    payload = data_access.ticker_payload(panel_data, "bfly")

    assert payload["dossier"]["identity"]["exchange"] == "NYSE"
    assert payload["dossier"]["identity"]["tradingview_symbol"] == "NYSE:BFLY"


def test_ticker_payload_does_not_guess_nasdaq_without_exchange_data() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
        tables={"universe_screen": [{"symbol": "BFLY", "name": "Butterfly Network", "asset_class": "equity"}]},
    )

    payload = data_access.ticker_payload(panel_data, "bfly")

    assert payload["dossier"]["identity"]["tradingview_symbol"] == "BFLY"


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


def test_scope_loader_materializes_only_requested_tables(migrated_postgres_dsn: str) -> None:
    config = {"database": {"url": migrated_postgres_dsn}}

    panel_data = data_access.load_panel_scope_data(config, "feed")

    assert set(panel_data.tables) == {"feed_signals"}
    assert panel_data.rows("source_freshness") == []


def test_source_table_loader_uses_requested_postgresql_model(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_helper(config: dict[str, object], table_names: tuple[str, ...]):
        calls.append(table_names)
        return {"source_items": []}, {"database": "postgresql"}

    monkeypatch.setattr(data_access.loaders, "load_postgres_tables", fake_helper)

    data_access.load_table_panel_data({"database": {"url": "postgresql:///test"}}, "source_items")

    assert calls == [("source_items",)]


def test_default_panel_loader_requests_complete_contract(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_helper(config: dict[str, object], table_names: tuple[str, ...]):
        calls.append(table_names)
        return {name: [] for name in table_names}, {"database": "postgresql"}

    monkeypatch.setattr(data_access.loaders, "load_postgres_tables", fake_helper)

    panel_data = data_access.load_panel_data({"database": {"url": "postgresql:///test"}})

    assert panel_data.status.ready is True
    assert len(calls) == 1
    assert "signals" in calls[0]
    assert "option_radar_opportunity" in calls[0]


def test_empty_settings_scope_does_not_touch_missing_database(tmp_path) -> None:
    db_path = tmp_path / "missing-settings.duckdb"

    panel_data = data_access.load_panel_scope_data({"database": {"duckdb_path": str(db_path)}}, "settings")

    assert panel_data.status.ready is True
    assert panel_data.status.source == "postgresql"
    assert panel_data.tables == {}
    assert not db_path.exists()


def test_market_panel_loader_handles_empty_postgresql(migrated_postgres_dsn: str) -> None:
    panel_data = data_access.load_market_panel_data({"database": {"url": migrated_postgres_dsn}})

    assert panel_data.status.ready is True
    assert panel_data.status.source == "postgresql"
    assert panel_data.metadata["unavailable_models"] == []
    assert panel_data.rows("market_valuation_reference_charts") == []
    assert panel_data.rows("market_environment_assets") == []
    assert panel_data.rows("market_environment_model") == []


def test_pure_scoped_postgresql_read_is_empty_when_unpublished(migrated_postgres_dsn: str) -> None:
    panel_data = data_access.load_table_panel_data({"database": {"url": migrated_postgres_dsn}}, "source_health")

    assert panel_data.status.source == "postgresql"
    assert panel_data.rows("source_health") == []


def test_scheduler_compatible_panel_read_does_not_poison_later_writer(tmp_path) -> None:
    db_path = tmp_path / "scheduler-compatible.duckdb"
    init_db(db_path)
    script = f"""
from pathlib import Path
from investment_panel.core.db import init_db
from investment_panel.core.panel.read_session import panel_read_session

db_path = Path({str(db_path)!r})
with panel_read_session(db_path, needs_write=False) as con:
    assert con is not None
    con.execute("SELECT 1").fetchone()
init_db(db_path)
"""
    env = {**os.environ, "MARKET_SCHEDULER_ENABLED": "1"}
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_panel_read_session_uses_read_only_fail_fast_by_default(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "read-only.duckdb"
    db_path.write_bytes(b"placeholder")
    calls: list[tuple[bool, int, float]] = []

    @contextmanager
    def fake_db(_path, read_only: bool = False, *, retries: int = 30, delay_seconds: float = 1.0):
        calls.append((read_only, retries, delay_seconds))
        yield object()

    monkeypatch.setenv("MARKET_SCHEDULER_ENABLED", "1")
    monkeypatch.delenv("MARKET_PANEL_READ_ONLY", raising=False)
    monkeypatch.delenv("MARKET_PANEL_READ_LOCK_RETRIES", raising=False)
    monkeypatch.setattr(read_session, "_schema_needs_migration", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(read_session, "db", fake_db)

    with read_session.panel_read_session(db_path, needs_write=False) as con:
        assert con is not None

    assert calls == [(True, 0, 0.1)]


def test_scoped_panel_status_is_ready_when_publication_has_rows(migrated_postgres_dsn: str) -> None:
    config = {"database": {"url": migrated_postgres_dsn}}
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = AnalysisRepository(runtime)
    run_id = repository.start_run(
        "feed", input_cutoff=datetime.now(UTC), code_version="test", inputs={"feed": 1}
    )
    repository.finish_run(run_id, "succeeded")
    repository.publish(run_id, "feed", {"feed_signals": [{"symbol": "NVDA", "summary": "NVDA thesis"}]})
    runtime.close()
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
    assert contract["endpoint_tables"]["instrument-market-identity"] == "instrument_market_identity"
    assert contract["endpoint_tables"]["watchlist/symbols"] == "manual_watchlist"


def test_options_radar_scope_compacts_heavy_learning_tables() -> None:
    panel_data = data_access.PanelData(
        status=data_access.DataStatus(True, "ok", "test"),
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

    payload = data_access.panel_snapshot_payload(panel_data, "options-radar")
    tables = payload["tables"]

    assert tables["missed_winner_event"]["count"] == 120
    assert len(tables["missed_winner_event"]["rows"]) == 80
    assert "raw" not in tables["missed_winner_event"]["rows"][0]
    assert tables["strategy_backtest_result"]["count"] == 140
    assert len(tables["strategy_backtest_result"]["rows"]) == 100
    assert "metrics" not in tables["strategy_backtest_result"]["rows"][0]
    assert "raw" not in tables["strategy_backtest_result"]["rows"][0]
    assert tables["strategy_forward_test_result"]["count"] == 140
    assert len(tables["strategy_forward_test_result"]["rows"]) == 100
    assert "metrics" not in tables["strategy_forward_test_result"]["rows"][0]
    assert tables["strategy_forward_test_result"]["rows"][0]["raw"] == {"min_forward_test_days": 5}

    research_payload = data_access.panel_snapshot_payload(panel_data, "research")

    assert "raw" in research_payload["tables"]["missed_winner_event"]["rows"][0]
    assert "metrics" in research_payload["tables"]["strategy_backtest_result"]["rows"][0]
    assert "raw" in research_payload["tables"]["strategy_backtest_result"]["rows"][0]


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
        "database": {"url": "postgresql:///market"},
        "arco": {"raw_dir": "/Volumes/agent/brain/raw/sources/arco"},
        "birdclaw": {"command": "birdclaw export"},
    }
    panel_data = data_access.PanelData(status=data_access.DataStatus(True, "ok", "test"), tables={})

    payload = data_access.settings_payload(config, panel_data)

    assert payload["status"]["ready"] is True
    assert payload["config"]["database"]["url"] == "postgresql:///market"
    assert payload["integration"]["database_url"] == "postgresql:///market"
    assert payload["integration"]["arco_raw_dir"] == "/Volumes/agent/brain/raw/sources/arco"
    assert payload["integration"]["birdclaw_command"] == "birdclaw export"


def test_settings_payload_redacts_database_credentials() -> None:
    config = {"database": {"url": "postgresql://market:secret@db.internal:5433/market?sslmode=require"}}
    panel_data = data_access.PanelData(status=data_access.DataStatus(True, "ok", "test"), tables={})

    payload = data_access.settings_payload(config, panel_data)

    assert payload["config"]["database"]["url"] == "postgresql://db.internal:5433/market"
    assert payload["integration"]["database_url"] == "postgresql://db.internal:5433/market"
    assert "secret" not in str(payload)


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


def test_fastapi_config_reports_runtime_database_override(tmp_path, monkeypatch) -> None:
    runtime_url = "postgresql://localhost/runtime"
    monkeypatch.setenv("MARKET_DATABASE_URL", runtime_url)

    config = data_access.load_config(tmp_path / "missing.yaml")

    assert config["database"]["url"] == runtime_url
    assert config["runtime_overrides"]["MARKET_DATABASE_URL"] == runtime_url


def test_save_and_delete_portfolio_position(migrated_postgres_dsn: str) -> None:
    config = {"database": {"url": migrated_postgres_dsn}}

    saved = data_access.save_portfolio_position(
        config,
        {"symbol": "nvda", "quantity": 3, "avg_cost": 125.5, "purchase_date": "2024-01-15", "notes": "core"},
    )
    rows = data_access.portfolio_rows(config)

    assert saved["symbol"] == "NVDA"
    assert saved["purchase_date"] == "2024-01-15"
    assert rows[0]["symbol"] == "NVDA"
    assert rows[0]["quantity"] == 3
    assert str(rows[0]["purchase_date"]) == "2024-01-15"

    deleted = data_access.delete_portfolio_position(config, "NVDA")
    assert deleted == {"symbol": "NVDA", "deleted": True}
    assert data_access.portfolio_rows(config) == []


def test_save_thesis_records_content_and_clears_stale(migrated_postgres_dsn: str) -> None:
    config = {"database": {"url": migrated_postgres_dsn}, "watchlist": [{"symbol": "NVDA"}]}

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

    rows = data_access.thesis_monitor_rows(config)
    nvda = next(row for row in rows if row["symbol"] == "NVDA")
    assert nvda["source"] == "theses"
    assert nvda["stale_thesis"] is False
    assert nvda.get("needs_review", False) is False
    assert nvda["invalidation_price"] == 95


def test_save_thesis_requires_thesis_text(tmp_path) -> None:
    config = {"database": {"duckdb_path": str(tmp_path / "thesis-empty.duckdb")}}
    with pytest.raises(ValueError):
        data_access.save_thesis(config, "NVDA", {"thesis": "   "})


def test_mark_thesis_reviewed_stamps_review_date(migrated_postgres_dsn: str) -> None:
    config = {"database": {"url": migrated_postgres_dsn}, "watchlist": [{"symbol": "MU"}]}

    data_access.save_thesis(config, "MU", {"thesis": "Memory upcycle.", "invalidation": "below $80"})
    reviewed = data_access.mark_thesis_reviewed(config, "mu")

    assert reviewed["symbol"] == "MU"
    assert reviewed["last_reviewed"]


def test_delete_config_watchlist_symbol_persists_unwatch_override(migrated_postgres_dsn: str) -> None:
    config = {
        "database": {"url": migrated_postgres_dsn},
        "watchlist": [{"symbol": "NVDA", "name": "NVIDIA", "asset_class": "equity"}],
    }
    data_access.save_watchlist_symbol(config, config["watchlist"][0])

    deleted = data_access.delete_watchlist_symbol(config, "NVDA")
    assert deleted == {"symbol": "NVDA", "deleted": True}
    assert data_access.watchlist_rows(config) == []
    assert data_access.watchlist_rows(config, include_excluded=True)[0]["watch_state"] == "excluded"


def test_delete_source_watchlist_symbol_persists_unwatch_override(migrated_postgres_dsn: str) -> None:
    config = {"database": {"url": migrated_postgres_dsn}, "watchlist": []}
    data_access.save_watchlist_symbol(config, {"symbol": "PLTR", "name": "Palantir"})

    deleted = data_access.delete_watchlist_symbol(config, "PLTR")
    assert deleted == {"symbol": "PLTR", "deleted": True}
    assert data_access.watchlist_rows(config) == []
    assert data_access.watchlist_rows(config, include_excluded=True)[0]["watch_state"] == "excluded"


def test_save_watchlist_crypto_alias_uses_crypto_asset_class(migrated_postgres_dsn: str) -> None:
    config = {"database": {"url": migrated_postgres_dsn}}

    saved = data_access.save_watchlist_symbol(config, {"symbol": "btc", "asset_class": "equity"})
    assert saved["symbol"] == "BTC-USD"
    assert saved["asset_class"] == "crypto"
    assert data_access.watchlist_rows(config)[0]["asset_class"] == "crypto"


def test_populate_watchlist_symbol_data_runs_targeted_refresh(tmp_path, monkeypatch, migrated_postgres_dsn: str) -> None:
    import pandas as pd

    config = {
        "database": {"url": migrated_postgres_dsn},
        "market_data": {"lookback_days": 30, "mode": "online"},
        "data_sources": {
            "opencli": {"enabled": True, "command": "opencli", "timeout_seconds": 25},
            "tradingview": {"enabled": True},
            "yfinance": {"enabled": True},
        },
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
    result = data_access.populate_watchlist_symbol_data(config, "XYZ", "equity")

    assert result["status"] == "ok"
    assert result["quote_rows"] == 1
    assert result["provider_rows_received"] == 2
    assert result["history_policy"] == "latest_only"
    rows = data_access.load_table_panel_data(config, "quotes").rows("quotes")
    assert rows[0]["symbol"] == "XYZ"
    assert float(rows[0]["price"]) == 12


def test_save_watchlist_symbol_rejects_malformed_ticker(tmp_path) -> None:
    config = {"database": {"duckdb_path": str(tmp_path / "bad-watchlist.duckdb")}}

    import pytest

    with pytest.raises(ValueError, match="valid ticker"):
        data_access.save_watchlist_symbol(config, {"symbol": "ABC!"})
