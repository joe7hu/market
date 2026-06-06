from __future__ import annotations

import json
from datetime import date, timedelta

from investment_panel.core.db import db, init_db
from investment_panel.core.panel import (
    disclosures,
    feed_signals,
    liquidity,
    market_environment_assets,
    market_environment_model,
    market_valuation_reference_charts,
    market_valuation_charts,
    ownership_consensus,
    quotes,
    screener,
    sepa,
    source_consensus,
    source_ticker_ranking_rows,
    technicals,
    universe_screen,
    valuations,
)
from investment_panel.analysis.market_environment import parse_fred_ten_year_yield_csv, parse_fullstack_market_model_csv, parse_history_of_market_forward_pe_json, parse_multpl_valuation_table


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


def test_watchlist_supporting_read_models_cover_large_universe(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        for index in range(205):
            symbol = f"T{index:03d}"
            con.execute("INSERT INTO prices_daily VALUES (?, '2026-05-14', 90, 100, 80, 100, 1000, 'test')", [symbol])
            con.execute("INSERT INTO prices_daily VALUES (?, '2026-05-15', 110, 120, 100, 120, 1000, 'test')", [symbol])
            con.execute(
                "INSERT INTO technical_features VALUES (?, '2026-05-15', ?)",
                [symbol, json.dumps({"close": 120, "return_20d": 0.1, "return_60d": 0.2, "technical_score": 70})],
            )
            con.execute(
                "INSERT INTO market_screener_rows VALUES ('run-1', ?, '2026-05-15T20:00:00Z', ?, ?, 'test')",
                [symbol, symbol, json.dumps({"market_cap": index + 1})],
            )

        quote_rows = quotes(con)
        technical_rows = technicals(con)
        screener_rows = screener(con)

    assert len(quote_rows) == 205
    assert len(technical_rows) == 205
    assert len(screener_rows) == 205
    assert "T204" in {row["symbol"] for row in quote_rows}
    assert "T204" in {row["symbol"] for row in technical_rows}
    assert "T204" in {row["symbol"] for row in screener_rows}
    latest_technical = next(row for row in technical_rows if row["symbol"] == "T204")
    assert round(latest_technical["return_ytd"], 6) == 0.2
    assert round(latest_technical["return_1y"], 6) == 0.2
    assert latest_technical["chart_1y"] == [100.0, 120.0]
    assert latest_technical["rs_1m_bars"] == [0.0, 100.0]


def test_screener_keeps_latest_row_per_symbol_not_only_latest_run(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            "INSERT INTO market_screener_rows VALUES ('run-1', 'ORCL', '2026-05-31T20:00:00Z', 'Oracle', ?, 'yfinance_info')",
            [json.dumps({"market_cap": 100})],
        )
        con.execute(
            "INSERT INTO market_screener_rows VALUES ('run-2', 'CRCL', '2026-05-31T20:01:00Z', 'Circle', ?, 'yfinance_info')",
            [json.dumps({"market_cap": 200})],
        )
        con.execute(
            "INSERT INTO market_screener_rows VALUES ('run-3', 'ORCL', '2026-05-31T19:00:00Z', 'Oracle old', ?, 'yfinance_info')",
            [json.dumps({"market_cap": 50})],
        )

        rows = screener(con)

    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["ORCL"]["metrics"]["market_cap"] == 100
    assert by_symbol["CRCL"]["metrics"]["market_cap"] == 200


def test_technicals_keep_full_one_year_chart_as_compact_points(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    start = date(2025, 1, 1)
    with db(db_path) as con:
        for offset in range(253):
            close = 100 + offset
            day = (start + timedelta(days=offset)).isoformat()
            con.execute("INSERT INTO prices_daily VALUES ('FULL', ?, ?, ?, ?, ?, 1000, 'test')", [day, close, close, close, close])
        con.execute(
            "INSERT INTO technical_features VALUES ('FULL', '2025-09-10', ?)",
            [json.dumps({"close": 352, "return_20d": 0.1, "return_60d": 0.2, "technical_score": 70})],
        )

        row = next(row for row in technicals(con) if row["symbol"] == "FULL")

    assert len(row["chart_1y"]) == 253
    assert row["chart_1y"][:3] == [100.0, 101.0, 102.0]
    assert row["chart_1y"][-3:] == [350.0, 351.0, 352.0]
    assert "price_history_1y" not in row


def test_technicals_include_watchlist_volume_and_atr_context(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    start = date(2026, 1, 1)
    with db(db_path) as con:
        for offset in range(90):
            close = 100 + offset
            volume = 1000 if offset < 68 else 2000 + offset
            day = (start + timedelta(days=offset)).isoformat()
            con.execute(
                "INSERT INTO prices_daily VALUES ('VOL', ?, ?, ?, ?, ?, ?, 'test')",
                [day, close - 1, close + 3, close - 2, close, volume],
            )
        con.execute(
            "INSERT INTO technical_features VALUES ('VOL', ?, ?)",
            [(start + timedelta(days=89)).isoformat(), json.dumps({"close": 189, "technical_score": 70})],
        )

        row = next(row for row in technicals(con) if row["symbol"] == "VOL")

    assert row["return_3m"] > 0.45
    assert row["rel_volume_1m"] > 2
    assert len(row["volume_bars_1m"]) == 22
    assert row["atr_pct_1m"] > 0
    assert len(row["atr_pct_1m_points"]) == 22
    assert len(row["rs_3m_bars"]) == 63


def test_valuations_include_own_history_expensiveness_percentile(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute("INSERT INTO valuation_models VALUES ('MSFT', '2026-05-13', 'blended', 100, -10, '{}', '{}')")
        con.execute("INSERT INTO valuation_models VALUES ('MSFT', '2026-05-14', 'blended', 110, 10, '{}', '{}')")
        con.execute("INSERT INTO valuation_models VALUES ('MSFT', '2026-05-15', 'blended', 120, 30, '{}', '{}')")

        row = next(row for row in valuations(con) if row["symbol"] == "MSFT")

    assert row["own_history_percentile"] == 0
    assert row["valuation_percentile_own_history"] == 0


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
            [json.dumps({"market_cap": 3000, "total_revenue": 1000, "revenue_growth": 0.12, "net_margin": 0.2, "free_cash_flow": 300})],
        )
        con.execute("INSERT INTO prices_daily VALUES ('MSFT', '2026-05-14', 90, 100, 80, 100, 1000, 'test')")
        con.execute("INSERT INTO prices_daily VALUES ('MSFT', '2026-05-15', 110, 120, 100, 120, 2000, 'test')")
        con.execute(
            "INSERT INTO technical_features VALUES ('MSFT', '2026-05-15', ?)",
            [json.dumps({"close": 120, "return_3m": 0.22, "rel_volume_1m": 1.4, "atr_pct_1m": 0.05})],
        )
        con.execute("INSERT INTO valuation_models VALUES ('MSFT', '2026-05-13', 'blended', 100, -10, '{}', '{}')")
        con.execute("INSERT INTO valuation_models VALUES ('MSFT', '2026-05-15', 'blended', 120, 30, '{}', '{}')")

        rows = universe_screen(con, [{"symbol": "MSFT"}])

    msft = rows[0]
    assert msft["ps_ratio"] == 3
    assert msft["pe_ratio"] == 15
    assert msft.get("forward_pe") is None
    assert msft["roic"] == 20
    assert msft["rev_yoy"] == 0.12
    assert msft["fcf_yield"] == 0.1
    assert msft["fcf_margin"] == 0.3
    assert msft["rs_3m"] == 100
    assert msft["return_3m"] == 0.22
    assert msft["relvol_1m"] == 1.4
    assert msft["atr_pct_1m"] == 0.05
    assert msft["valuation_percentile_own_history"] == 0
    assert msft["forward_pe_source"] == "missing"


def test_universe_screen_keeps_provider_forward_pe_separate_from_current_pe(tmp_path) -> None:
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
            VALUES ('NVDA', 'NVIDIA', 'equity', '["configured watchlist"]', '{"config_watchlist": 1}', now(), now(), NULL, 'eligible', '', 1, 1, 1, 1, 1, true, now())
            """
        )
        con.execute(
            "INSERT INTO market_screener_rows VALUES ('run-1', 'NVDA', now(), 'NVIDIA', ?, 'yfinance_info')",
            [json.dumps({"market_cap": 5000, "total_revenue": 100, "net_margin": 0.5, "trailing_pe": 32.4, "forward_pe": 16.7})],
        )

        rows = universe_screen(con, [{"symbol": "NVDA"}])

    nvda = rows[0]
    assert nvda["pe_ratio"] == 32.4
    assert nvda["forward_pe"] == 16.7
    assert nvda["forward_pe_source"] == "provider"


def test_universe_screen_does_not_render_missing_growth_as_zero(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        for rank, symbol in [(1, "MISS"), (2, "FLAT")]:
            con.execute(
                """
                INSERT INTO discovered_universe (
                    symbol, name, asset_class, inclusion_reasons, source_counts,
                    latest_source_timestamp, latest_observed_at, next_event_at,
                    eligibility_status, eligibility_detail, evidence_score,
                    discovery_score, liquidity_score, recency_score, universe_rank,
                    decision_universe_member, updated_at
                )
                VALUES (?, ?, 'equity', '["configured watchlist"]', '{"config_watchlist": 1}', now(), now(), NULL, 'eligible', '', 1, 1, 1, 1, ?, true, now())
                """,
                [symbol, symbol, rank],
            )
        con.execute(
            "INSERT INTO market_screener_rows VALUES ('run-1', 'MISS', now(), 'Missing Growth', ?, 'yfinance_info')",
            [json.dumps({"market_cap": 1000, "total_revenue": 100, "net_margin": 0.1})],
        )
        con.execute(
            "INSERT INTO market_screener_rows VALUES ('run-1', 'FLAT', now(), 'Flat Growth', ?, 'yfinance_info')",
            [json.dumps({"market_cap": 1000, "total_revenue": 100, "revenue_growth": 0.0, "net_margin": 0.1})],
        )

        rows = universe_screen(con, [{"symbol": "MISS"}, {"symbol": "FLAT"}])

    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["MISS"].get("rev_yoy") is None
    assert by_symbol["MISS"].get("revenue_yoy") is None
    assert by_symbol["FLAT"]["rev_yoy"] == 0.0
    assert by_symbol["FLAT"]["revenue_yoy"] == 0.0


def test_universe_screen_preserves_reported_zero_free_cash_flow(tmp_path) -> None:
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
            VALUES ('ZFCF', 'Zero FCF', 'equity', '["configured watchlist"]', '{"config_watchlist": 1}', now(), now(), NULL, 'eligible', '', 1, 1, 1, 1, 1, true, now())
            """
        )
        con.execute(
            "INSERT INTO market_screener_rows VALUES ('run-1', 'ZFCF', now(), 'Zero FCF', ?, 'yfinance_info')",
            [json.dumps({"market_cap": 1000, "total_revenue": 100, "net_margin": 0.2, "operating_cash_flow": 50, "free_cash_flow": 0})],
        )

        rows = universe_screen(con, [{"symbol": "ZFCF"}])

    row = rows[0]
    assert row["free_cash_flow"] == 0
    assert row["fcf_yield"] == 0
    assert row["fcf_margin"] == 0


def test_market_valuation_charts_include_whole_market_and_watchlist_symbols(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        for symbol, price, fair_value, forward_pe in [("MSFT", 100, 125, 24), ("NVDA", 200, 160, 48)]:
            con.execute("INSERT INTO prices_daily VALUES (?, '2026-05-14', ?, ?, ?, ?, 1000, 'test')", [symbol, price - 5, price + 5, price - 10, price - 2])
            con.execute("INSERT INTO prices_daily VALUES (?, '2026-05-15', ?, ?, ?, ?, 1000, 'test')", [symbol, price - 2, price + 10, price - 4, price])
            con.execute(
                "INSERT INTO market_screener_rows VALUES ('run-1', ?, '2026-05-15T20:00:00Z', ?, ?, 'yfinance_info')",
                [symbol, symbol, json.dumps({"market_cap": price * 1_000_000, "total_revenue": price * 100_000, "forward_pe": forward_pe})],
            )
            con.execute(
                "INSERT INTO valuation_models VALUES (?, '2026-05-15', 'blended_dcf_relative', ?, ?, '{}', '{\"confidence\":\"medium_low\"}')",
                [symbol, fair_value, ((fair_value - price) / price) * 100],
            )

        rows = market_valuation_charts(con, [{"symbol": "MSFT"}, {"symbol": "NVDA"}])

    market = rows[0]
    msft = next(row for row in rows if row["symbol"] == "MSFT")
    nvda = next(row for row in rows if row["symbol"] == "NVDA")
    assert market["symbol"] == "MARKET"
    assert market["component_count"] == 2
    assert len(market["history"]) == 2
    assert msft["valuation_posture"] == "discounted"
    assert nvda["valuation_posture"] == "stretched"
    assert msft["history"][-1]["fair_value"] == 125


def test_parse_fullstack_market_model_csv_extracts_environment_rows() -> None:
    def csv_row(values: dict[int, str]) -> str:
        cells = [""] * 33
        for index, value in values.items():
            cells[index] = f'"{value}"' if "," in value else value
        return ",".join(cells)

    csv_text = "\n".join(
        [
            csv_row({1: "Market"}),
            csv_row({1: "Ticker", 2: "Index"}),
            csv_row({1: "SPY", 2: "SPDR S&P 500 ETF", 3: "590.00", 4: "0.5%", 6: "8.2%", 7: "1.1%", 8: "3.4%", 9: "15.0%", 13: "2.0%", 15: "UP", 16: "UP", 17: "UP", 18: "UP", 19: "UP", 20: "UP", 31: "92.0%", 32: "green"}),
            csv_row({1: "Macro"}),
            csv_row({1: "VIX", 2: "CBOE Volatility Index", 3: "16.5", 4: "-2.0%", 8: "-8.0%", 15: "DOWN", 16: "DOWN", 17: "DOWN", 18: "DOWN", 31: "40.0%", 32: "yellow"}),
            csv_row({1: "May 28, 2026"}),
        ]
    )

    records = parse_fullstack_market_model_csv(csv_text, source_url="https://example.com/model.csv")

    spy = next(record for record in records if record["symbol"] == "SPY")
    vix = next(record for record in records if record["symbol"] == "VIX")
    assert spy["as_of"] == "2026-05-28"
    assert spy["group_name"] == "Market"
    assert spy["return_ytd"] == 8.2
    assert spy["sma_50_gt_200"] is True
    assert vix["group_name"] == "Macro"
    assert vix["sma_20_up"] is False


def test_parse_multpl_valuation_table_extracts_monthly_points() -> None:
    html = """
    <table>
      <tr><th>Date</th><th>Value</th></tr>
      <tr><td>May 29, 2026</td><td><abbr title="Estimate">†</abbr>32.67</td></tr>
      <tr><td>May 1, 2026</td><td>&#x2002;31.42</td></tr>
    </table>
    """

    rows = parse_multpl_valuation_table(html)

    assert rows == [("2026-05-29", 32.67), ("2026-05-01", 31.42)]


def test_parse_history_of_market_forward_pe_json_extracts_points() -> None:
    payload = {
        "forward": [
            {"date": "2026-03-31", "value": 21.54},
            {"date": "2025-12-31", "value": "20.85"},
        ]
    }

    rows = parse_history_of_market_forward_pe_json(payload)

    assert rows == [("2026-03-31", 21.54), ("2025-12-31", 20.85)]


def test_parse_fred_ten_year_yield_csv_uses_latest_monthly_observation() -> None:
    csv_text = "observation_date,DGS10\n2026-05-28,4.42\n2026-05-29,4.40\n2026-06-01,.\n"

    rows = parse_fred_ten_year_yield_csv(csv_text)

    assert rows == {"2026-05": 4.40}


def test_market_reference_and_asset_rows_drive_comprehensive_environment_model(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            """
            INSERT INTO market_valuation_metric_points VALUES
            ('sp500_forward_pe', '2026-05-01', 'S&P 500 Forward P/E', 18.0, 'x', false, 'multpl', 'https://example.com'),
            ('sp500_forward_pe', '2026-05-02', 'S&P 500 Forward P/E', 22.0, 'x', false, 'multpl', 'https://example.com'),
            ('equity_risk_premium', '2026-05-01', 'Equity Risk Premium', 1.5, '%', true, 'multpl', 'https://example.com'),
            ('equity_risk_premium', '2026-05-02', 'Equity Risk Premium', -1.0, '%', true, 'multpl', 'https://example.com'),
            ('sp500_price', '2026-05-01', 'S&P 500 Price', 6000, '', true, 'multpl', 'https://example.com'),
            ('sp500_price', '2026-05-02', 'S&P 500 Price', 6100, '', true, 'multpl', 'https://example.com')
            """
        )
        con.execute(
            """
            INSERT INTO market_environment_asset_snapshots
            (symbol, as_of, group_name, name, price, return_1d, return_ytd, return_1w,
             return_1m, return_1y, pct_from_52w_high, sma_10_up, sma_20_up, sma_50_up,
             sma_200_up, sma_20_gt_50, sma_50_gt_200, range_ratio_52w, color, source, raw)
            VALUES
            ('SPY', '2026-05-02', 'Market', 'S&P 500 ETF', 590, 0.2, 8, 1, 3, 15, 2, true, true, true, true, true, true, 95, 'green', 'fullstack_market_model_sheet', '{}'),
            ('XLK', '2026-05-02', 'Sectors', 'Technology', 240, 0.1, 10, 2, 5, 20, 3, true, true, true, true, true, true, 90, 'green', 'test', '{}'),
            ('VIX', '2026-05-02', 'Macro', 'Volatility', 16, -2, -5, -1, -8, -10, 30, false, false, false, false, false, false, 35, 'yellow', 'test', '{}'),
            ('TLT', '2026-05-02', 'Macro', 'Long Bonds', 92, 0.3, 2, 1, 4, 5, 8, true, true, true, true, true, false, 70, 'green', 'test', '{}')
            """
        )

        reference_rows = market_valuation_reference_charts(con)
        asset_rows = market_environment_assets(con)
        model_rows = market_environment_model(con, [])
        market_only_rows = market_environment_model(con, [], include_exposure=False)

    assert len(reference_rows) == 2
    assert reference_rows[0]["metric"] == "sp500_forward_pe"
    assert reference_rows[0]["percentile"] == 100
    assert reference_rows[0]["score"] == 0
    assert reference_rows[0]["history"][-1]["index_price"] == 6100
    assert len(asset_rows) == 4
    assert asset_rows[0]["source"] == "market_environment_asset_matrix"
    categories = {row["category"]: row for row in model_rows}
    assert categories["Overall"]["source"] == "Weighted environment model"
    assert categories["Valuation"]["source"] == "Multpl valuation tables"
    assert categories["Price Trend"]["source"] == "Market environment asset matrix"
    assert categories["Market Breadth"]["source"] == "Market environment asset matrix"
    assert categories["Risk Appetite"]["score"] is not None
    assert categories["Sector / Theme Leadership"]["score"] is not None
    assert {row["category"] for row in market_only_rows} == {
        "Overall",
        "Valuation",
        "Price Trend",
        "Market Breadth",
        "Risk Appetite",
        "Sector / Theme Leadership",
    }


def test_market_environment_model_scores_valuation_trend_and_risk(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute("INSERT INTO prices_daily VALUES ('MSFT', '2026-05-14', 90, 100, 80, 100, 1000, 'test')")
        con.execute("INSERT INTO prices_daily VALUES ('MSFT', '2026-05-15', 110, 120, 100, 120, 1000, 'test')")
        con.execute(
            "INSERT INTO market_screener_rows VALUES ('run-1', 'MSFT', '2026-05-15T20:00:00Z', 'Microsoft', ?, 'yfinance_info')",
            [json.dumps({"market_cap": 3000, "total_revenue": 1000, "forward_pe": 20})],
        )
        con.execute("INSERT INTO valuation_models VALUES ('MSFT', '2026-05-15', 'blended_dcf_relative', 150, 25, '{}', '{}')")
        con.execute(
            "INSERT INTO technical_features VALUES ('MSFT', '2026-05-15', ?)",
            [json.dumps({"close": 120, "return_60d": 0.12, "technical_score": 72})],
        )
        con.execute("INSERT INTO liquidity_metrics VALUES ('MSFT', '2026-05-15', 'A', 1000, 500000000, 1, 1, 1, '{}')")

        rows = market_environment_model(con, [{"symbol": "MSFT"}])

    overall = rows[0]
    valuation = next(row for row in rows if row["category"] == "Valuation")
    trend = next(row for row in rows if row["category"] == "Price Trend")
    liquidity_row = next(row for row in rows if row["category"] == "Liquidity")
    assert overall["category"] == "Overall"
    assert valuation["posture"] == "constructive"
    assert trend["posture"] == "constructive"
    assert liquidity_row["posture"] == "constructive"
    assert "next_action" in overall


def test_market_environment_model_does_not_treat_missing_breadth_as_defensive(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute("INSERT INTO prices_daily VALUES ('MSFT', '2026-05-15', 110, 120, 100, 120, 1000, 'test')")
        con.execute(
            "INSERT INTO market_screener_rows VALUES ('run-1', 'MSFT', '2026-05-15T20:00:00Z', 'Microsoft', ?, 'yfinance_info')",
            [json.dumps({"market_cap": 3000, "total_revenue": 1000, "forward_pe": 20})],
        )

        rows = market_environment_model(con, [{"symbol": "MSFT"}])

    breadth = next(row for row in rows if row["category"] == "Market Breadth")
    assert "score" not in breadth
    assert breadth["posture"] == "not enough data"


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


def test_source_ticker_rankings_group_raw_signal_evidence(tmp_path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            "INSERT INTO source_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "birdclaw_primary_tweets",
                "Birdclaw primary X/Twitter",
                "private_graph",
                "tweet",
                "market",
                True,
                "mounted_raw",
                "private",
                None,
                None,
                "{}",
                "2026-06-06T00:00:00Z",
                "2026-06-06T00:00:00Z",
            ],
        )
        for item_id, title, observed_at in [
            ("tweet-1", "NVDA and MSFT source item", "2026-06-06T02:00:00Z"),
            ("tweet-2", "NVDA follow-up source item", "2026-06-06T03:00:00Z"),
        ]:
            con.execute(
                "INSERT INTO source_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    item_id,
                    "birdclaw_primary_tweets",
                    "run-1",
                    "tweet",
                    title,
                    f"https://x.com/example/status/{item_id}",
                    "tester",
                    observed_at,
                    observed_at,
                    title,
                    "[]",
                    "[]",
                    "{}",
                    item_id,
                    "private",
                ],
            )
        for signal_id, item_id, symbol, observed_at, sentiment in [
            ("sig-1", "tweet-1", "NVDA", "2026-06-06T02:00:00Z", "bullish"),
            ("sig-2", "tweet-2", "NVDA", "2026-06-06T03:00:00Z", "bearish"),
            ("sig-3", "tweet-1", "MSFT", "2026-06-06T02:00:00Z", "bullish"),
        ]:
            con.execute(
                "INSERT INTO ticker_source_signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    signal_id,
                    item_id,
                    "birdclaw_primary_tweets",
                    symbol,
                    observed_at,
                    "tweet",
                    sentiment,
                    "unknown",
                    0.5,
                    f"{symbol} appeared in source evidence.",
                    "No structured antithesis.",
                    "[]",
                    "[]",
                    "Watch for follow-through.",
                    "[]",
                    True,
                    "{}",
                ],
            )

        rows = source_ticker_ranking_rows(con)

    assert rows[0]["symbol"] == "NVDA"
    assert rows[0]["signal_count"] == 2
    assert rows[0]["source_count"] == 1
    assert rows[0]["bullish_count"] == 1
    assert rows[0]["bearish_count"] == 1
    assert rows[0]["latest_source"] == "Birdclaw primary X/Twitter"
    assert rows[0]["latest_title"] == "NVDA follow-up source item"
    assert rows[1]["symbol"] == "MSFT"


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
