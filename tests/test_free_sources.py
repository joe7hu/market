from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from investment_panel.analysis import run_all_analyses
from investment_panel.analysis.earnings_setup import analyze_earnings_setup
from investment_panel.analysis.options_payoff import OptionLeg, evaluate_strategy
from investment_panel.analysis.valuation import metrics_pass_sanity_checks, store_valuation_models
from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db, query_rows, upsert_instrument
from investment_panel.core.free_sources import infer_event_date, store_expiries, store_news_rows, store_options_chain, store_screener_rows, upsert_quote
from investment_panel.core.panel import load_panel_data
from investment_panel.core.prices import sample_prices, upsert_prices
from investment_panel.core.scoring import score_and_store
from investment_panel.core.technicals import compute_and_store
from investment_panel.providers.tradingview import TradingViewProvider


class FakeRunner:
    def read_json(self, args: list[str]) -> Any:
        command = " ".join(args)
        if "quote" in command:
            return [{"symbol": "NASDAQ:NVDA", "close": 200, "change": 1.5, "change_abs": 3, "currency": "USD", "time": "2026-05-10T12:00:00Z"}]
        if "options-expiries" in command:
            return [{"expiry": "2026-06-19", "dte": 40, "contracts_count": 120}]
        if "options-chain" in command:
            return [{"expiry": "2026-06-19", "strike": 200, "type": "call", "bid": 10, "ask": 11, "mid": 10.5, "iv": 0.4, "delta": 0.5}]
        if "screener" in command:
            return [{"symbol": "NASDAQ:NVDA", "name": "NVIDIA", "close": 200, "volume": 1000}]
        if "news" in command:
            return [{"id": "n1", "published": "2026-05-10T12:00:00Z", "provider": "TV", "title": "NVIDIA headline", "related_symbols": ["NASDAQ:NVDA"], "link": "https://example.com"}]
        if "search" in command:
            return [{"symbol": "NASDAQ:NVDA", "description": "NVIDIA Corp", "type": "stock", "exchange": "NASDAQ", "country": "US", "currency": "USD"}]
        if "watchlists" in command:
            return [{"id": "wl1", "name": "AI", "symbol_count": 1, "symbols": ["NASDAQ:NVDA"]}]
        if "alerts" in command:
            return [{"id": "a1", "name": "NVDA alert", "symbol": "NASDAQ:NVDA", "type": "price", "condition": "crossing", "value": 210, "active": True, "status": "active"}]
        if "chart-state" in command:
            return [{"layout_id": "layout1", "symbol": "NASDAQ:NVDA", "interval": "1D", "url": "https://www.tradingview.com/chart/"}]
        if "status" in command:
            return [{"connected": True, "tabs": []}]
        return []


def test_tradingview_provider_interface_uses_json_runner() -> None:
    provider = TradingViewProvider(FakeRunner())

    assert provider.quote("NVDA")["close"] == 200
    assert provider.options_expiries("NVDA")[0]["expiry"] == "2026-06-19"
    assert provider.options_chain("NVDA", "2026-06-19")[0]["mid"] == 10.5
    assert provider.screener(limit=1)[0]["symbol"] == "NASDAQ:NVDA"
    assert provider.news("NVDA")[0]["id"] == "n1"
    assert provider.search("NVDA")[0]["exchange"] == "NASDAQ"
    assert provider.watchlists()[0]["name"] == "AI"
    assert provider.alerts()[0]["id"] == "a1"
    assert provider.chart_state()[0]["interval"] == "1D"


def test_infer_event_date_prioritizes_earnings_over_dividends() -> None:
    assert (
        infer_event_date(
            {
                "calendar": {
                    "Ex-Dividend Date": "1995-04-26",
                    "Earnings Date": ["2026-08-04"],
                }
            }
        )
        == "2026-08-04"
    )


def test_valuation_sanity_checks_reject_implausible_margins() -> None:
    assert metrics_pass_sanity_checks(0.2, 0.18)
    assert not metrics_pass_sanity_checks(0.61, 4.46)


def test_options_payoff_engine_computes_breakeven_and_bounded_loss() -> None:
    scenario = evaluate_strategy(
        "NVDA",
        "long_call",
        200,
        [OptionLeg(option_type="call", side="long", strike=200, premium=10, dte=30, iv=0.4)],
    )

    assert scenario["max_profit"] is None
    assert scenario["max_loss"] == -1000
    assert any(abs(value - 210) < 5 for value in scenario["breakevens"])
    assert scenario["diagnostics"]["pricing_model"] == "black_scholes"


def test_earnings_setup_scores_revision_surprise_spread_and_sentiment() -> None:
    setup = analyze_earnings_setup(
        {
            "eps_trend": [{"current": 2.2, "30daysAgo": 2.0}],
            "eps_revisions": [{"upLast30days": 8, "downLast30days": 2}],
            "earnings_estimate": [{"avg": 2.2, "low": 2.1, "high": 2.3, "numberOfAnalysts": 24}],
            "analyst_price_targets": {"current": 200, "mean": 240},
        },
        {"earnings_history": [{"epsEstimate": 2.0, "epsActual": 2.2, "surprisePercent": 10}]},
    )

    assert setup["score"] > 70
    assert setup["verdict"] == "positive_revision_setup"
    assert setup["metrics"]["revision"]["average_revision_up_ratio"] == 0.8


def test_valuation_models_include_dcf_relative_and_blend(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        upsert_instrument(con, {"symbol": "NVDA", "name": "NVIDIA", "asset_class": "equity", "category": "ai"})
        upsert_instrument(con, {"symbol": "AMD", "name": "AMD", "asset_class": "equity", "category": "ai"})
        upsert_quote(con, "NVDA", "2026-05-10T12:00:00Z", {"symbol": "NASDAQ:NVDA", "close": 200, "change": 0})
        upsert_quote(con, "AMD", "2026-05-10T12:00:00Z", {"symbol": "NASDAQ:AMD", "close": 100, "change": 0})
        store_screener_rows(
            con,
            "run1",
            "2026-05-10T12:00:00Z",
            [
                {"symbol": "NASDAQ:NVDA", "name": "NVIDIA", "market_cap_basic": 500_000_000_000},
                {"symbol": "NASDAQ:AMD", "name": "AMD", "market_cap_basic": 200_000_000_000},
            ],
        )
        for symbol, revenue, growth, margin, cash, liabilities in [
            ("NVDA", 100_000_000_000, 0.25, 0.25, 20_000_000_000, 10_000_000_000),
            ("AMD", 40_000_000_000, 0.15, 0.15, 5_000_000_000, 4_000_000_000),
        ]:
            con.execute(
                """
                INSERT INTO equity_fundamentals
                VALUES (?, current_date, current_date, '10-K', ?, 'https://example.com')
                """,
                [
                    symbol,
                    json.dumps(
                        {
                            "status": "ok",
                            "revenue": revenue,
                            "revenue_growth": growth,
                            "net_margin": margin,
                            "cash": cash,
                            "liabilities": liabilities,
                        }
                    ),
                ],
            )

        assert store_valuation_models(con, ["NVDA", "AMD"]) == 6
        rows = query_rows(con, "SELECT method, fair_value, upside_pct FROM valuation_models WHERE symbol = 'NVDA'")

    methods = {row["method"] for row in rows}
    assert {"dcf_base_case", "relative_revenue_multiple", "blended_dcf_relative"} == methods
    assert all(row["fair_value"] > 0 for row in rows)


def test_free_source_rows_and_analyses_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        upsert_instrument(con, {"symbol": "NVDA", "name": "NVIDIA", "asset_class": "equity", "category": "ai"})
        upsert_instrument(con, {"symbol": "SPY", "name": "SPY", "asset_class": "etf", "category": "market"})
        upsert_prices(con, sample_prices("NVDA", 260))
        upsert_prices(con, sample_prices("SPY", 260))
        compute_and_store(con, "NVDA")
        compute_and_store(con, "SPY")
        upsert_quote(con, "NVDA", "2026-05-10T12:00:00Z", FakeRunner().read_json(["tradingview", "quote"])[0])
        store_screener_rows(
            con,
            "run1",
            "2026-05-10T12:00:00Z",
            [{"symbol": "NASDAQ:NVDA", "name": "NVIDIA", "close": 200, "volume": 1000, "market_cap_basic": 500_000_000_000}],
        )
        store_expiries(con, "NVDA", "2026-05-10T12:00:00Z", FakeRunner().read_json(["tradingview", "options-expiries"]))
        store_options_chain(
            con,
            "NVDA",
            "2026-05-10T12:00:00Z",
            [
                {"expiry": "2026-06-19", "strike": 190, "type": "put", "bid": 8, "ask": 9, "mid": 8.5, "iv": 0.42, "delta": -0.45},
                {"expiry": "2026-06-19", "strike": 200, "type": "put", "bid": 10, "ask": 11, "mid": 10.5, "iv": 0.4, "delta": -0.5},
                {"expiry": "2026-06-19", "strike": 200, "type": "call", "bid": 10, "ask": 11, "mid": 10.5, "iv": 0.4, "delta": 0.5},
                {"expiry": "2026-06-19", "strike": 210, "type": "call", "bid": 7, "ask": 8, "mid": 7.5, "iv": 0.39, "delta": 0.4},
            ],
        )
        store_news_rows(con, FakeRunner().read_json(["tradingview", "news"]), "tradingview")
        con.execute(
            "INSERT INTO analyst_estimates VALUES ('NVDA', current_date, ?, 'yfinance')",
            [
                '{"eps_trend":[{"current":2.2,"30daysAgo":2.0}],"eps_revisions":[{"upLast30days":8,"downLast30days":2}],"earnings_estimate":[{"avg":2.2,"low":2.1,"high":2.3,"numberOfAnalysts":24}],"analyst_price_targets":{"current":200,"mean":240}}'
            ],
        )
        con.execute(
            "INSERT INTO earnings_events VALUES ('NVDA', current_date, 'earnings', ?, 'yfinance')",
            ['{"earnings_history":[{"epsEstimate":2.0,"epsActual":2.2,"surprisePercent":10}]}'],
        )
        result = run_all_analyses(con, load_config())
        score_and_store(con, ["NVDA"], load_config().scoring.weights)

        assert result["sepa_rows"] >= 1
        assert result["liquidity_rows"] >= 1
        assert result["correlation_runs"] >= 1
        assert result["earnings_setups"] >= 1
        assert result["options_payoff_scenarios"] >= 1
        assert query_rows(con, "SELECT * FROM quotes_intraday WHERE symbol = 'NVDA'")
        assert query_rows(con, "SELECT * FROM options_chain WHERE symbol = 'NVDA'")
        assert query_rows(con, "SELECT * FROM options_payoff_scenarios WHERE symbol = 'NVDA'")
        assert query_rows(con, "SELECT * FROM earnings_setups WHERE symbol = 'NVDA'")
        assert query_rows(con, "SELECT * FROM news_items WHERE id = 'n1'")

    panel = load_panel_data({"database": {"duckdb_path": str(db_path)}})
    tables = panel["tables"]
    assert tables["opportunities_ranked"][0]["symbol"] == "NVDA"
    assert tables["opportunity_sources"]
    assert tables["technicals"][0]["technical_score"] is not None
    assert tables["research_packets"][0]["symbol"] == "NVDA"
    assert tables["options_payoff_scenarios"]
    assert tables["earnings_setups"]
