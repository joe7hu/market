from __future__ import annotations

import json

from investment_panel.core.db import db, init_db
from investment_panel.core.panel import disclosures, feed_signals, liquidity, ownership_consensus, quotes, sepa, source_consensus, universe_screen


def test_13f_disclosures_include_allocation_and_filing_history(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    first_raw = {
        "holdings_count": 2,
        "holdings_value_thousands": 1000,
        "holdings": [
            {"symbol": "AAA", "name": "AAA Inc", "value_thousands": 700, "shares_or_principal_amount": 70},
            {"symbol": "BBB", "name": "BBB Inc", "value_thousands": 300, "shares_or_principal_amount": 30},
        ],
    }
    second_raw = {
        "holdings_count": 2,
        "holdings_value_thousands": 2000,
        "holdings": [
            {"symbol": "BBB", "name": "BBB Inc", "value_thousands": 1200, "shares_or_principal_amount": 120},
            {"symbol": "AAA", "name": "AAA Inc", "value_thousands": 800, "shares_or_principal_amount": 80},
        ],
    }
    with db(db_path) as con:
        con.execute(
            "INSERT INTO disclosures VALUES (?, '13f', ?, ?, NULL, ?, ?, 'HOLDINGS', ?, ?, ?)",
            ["first", "Test 13F", "Test Filer", "2025-03-31", "2025-05-15", "1000", json.dumps(first_raw), "https://example.com/first"],
        )
        con.execute(
            "INSERT INTO disclosures VALUES (?, '13f', ?, ?, NULL, ?, ?, 'HOLDINGS', ?, ?, ?)",
            ["second", "Test 13F", "Test Filer", "2025-06-30", "2025-08-14", "2000", json.dumps(second_raw), "https://example.com/second"],
        )

        rows = disclosures(con)

    latest = next(row for row in rows if row["trader_name"] == "Test 13F" and str(row["event_date"]) == "2025-06-30")
    assert [holding["symbol"] for holding in latest["holding_sample"]] == ["BBB", "AAA"]
    assert [round(holding["weight"], 1) for holding in latest["holding_sample"]] == [60.0, 40.0]
    assert len(latest["allocation_history"]) == 2
    assert latest["allocation_history"][0]["symbol"] == "BBB"
    assert round(latest["allocation_history"][0]["weight_before"], 1) == 30.0
    assert round(latest["allocation_history"][0]["weight_after"], 1) == 60.0
    assert [point["date"] for point in latest["portfolio_history"]] == ["2025-03-31", "2025-06-30"]
    assert [point["value"] for point in latest["portfolio_history"]] == [1000.0, 2000.0]


def test_quotes_prefer_previous_close_when_intraday_is_stale(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute("INSERT INTO quotes_intraday VALUES ('NVDA', '2026-05-10T14:30:00Z', 500, 1.0, 5.0, 'USD', 'tradingview', '{}')")
        con.execute("INSERT INTO prices_daily VALUES ('NVDA', '2026-05-14', 90, 100, 80, 100, 1000, 'yahoo-chart')")
        con.execute("INSERT INTO prices_daily VALUES ('NVDA', '2026-05-15', 110, 120, 100, 120, 1000, 'yahoo-chart')")
        con.execute("INSERT INTO source_freshness VALUES ('tradingview:NVDA', 'intraday_quote', 'tradingview', '2026-05-10T14:30:00Z', 'stale', '4 market hours', 'ok', '', false, current_timestamp)")
        con.execute("INSERT INTO source_freshness VALUES ('previous_close:NVDA', 'closing_quote', 'yahoo-chart', '2026-05-15', 'fresh', 'previous close while market is closed', 'ok', '', false, current_timestamp)")

        rows = quotes(con)

    assert rows[0]["symbol"] == "NVDA"
    assert rows[0]["price"] == 120
    assert rows[0]["source"] == "previous_close:yahoo-chart"


def test_quotes_use_newest_stale_quote_when_no_fresh_source_exists(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute("INSERT INTO quotes_intraday VALUES ('NVDA', '2026-05-10T14:30:00Z', 500, 1.0, 5.0, 'USD', 'tradingview', '{}')")
        con.execute("INSERT INTO prices_daily VALUES ('NVDA', '2026-05-14', 90, 100, 80, 100, 1000, 'yahoo-chart')")
        con.execute("INSERT INTO prices_daily VALUES ('NVDA', '2026-05-15', 110, 120, 100, 120, 1000, 'yahoo-chart')")
        con.execute("INSERT INTO source_freshness VALUES ('tradingview:NVDA', 'intraday_quote', 'tradingview', '2026-05-10T14:30:00Z', 'stale', '4 market hours', 'ok', '', false, current_timestamp)")
        con.execute("INSERT INTO source_freshness VALUES ('previous_close:NVDA', 'closing_quote', 'yahoo-chart', '2026-05-15', 'stale', 'previous close while market is closed', 'ok', '', false, current_timestamp)")

        rows = quotes(con)

    assert rows[0]["symbol"] == "NVDA"
    assert rows[0]["price"] == 120
    assert rows[0]["freshness_status"] == "stale"


def test_analysis_read_models_return_current_row_per_symbol(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute("INSERT INTO sepa_analyses VALUES ('NVDA', '2026-05-10', 99, 'old-high', 'old', '{}', '{}')")
        con.execute("INSERT INTO sepa_analyses VALUES ('NVDA', '2026-05-19', 50, 'current', 'current', '{}', '{}')")
        con.execute("INSERT INTO liquidity_metrics VALUES ('NVDA', '2026-05-10', 'A', 1000, 999999999, 1, 1, 1, '{}')")
        con.execute("INSERT INTO liquidity_metrics VALUES ('NVDA', '2026-05-19', 'B', 1000, 100, 1, 1, 1, '{}')")

        sepa_rows = sepa(con)
        liquidity_rows = liquidity(con)

    assert len([row for row in sepa_rows if row["symbol"] == "NVDA"]) == 1
    assert sepa_rows[0]["as_of"].isoformat() == "2026-05-19"
    assert sepa_rows[0]["stage"] == "current"
    assert len([row for row in liquidity_rows if row["symbol"] == "NVDA"]) == 1
    assert liquidity_rows[0]["as_of"].isoformat() == "2026-05-19"
    assert liquidity_rows[0]["grade"] == "B"


def test_universe_screen_derives_value_quality_metrics_from_loaded_fundamentals(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            """
            INSERT INTO discovered_universe (
                symbol, name, asset_class, inclusion_reasons, source_counts,
                latest_source_timestamp, latest_observed_at, next_event_at,
                eligibility_status, eligibility_detail, evidence_score,
                discovery_score, liquidity_score, recency_score, universe_rank,
                decision_universe_member, updated_at
            )
            VALUES ('MSFT', 'Microsoft', 'equity', '["configured watchlist"]', '{"config_watchlist": 1}', now(), now(), NULL, 'eligible', '', 1, 1, 1, 1, 1, true, now())
            """
        )
        con.execute(
            "INSERT INTO market_screener_rows VALUES ('run-1', 'MSFT', now(), 'Microsoft', ?, 'yfinance_info')",
            [json.dumps({"market_cap": 3000, "total_revenue": 1000, "net_margin": 0.2})],
        )

        rows = universe_screen(con, [{"symbol": "MSFT"}])

    msft = rows[0]
    assert msft["forward_pe"] == 15
    assert msft["roic"] == 20
    assert msft["forward_pe_source"] == "fundamental_proxy"


def test_source_consensus_builds_per_source_ticker_history(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            "INSERT INTO news_items VALUES ('n1', '2026-05-29T12:00:00Z', 'Reuters', 'Nvidia shares rally after guidance raise', ?, 'https://example.com/n1', 'tradingview', '{}')",
            [json.dumps(["NASDAQ:NVDA"])],
        )
        con.execute(
            "INSERT INTO birdclaw_theses VALUES ('t1', 'MU', 'ArcoSource', '2026-05-28T12:00:00Z', 'Memory thesis', ?, '{}', 'https://x.com/source')",
            [json.dumps({"text": "HBM cycle thesis", "evidence": [{"text": "primary claim"}]})],
        )

        rows = source_consensus(con)

    reuters = next(row for row in rows if row["source_name"] == "Reuters")
    assert "NVDA" in reuters["bullish_symbols"]
    assert reuters["ticker_history"][0]["symbols"] == ["NVDA"]
    arco = next(row for row in rows if row["source_name"] == "ArcoSource")
    assert arco["content_type"] == "private_graph"
    assert arco["bullish_symbols"] == ["MU"]


def test_feed_signals_include_source_items_with_required_decision_fields(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            "INSERT INTO news_items VALUES ('n1', '2026-05-29T12:00:00Z', 'Reuters', 'Microsoft shares rally after AI order win', ?, 'https://example.com/n1', 'tradingview', '{}')",
            [json.dumps(["NASDAQ:MSFT"])],
        )

        rows = feed_signals(con, [{"symbol": "MSFT"}])

    source_card = next(row for row in rows if row["id"] == "news:n1")
    assert source_card["source"] == "Reuters"
    assert source_card["symbols"] == ["MSFT"]
    assert source_card["thesis"]
    assert source_card["antithesis"]
    assert source_card["portfolio_relevance"]
    assert source_card["next_action"]


def test_ownership_consensus_expands_13f_holdings_into_weighted_ticker_rows(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    raw = {
        "holdings_count": 2,
        "holdings_value_thousands": 1000,
        "holdings": [
            {"symbol": "NVDA", "name": "NVIDIA", "value_thousands": 700, "shares_or_principal_amount": 70},
            {"symbol": "MSFT", "name": "Microsoft", "value_thousands": 300, "shares_or_principal_amount": 30},
        ],
    }
    with db(db_path) as con:
        con.execute(
            "INSERT INTO disclosures VALUES ('f1', '13f', 'Test Investor', 'TEST FILER', NULL, '2025-12-31', '2026-02-14', '13F-HR', '1000', ?, 'https://example.com/13f')",
            [json.dumps(raw)],
        )

        rows = ownership_consensus(con)

    nvda = next(row for row in rows if row["symbol"] == "NVDA")
    investor = next(row for row in rows if row.get("source_type") == "investor" and row["investor"] == "Test Investor")
    assert nvda["holders"] == 1
    assert nvda["total_value"] == 700
    assert investor["holdings"] == 2
