from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import deps as api_deps
from app import main as api_main
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.decision import build_source_freshness, classify_freshness, symbol_freshness_detail
from investment_panel.core.panel import load_panel_data
from investment_panel.core.source_ingestion.health import record_verified_sources
from investment_panel.core.sources import MUNGERMODE_BENCHMARK_SOURCES, SOURCE_DEFINITIONS, source_ingestion_audit


DECISION_TABLES = {
    "discovered_universe",
    "decision_queue",
    "decision_readiness",
    "source_freshness",
    "symbol_decision_snapshot",
}


def test_discovered_universe_merges_all_source_clusters(tmp_path: Path) -> None:
    db_path = seed_decision_fixture(tmp_path)

    panel = load_panel_data(config_for(db_path))
    discovered = require_rows(panel, "discovered_universe")
    symbols = {normalize_symbol(row.get("symbol")) for row in discovered}

    assert {"NVDA", "MU", "MSFT", "SMCI", "AAPL", "COIN", "TSLA"}.issubset(symbols)
    for symbol in {"NVDA", "MU", "MSFT", "SMCI", "AAPL", "COIN", "TSLA"}:
        row = row_for_symbol(discovered, symbol)
        assert row.get("source_count", 0) >= 1
        assert "latest_observed_at" in row
        assert "next_event_at" in row
        assert row.get("eligibility_status") in {"eligible", "ineligible", "watch_only", "source_thin"}
        assert nonempty_list(row.get("inclusion_reasons"))


def test_manual_watchlist_is_persisted_universe_source_for_all_pages(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)

    with db(db_path) as con:
        con.execute(
            """
            INSERT INTO manual_watchlist (symbol, name, asset_class, notes, created_at, updated_at)
            VALUES ('CRWV', 'CoreWeave', 'equity', 'manual AI infra watch', now(), now())
            """
        )

    panel = load_panel_data(config_for(db_path) | {"watchlist": []})
    discovered = require_rows(panel, "discovered_universe")
    screen = require_rows(panel, "universe_screen")

    discovered_row = row_for_symbol(discovered, "CRWV")
    screen_row = row_for_symbol(screen, "CRWV")

    assert discovered_row["source_counts"]["manual_watchlist"] == 1
    assert discovered_row["source_count"] == 0
    assert "manual watchlist" in discovered_row["inclusion_reasons"]
    assert screen_row["watch_state"] == "watched"

    with db(db_path) as con:
        instrument = row_for_symbol(query_rows(con, "SELECT symbol, name, asset_class, source FROM instruments"), "CRWV")
    assert instrument["source"] == "manual_watchlist"


def test_source_freshness_contracts_degrade_stale_and_docs_only_rows(tmp_path: Path) -> None:
    db_path = seed_decision_fixture(tmp_path)

    panel = load_panel_data(config_for(db_path))
    freshness = require_rows(panel, "source_freshness")

    docs = row_for_source(freshness, "docs/data-sources.md")
    assert docs.get("source_kind") in {"documentation", "docs"}
    assert docs.get("freshness_status") in {"documentation", "not_applicable"}

    stale_quote = row_for_source(freshness, "tradingview:OLD")
    assert stale_quote.get("freshness_status") in {"stale", "degraded", "failed"}
    assert stale_quote.get("freshness_status") != "healthy"

    failed_provider = row_for_source(freshness, "yfinance:provider-run")
    assert failed_provider.get("freshness_status") in {"failed", "degraded", "stale"}
    assert failed_provider.get("provider_status") == "failed"

    fresh_quote = row_for_source(freshness, "tradingview:NVDA")
    assert fresh_quote.get("freshness_status") in {"fresh", "healthy"}


def test_source_freshness_aggregates_historical_provider_items(tmp_path: Path) -> None:
    db_path = tmp_path / "freshness.duckdb"
    init_db(db_path)
    observed = datetime.now(UTC)
    with db(db_path, read_only=False) as con:
        for index in range(100):
            con.execute(
                """
                INSERT INTO source_items (id, source_id, source_kind, observed_at)
                VALUES (?, 'legacy_feed', 'news', ?)
                """,
                [f"item-{index}", observed - timedelta(minutes=index)],
            )
        for index in range(50):
            expiry = (observed + timedelta(days=index + 1)).date().isoformat()
            con.execute(
                """
                INSERT INTO options_expiries (symbol, expiry, observed_at, source)
                VALUES ('NVDA', ?, ?, 'ibkr')
                """,
                [expiry, observed - timedelta(minutes=index)],
            )

        rows = build_source_freshness(con)

    source_item_rows = [row for row in rows if str(row.get("source_key", "")).startswith("legacy_feed:news")]
    option_rows = [row for row in rows if str(row.get("source_key", "")).startswith("ibkr:options:NVDA")]
    stale_provider_item_rows = [row for row in rows if row.get("provider") == "legacy_feed" and row.get("source_type") == "provider_run"]

    assert len(source_item_rows) == 1
    assert source_item_rows[0]["detail"] == "100 source items"
    assert len(option_rows) == 1
    assert option_rows[0]["detail"] == "50 expiries"
    assert stale_provider_item_rows == []


def test_verified_docs_and_live_source_health_do_not_collide(tmp_path: Path) -> None:
    db_path = tmp_path / "health.duckdb"
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        record_verified_sources(con)
        con.execute(
            """
            INSERT OR REPLACE INTO source_health (source, checked_at, status, detail, source_url)
            VALUES ('sec_edgar', ?, 'ok', 'HTTP 200', 'https://data.sec.gov/')
            """,
            [datetime.now(UTC)],
        )
        rows = query_rows(con, "SELECT source, status FROM source_health WHERE source IN ('docs:sec_edgar', 'sec_edgar') ORDER BY source")

    assert rows == [
        {"source": "docs:sec_edgar", "status": "verified_docs"},
        {"source": "sec_edgar", "status": "ok"},
    ]


def test_intraday_freshness_uses_market_hours_not_wall_clock() -> None:
    sunday_after_close = datetime(2026, 5, 17, 16, 0, tzinfo=UTC)
    friday_close_snapshot = datetime(2026, 5, 15, 20, 0, tzinfo=UTC)
    friday_morning_snapshot = datetime(2026, 5, 15, 14, 0, tzinfo=UTC)
    old_snapshot = datetime(2026, 5, 7, 20, 0, tzinfo=UTC)

    assert classify_freshness("intraday_quote", friday_close_snapshot, "ok", False, now=sunday_after_close) == "fresh"
    assert classify_freshness("intraday_quote", friday_morning_snapshot, "ok", False, now=sunday_after_close) == "stale"
    assert classify_freshness("intraday_quote", old_snapshot, "ok", False, now=sunday_after_close) == "stale"


def test_source_freshness_preserves_explicit_unknown_status() -> None:
    checked_at = datetime(2026, 5, 17, 16, 0, tzinfo=UTC)
    assert classify_freshness("provider_run", checked_at, "unknown", False, now=checked_at) == "unknown"
    assert classify_freshness("provider_run", checked_at, "not_loaded", False, now=checked_at) == "stale"


def test_daily_freshness_uses_trading_day_lag_when_market_is_closed() -> None:
    sunday_after_close = datetime(2026, 5, 17, 16, 0, tzinfo=UTC)
    friday_daily = datetime(2026, 5, 15, 0, 0, tzinfo=UTC)
    stale_daily = datetime(2026, 5, 13, 0, 0, tzinfo=UTC)

    assert classify_freshness("daily", friday_daily, "ok", False, now=sunday_after_close) == "fresh"
    assert classify_freshness("daily", stale_daily, "ok", False, now=sunday_after_close) == "stale"


def test_previous_close_satisfies_quote_freshness_only_when_market_is_closed() -> None:
    sunday_after_close = datetime(2026, 5, 17, 16, 0, tzinfo=UTC)
    monday_open = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    friday_daily = datetime(2026, 5, 15, 0, 0, tzinfo=UTC)

    assert classify_freshness("closing_quote", friday_daily, "ok", False, now=sunday_after_close) == "fresh"
    assert classify_freshness("closing_quote", friday_daily, "ok", False, now=monday_open) == "stale"


def test_previous_close_can_clear_stale_intraday_quote_gate_after_close() -> None:
    detail = symbol_freshness_detail(
        [
            {"source_key": "tradingview:NVDA", "source_type": "intraday_quote", "freshness_status": "stale"},
            {"source_key": "previous_close:NVDA", "source_type": "closing_quote", "freshness_status": "fresh"},
            {"source_key": "technicals:NVDA", "source_type": "daily", "freshness_status": "fresh"},
        ]
    )

    assert detail["NVDA"]["quote_freshness"] == "fresh"
    assert detail["NVDA"]["overall_decision_freshness"] == "fresh"


def test_decision_queue_applies_stale_evidence_liquidity_and_portfolio_gates(tmp_path: Path) -> None:
    db_path = seed_decision_fixture(tmp_path)

    panel = load_panel_data(config_for(db_path))
    queue = require_rows(panel, "decision_queue")
    grades = [row.get("action_grade") for row in queue]

    assert set(grades) >= {"Act", "Research", "Watch", "Reject", "Stale"}
    assert len(queue) <= 250
    hard_gate_terms = ("intraday", "daily", "stale")
    for row in queue:
        if row.get("action_grade") in {"Act", "Research"}:
            gates = [str(gate).lower() for gate in row.get("blocking_gates") or []]
            assert not any(term in gate for gate in gates for term in hard_gate_terms)

    top_act = next(row for row in queue if row.get("action_grade") == "Act")
    assert top_act["symbol"] == "NVDA"
    assert top_act.get("freshness_status") in {"fresh", "healthy"}
    assert top_act.get("evidence_count", 0) >= 2
    assert not top_act.get("blocking_gates")
    assert top_act.get("quote_freshness") == "fresh"
    assert top_act.get("daily_analysis_freshness") == "fresh"
    assert top_act.get("decision_score") >= top_act.get("action_score")

    research = row_for_symbol(queue, "RSCH")
    assert research.get("action_grade") == "Research"
    assert not contains_gate(research, "quote")
    assert not contains_gate(research, "daily")

    stale = row_for_symbol(queue, "OLD")
    assert stale.get("action_grade") == "Stale"
    assert stale.get("freshness_status") in {"stale", "degraded", "failed"}
    assert contains_gate(stale, "stale")

    thin = row_for_symbol(queue, "THIN")
    assert thin.get("action_grade") in {"Watch", "Reject"}
    assert contains_gate(thin, "evidence")

    illiquid = row_for_symbol(queue, "ILLIQ")
    assert illiquid.get("action_grade") == "Reject"
    assert contains_gate(illiquid, "liquidity")

    no_market_data = row_for_symbol(queue, "AAPL")
    assert no_market_data.get("action_grade") == "Stale"
    assert contains_gate(no_market_data, "intraday")
    assert contains_gate(no_market_data, "daily")
    assert no_market_data.get("raw_source_rows", 0) >= no_market_data.get("evidence_count", 0)
    assert no_market_data["decision_basis"]["evidence_items_count"] == no_market_data.get("evidence_items_count")


def test_symbol_decision_snapshot_explains_basis_blockers_and_invalidation(tmp_path: Path) -> None:
    db_path = seed_decision_fixture(tmp_path)

    panel = load_panel_data(config_for(db_path))
    snapshots = require_rows(panel, "symbol_decision_snapshot")
    nvda = row_for_symbol(snapshots, "NVDA")
    old = row_for_symbol(snapshots, "OLD")

    for row in [nvda, old]:
        assert row.get("action_grade")
        assert row.get("freshness_status")
        assert row.get("source_cluster")
        assert row.get("decision_basis")
        assert row.get("invalidation")
        assert row.get("as_of")

        assert nvda.get("action_grade") == "Act"
        assert nvda.get("quote_freshness") == "fresh"
        assert nvda.get("daily_analysis_freshness") == "fresh"
    assert old.get("action_grade") == "Stale"
    assert contains_gate(old, "stale")


def test_decision_readiness_contract_preserves_scores_and_unblock_actions(tmp_path: Path) -> None:
    db_path = seed_decision_fixture(tmp_path)

    panel = load_panel_data(config_for(db_path))
    readiness = require_rows(panel, "decision_readiness")
    nvda = row_for_symbol(readiness, "NVDA")
    old = row_for_symbol(readiness, "OLD")

    assert {
        "symbol",
        "status",
        "decision_score",
        "action_score",
        "freshness_status",
        "next_action",
        "source_counts",
        "portfolio_fit",
        "as_of",
    }.issubset(nvda)
    assert old["status"] == "blocked_refresh"
    assert old["decision_score"] >= old["action_score"]
    assert old["blockers"]
    assert old["missing_inputs"]
    assert any("stale" in blocker for blocker in old["blockers"])
    assert "full_market_refresh" in old["next_action"]
    assert nvda["portfolio_fit"]["has_portfolio_context"] is True


def test_canonical_source_signals_promote_universe_with_market_context_blockers(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    observed_at = datetime.now(UTC).isoformat()
    with db(db_path) as con:
        con.execute(
            "INSERT INTO news_items VALUES ('news-newt', ?, 'Test Research', 'NEWT source catalyst', ?, 'https://example.com/newt', 'test_research', '{}')",
            [observed_at, json.dumps(["NEWT"])],
        )

    panel = load_panel_data(config_for(db_path))
    sources = require_rows(panel, "sources")
    source_runs = require_rows(panel, "source_runs")
    source_items = require_rows(panel, "source_items")
    source_signals = require_rows(panel, "ticker_source_signals")
    discovered = require_rows(panel, "discovered_universe")
    queue = require_rows(panel, "decision_queue")

    assert any(row.get("source_id") == "test_research" for row in sources)
    assert any(row.get("source_id") == "test_research" for row in source_runs)
    assert any(row.get("id") == "news:news-newt" for row in source_items)

    signal = row_for_symbol(source_signals, "NEWT")
    assert signal["source_id"] == "test_research"
    assert signal["needs_market_context"] is True

    universe_row = row_for_symbol(discovered, "NEWT")
    assert universe_row["source_counts"]["test_research"] >= 1

    decision_row = row_for_symbol(queue, "NEWT")
    assert decision_row["action_grade"] == "Stale"
    assert contains_gate(decision_row, "intraday")
    assert contains_gate(decision_row, "daily")

    with db(db_path) as con:
        instruments = query_all(con, "SELECT symbol, category, source FROM instruments WHERE symbol = 'NEWT'")
    assert instruments == [{"symbol": "NEWT", "category": "source-discovered", "source": "source_signal:test_research"}]


def test_detail_source_tables_materialize_canonical_signals(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            "INSERT INTO equity_fundamentals VALUES ('NVDA', current_date, current_date, '10-K', ?, 'https://example.com/nvda-10k')",
            [json.dumps({"revenue": 100, "status": "ok"})],
        )
        con.execute(
            "INSERT INTO crypto_fundamentals VALUES ('BTC-USD', current_date, ?, 'coingecko')",
            [json.dumps({"market_cap": 1_000_000})],
        )
        con.execute(
            "INSERT INTO earnings_events VALUES ('COIN', current_date, 'earnings', ?, 'yfinance')",
            [json.dumps({"source": "calendar"})],
        )
        con.execute(
            "INSERT INTO analyst_estimates VALUES ('AMD', current_date, ?, 'yfinance')",
            [json.dumps({"recommendation": "buy"})],
        )
        con.execute(
            "INSERT INTO disclosures VALUES ('disc-rsch', 'public_disclosure_transaction', 'Test Trader', 'Test Filer', 'RSCH', current_date, current_date, 'BUY', '$1M', '{}', 'https://example.com/rsch')"
        )

    panel = load_panel_data(config_for(db_path))
    sources = require_rows(panel, "sources")
    source_items = require_rows(panel, "source_items")
    source_signals = require_rows(panel, "ticker_source_signals")

    source_ids = {row["source_id"] for row in sources}
    assert {"sec_edgar", "sec_annual_reports_10k", "coingecko", "yfinance"}.issubset(source_ids)
    assert {"equity_fundamental", "crypto_fundamental", "earnings_event", "analyst_estimate"}.issubset(
        {row["source_kind"] for row in source_items}
    )
    sec_item = next(row for row in source_items if row.get("source_id") == "sec_annual_reports_10k")
    assert sec_item["title"].startswith("NVDA 10-K")

    expected = {
        "NVDA": ("sec_annual_reports_10k", "fundamental"),
        "BTC-USD": ("coingecko", "fundamental"),
        "COIN": ("yfinance", "earnings_event"),
        "AMD": ("yfinance", "analyst_estimate"),
        "RSCH": ("sec_disclosures", "filing"),
    }
    for symbol, (source_id, signal_type) in expected.items():
        signal = row_for_symbol(source_signals, symbol)
        assert signal["source_id"] == source_id
        assert signal["signal_type"] == signal_type
        assert signal["evidence_refs"]
        assert signal["catalysts"]
        assert signal["risks"]
        assert signal["invalidation"]

    source_consensus = require_rows(panel, "source_consensus")
    assert any(row.get("source_id") == "sec_annual_reports_10k" for row in source_consensus)
    assert not any(row.get("recommendation") == "candidate_source" for row in source_consensus)


def test_mungermode_benchmark_sources_are_registry_covered(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    panel = load_panel_data(config_for(db_path))
    registry_names = {str(row.get("source_name") or "").casefold() for row in panel["tables"]["sources"]}
    benchmark_names = {row["source_name"].casefold() for row in MUNGERMODE_BENCHMARK_SOURCES}
    definition_names = {row["source_name"].casefold() for row in SOURCE_DEFINITIONS}

    assert len(MUNGERMODE_BENCHMARK_SOURCES) == 37
    assert benchmark_names.issubset(definition_names)
    assert benchmark_names.issubset(registry_names)

    with db(db_path) as con:
        audit = source_ingestion_audit(con)
    assert audit["mungermode_benchmark"]["benchmark_sources"] == 37
    assert audit["mungermode_benchmark"]["missing_sources"] == []


def test_canonical_sources_resync_new_sec_rows_after_cache_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            "INSERT INTO equity_fundamentals VALUES ('NVDA', current_date - 1, current_date - 1, '10-K', ?, 'https://example.com/nvda-10k')",
            [json.dumps({"revenue": 100})],
        )

    first_panel = load_panel_data(config_for(db_path))
    first_signals = require_rows(first_panel, "ticker_source_signals")
    assert row_for_symbol(first_signals, "NVDA")["source_id"] == "sec_annual_reports_10k"

    with db(db_path) as con:
        con.execute(
            "INSERT INTO equity_fundamentals VALUES ('MSFT', current_date, current_date, '10-K', ?, 'https://example.com/msft-10k')",
            [json.dumps({"revenue": 200})],
        )

    second_panel = load_panel_data(config_for(db_path))
    second_signals = require_rows(second_panel, "ticker_source_signals")
    assert row_for_symbol(second_signals, "MSFT")["source_id"] == "sec_annual_reports_10k"


def test_source_ingestion_audit_allows_login_gated_brokers_and_rejects_enabled_empty_source(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            """
            INSERT INTO source_registry
            (source_id, source_name, source_family, source_kind, origin, enabled, ingestion_mode,
             raw_access, source_url, notes, config, created_at, updated_at)
            VALUES ('empty_active', 'Empty Active', 'news', 'rss', 'test', true, 'rss_or_feed',
                    'public_feed', '', '', '{}', now(), now())
            """
        )
        con.execute(
            "INSERT INTO broker_provider_status VALUES ('ibkr', now(), 'disabled', 'disabled', 'IBKR login required locally.', NULL, NULL, NULL, NULL, NULL, '{}', '{}')"
        )
        con.execute(
            """
            INSERT INTO source_registry
            (source_id, source_name, source_family, source_kind, origin, enabled, ingestion_mode,
             raw_access, source_url, notes, config, created_at, updated_at)
            VALUES ('ibkr', 'IBKR', 'broker', 'broker_session', 'test', true, 'login_required',
                    'local_session', '', '', '{}', now(), now())
            """
        )
        con.execute(
            "INSERT INTO source_runs VALUES ('ibkr', 'disabled', 'broker_status', now(), now(), 'disabled', 0, 0, 'IBKR login required locally.', '{}')"
        )
        audit = source_ingestion_audit(con)

    assert audit["status"] == "failed"
    assert any(row["source_id"] == "empty_active" and row["status"] == "not_ingested" for row in audit["source_failures"])
    assert not any(row["source_id"] == "ibkr" for row in audit["source_failures"])
    ibkr = next(row for row in audit["broker_rows"] if row["provider"] == "ibkr")
    moomoo = next(row for row in audit["broker_rows"] if row["provider"] == "moomoo")
    assert ibkr["status"] == "expected_login_required"
    assert moomoo["status"] == "expected_login_required"


def test_source_ingestion_audit_allows_gateway_offline_broker_status(tmp_path: Path) -> None:
    db_path = tmp_path / "audit-offline.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            "INSERT INTO broker_provider_status VALUES ('ibkr', now(), 'gateway_offline', 'degraded', 'IB Gateway is not running locally.', NULL, 'paper', NULL, NULL, NULL, '{}', '{}')"
        )
        audit = source_ingestion_audit(con)

    ibkr = next(row for row in audit["broker_rows"] if row["provider"] == "ibkr")
    assert ibkr["status"] == "expected_login_required"
    assert ibkr["provider_status"] == "gateway_offline"
    assert not any(row.get("provider") == "ibkr" for row in audit["failures"])


def test_source_ingestion_audit_treats_broker_session_failure_as_failure(tmp_path: Path) -> None:
    db_path = tmp_path / "audit-session-failure.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            "INSERT INTO broker_provider_status VALUES ('ibkr', now(), 'session_failure', 'degraded', 'IBKR read-only API session failed.', NULL, 'paper', NULL, NULL, NULL, '{}', '{}')"
        )
        audit = source_ingestion_audit(con)

    ibkr = next(row for row in audit["broker_rows"] if row["provider"] == "ibkr")
    assert ibkr["status"] == "failure"
    assert ibkr["provider_status"] == "session_failure"
    assert any(row.get("provider") == "ibkr" for row in audit["failures"])


@pytest.mark.parametrize(
    ("path", "expected_key"),
    [
        ("/api/decision-readiness", "rows"),
        ("/api/discovered-universe", "rows"),
        ("/api/decision-queue", "rows"),
        ("/api/source-freshness", "rows"),
        ("/api/tickers/NVDA/decision-snapshot", "symbol"),
    ],
)
def test_decision_grade_api_contract_routes_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    expected_key: str,
) -> None:
    db_path = seed_decision_fixture(tmp_path)
    monkeypatch.setattr(api_deps, "load_config", lambda: config_for(db_path))
    client = TestClient(api_main.app)

    response = client.get(path)
    if response.status_code == 404:
        pytest.xfail(f"{path} is pending decision-grade API integration")
    if response.headers.get("content-type", "").startswith("text/html"):
        pytest.xfail(f"{path} is falling through to the frontend shell; API route is pending")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert expected_key in payload
    if expected_key == "rows":
        assert payload["count"] > 0


def seed_decision_fixture(tmp_path: Path) -> Path:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    now = datetime.now(UTC)
    fresh = now.isoformat()
    stale = (now - timedelta(days=10)).isoformat()
    recent_date = now.date().isoformat()
    stale_date = (now - timedelta(days=10)).date().isoformat()

    with db(db_path) as con:
        for symbol, name, source in [
            ("NVDA", "NVIDIA", "config"),
            ("MU", "Micron", "arco"),
            ("MSFT", "Microsoft", "13f"),
            ("SMCI", "Super Micro Computer", "tradingview_screener"),
            ("AAPL", "Apple", "tradingview_news"),
            ("COIN", "Coinbase", "earnings"),
            ("TSLA", "Tesla", "portfolio"),
            ("OLD", "Old Data Corp", "candidate"),
            ("THIN", "Thin Evidence Corp", "candidate"),
            ("ILLIQ", "Illiquid Corp", "candidate"),
            ("RSCH", "Research Grade Corp", "candidate"),
        ]:
            con.execute(
                "INSERT INTO instruments VALUES (?, ?, 'equity', NULL, NULL, 'test', ?)",
                [symbol, name, source],
            )

        for symbol, score, decision, evidence in [
            ("NVDA", 93, "research", ["quote", "thesis", "liquidity", "sepa"]),
            ("OLD", 95, "research", ["old_quote", "old_thesis"]),
            ("THIN", 91, "research", ["single_proxy_valuation"]),
            ("ILLIQ", 88, "research", ["quote", "thesis"]),
            ("RSCH", 82, "research", ["quote", "thesis", "liquidity"]),
        ]:
            con.execute(
                "INSERT INTO candidates VALUES (?, current_date, ?, ?, ?, ?, ?)",
                [
                    f"candidate-{symbol}",
                    symbol,
                    score,
                    json.dumps({"components": {"technical": score}}),
                    json.dumps(evidence),
                    decision,
                ],
            )

        for symbol, observed_at, price, source in [
            ("NVDA", fresh, 1100, "tradingview"),
            ("OLD", stale, 500, "tradingview"),
            ("THIN", fresh, 42, "tradingview"),
            ("ILLIQ", fresh, 3, "tradingview"),
            ("RSCH", fresh, 75, "tradingview"),
        ]:
            con.execute(
                "INSERT INTO quotes_intraday VALUES (?, ?, ?, 1.0, 1.0, 'USD', ?, '{}')",
                [symbol, observed_at, price, source],
            )

        con.execute(
            "INSERT INTO birdclaw_theses VALUES (?, 'MU', 'arco', ?, 'memory bandwidth thesis', '[]', '{}', 'https://example.com/mu')",
            ["thesis-mu", fresh],
        )
        con.execute(
            "INSERT INTO birdclaw_theses VALUES (?, 'NVDA', 'arco', ?, 'AI accelerator thesis', '[]', '{}', 'https://example.com/nvda')",
            ["thesis-nvda", fresh],
        )
        con.execute(
            "INSERT INTO birdclaw_theses VALUES (?, 'OLD', 'arco', ?, 'stale thesis', '[]', '{}', 'https://example.com/old')",
            ["thesis-old", stale],
        )

        con.execute(
            "INSERT INTO disclosures VALUES ('13f-msft', '13f', 'Test 13F', 'Test Filer', 'MSFT', ?, ?, 'HOLDINGS', '$1M', ?, 'https://example.com/13f')",
            [
                recent_date,
                recent_date,
                json.dumps(
                    {
                        "holdings_count": 1,
                        "holdings_value_thousands": 1000,
                        "holdings": [{"symbol": "MSFT", "name": "Microsoft", "value_thousands": 1000}],
                    }
                ),
            ],
        )
        con.execute(
            "INSERT INTO disclosures VALUES ('disc-rsch', 'public_disclosure_transaction', 'Test Trader', 'Test Filer', 'RSCH', ?, ?, 'BUY', '$1M', '{}', 'https://example.com/rsch')",
            [recent_date, recent_date],
        )
        con.execute(
            "INSERT INTO market_screener_rows VALUES ('screen-1', 'SMCI', ?, 'Super Micro Computer', ?, 'tradingview')",
            [fresh, json.dumps({"volume": 2_000_000, "market_cap": 50_000_000_000})],
        )
        con.execute(
            "INSERT INTO news_items VALUES ('news-aapl', ?, 'TradingView', 'Apple catalyst', ?, 'https://example.com/aapl', 'tradingview', '{}')",
            [fresh, json.dumps(["AAPL"])],
        )
        con.execute(
            "INSERT INTO earnings_events VALUES ('COIN', ?, 'earnings', ?, 'yfinance')",
            [recent_date, json.dumps({"source": "calendar"})],
        )
        con.execute(
            "INSERT INTO analyst_estimates VALUES ('NVDA', ?, ?, 'yfinance')",
            [recent_date, json.dumps({"recommendation": "buy"})],
        )
        con.execute("INSERT INTO portfolio_positions VALUES ('TSLA', 2, 175, ?, 'existing position')", [recent_date])

        for symbol, as_of, grade, adv, dollars in [
            ("NVDA", recent_date, "A", 20_000_000, 20_000_000_000),
            ("OLD", stale_date, "A", 10_000_000, 5_000_000_000),
            ("THIN", recent_date, "B", 1_000_000, 42_000_000),
            ("ILLIQ", recent_date, "F", 1_000, 3_000),
            ("RSCH", recent_date, "A", 4_000_000, 300_000_000),
        ]:
            con.execute(
                "INSERT INTO liquidity_metrics VALUES (?, ?, ?, ?, ?, 0.1, 0.0, 1.0, '{}')",
                [symbol, as_of, grade, adv, dollars],
            )
            con.execute(
                "INSERT INTO sepa_analyses VALUES (?, ?, 80, 'stage-2', 'constructive', '{}', '{}')",
                [symbol, as_of],
            )

        con.execute(
            "INSERT INTO valuation_models VALUES ('THIN', ?, 'proxy_low_confidence', 100, 120, '{}', ?)",
            [recent_date, json.dumps({"confidence": "low"})],
        )

        for source, checked_at, status, detail in [
            ("tradingview:NVDA", fresh, "ok", "fresh quote"),
            ("tradingview:OLD", stale, "ok", "stale quote"),
            ("yfinance:provider-run", stale, "failed", "calendar fetch failed"),
            ("docs/data-sources.md", fresh, "ok", "documentation row"),
        ]:
            con.execute("INSERT INTO source_health VALUES (?, ?, ?, ?, NULL)", [source, checked_at, status, detail])
        con.execute(
            "INSERT INTO provider_runs VALUES ('run-yf', 'yfinance', 'earnings', ?, ?, 'failed', 'calendar fetch failed', '{}')",
            [stale, stale],
        )
    return db_path


def config_for(db_path: Path) -> dict[str, Any]:
    return {
        "database": {"duckdb_path": str(db_path)},
        "watchlist": [{"symbol": "NVDA", "name": "NVIDIA", "asset_class": "equity"}],
    }


def require_rows(panel: dict[str, Any], table_name: str) -> list[dict[str, Any]]:
    if table_name not in panel.get("tables", {}):
        missing = ", ".join(sorted(DECISION_TABLES - set(panel.get("tables", {}))))
        pytest.xfail(f"decision-grade read models pending backend integration: missing {missing}")
    rows = panel["tables"][table_name]
    if isinstance(rows, dict):
        rows = rows.get("rows", [])
    assert isinstance(rows, list)
    assert rows, f"{table_name} should return rows for the seeded decision fixture"
    return rows


def row_for_symbol(rows: list[dict[str, Any]], symbol: str) -> dict[str, Any]:
    normalized = symbol.upper()
    for row in rows:
        if normalize_symbol(row.get("symbol")) == normalized:
            return row
    raise AssertionError(f"missing row for symbol {symbol}")


def row_for_source(rows: list[dict[str, Any]], source: str) -> dict[str, Any]:
    for row in rows:
        if row.get("source") == source or row.get("source_key") == source:
            return row
    raise AssertionError(f"missing source freshness row for {source}")


def query_all(con: Any, sql: str) -> list[dict[str, Any]]:
    result = con.execute(sql)
    columns = [column[0] for column in result.description]
    return [dict(zip(columns, row, strict=False)) for row in result.fetchall()]


def normalize_symbol(value: Any) -> str:
    return str(value or "").split(":")[-1].upper()


def nonempty_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value)


def contains_gate(row: dict[str, Any], expected: str) -> bool:
    gates = row.get("blocking_gates") or []
    if isinstance(gates, str):
        gates = [gates]
    return any(expected in str(gate).lower() for gate in gates)
