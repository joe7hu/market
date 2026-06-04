from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd

from investment_panel.core import free_sources as free_sources_core
from investment_panel.analysis import run_all_analyses
from investment_panel.analysis.earnings_setup import analyze_earnings_setup
from investment_panel.analysis.options_payoff import OptionLeg, evaluate_strategy
from investment_panel.analysis.valuation import metrics_pass_sanity_checks, store_valuation_models
from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db, query_rows, upsert_instrument
from investment_panel.core.fundamentals import metrics_from_company_facts
from investment_panel.core.free_sources import (
    infer_event_date,
    option_symbols,
    selected_option_expiries,
    store_expiries,
    store_news_rows,
    store_options_chain,
    store_screener_rows,
    store_yfinance_market_snapshot,
    upsert_quote,
)
from investment_panel.core.panel import load_panel_data
from investment_panel.core.prices import upsert_prices
from investment_panel.core.scoring import score_and_store
from investment_panel.core.technicals import compute_and_store
from investment_panel.jobs import update_free_sources
from investment_panel.providers import OpenCliError
from investment_panel.providers.tradingview import TradingViewProvider
from investment_panel.core.options_intelligence import refresh_options_intelligence


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


def fixture_prices(symbol: str, lookback_days: int = 260) -> pd.DataFrame:
    rows = []
    for index in range(lookback_days):
        day = date(2026, 5, 20) - timedelta(days=lookback_days - index)
        if day.weekday() >= 5:
            continue
        close = 100.0 + index
        rows.append(
            {
                "symbol": symbol,
                "date": day,
                "open": close - 1,
                "high": close + 1,
                "low": close - 2,
                "close": close,
                "volume": 1_000_000.0 + index,
                "source": "test_fixture",
            }
        )
    return pd.DataFrame(rows)


def test_company_facts_uses_latest_revenue_across_fallback_tags() -> None:
    payload = {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            {"fy": 2022, "fp": "FY", "form": "10-K", "end": "2022-01-30", "filed": "2022-03-18", "val": 100},
                        ]
                    }
                },
                "Revenues": {
                    "units": {
                        "USD": [
                            {"fy": 2025, "fp": "FY", "form": "10-K", "end": "2025-01-26", "filed": "2025-02-26", "val": 200},
                            {"fy": 2024, "fp": "FY", "form": "10-K", "end": "2024-01-28", "filed": "2024-02-21", "val": 150},
                        ]
                    }
                },
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            {"fy": 2025, "fp": "FY", "form": "10-K", "end": "2025-01-26", "filed": "2025-02-26", "val": 50},
                        ]
                    }
                },
                "Assets": {"units": {"USD": [{"form": "10-K", "end": "2025-01-26", "filed": "2025-02-26", "val": 500}]}},
                "Liabilities": {"units": {"USD": [{"form": "10-K", "end": "2025-01-26", "filed": "2025-02-26", "val": 100}]}},
                "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [{"form": "10-K", "end": "2025-01-26", "filed": "2025-02-26", "val": 80}]}},
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {"USD": [{"fy": 2025, "fp": "FY", "form": "10-K", "end": "2025-01-26", "filed": "2025-02-26", "val": 70}]}
                },
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {"USD": [{"fy": 2025, "fp": "FY", "form": "10-K", "end": "2025-01-26", "filed": "2025-02-26", "val": 10}]}
                },
            }
        }
    }

    metrics = metrics_from_company_facts(payload)

    assert metrics["period_end"] == "2025-01-26"
    assert metrics["revenue"] == 200
    assert metrics["revenue_prior"] == 150
    assert metrics["net_margin"] == 0.25
    assert metrics["free_cash_flow"] == 60
    assert metrics["fcf_margin"] == 0.3


def test_yfinance_market_snapshot_persists_market_cap_for_valuation(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        stored = store_yfinance_market_snapshot(
            con,
            "run1",
            "COIN",
            "2026-05-20T02:21:00Z",
            {
                "shortName": "Coinbase",
                "marketCap": 50_000_000_000,
                "sharesOutstanding": 250_000_000,
                "regularMarketPrice": 200,
                "previousClose": 195,
                "trailingPE": 32.5,
                "forwardPE": 18.25,
                "pegRatio": 1.4,
                "priceToSalesTrailing12Months": 7.1,
                "priceToBook": 4.2,
                "totalRevenue": 7_000_000_000,
                "revenueGrowth": 0.12,
                "profitMargins": 0.18,
                "freeCashflow": 900_000_000,
                "operatingCashflow": 1_100_000_000,
                "capitalExpenditures": 200_000_000,
                "totalCash": 1_000_000_000,
                "totalDebt": 500_000_000,
                "quoteType": "EQUITY",
            },
        )
        rows = query_rows(con, "SELECT symbol, name, metrics, source FROM market_screener_rows WHERE symbol = 'COIN'")

    assert stored is True
    assert rows[0]["source"] == "yfinance_info"
    metrics = json.loads(rows[0]["metrics"])
    assert metrics["market_cap_basic"] == 50_000_000_000
    assert metrics["shares_outstanding"] == 250_000_000
    assert metrics["trailing_pe"] == 32.5
    assert metrics["forward_pe"] == 18.25
    assert metrics["peg_ratio"] == 1.4
    assert metrics["price_to_sales"] == 7.1
    assert metrics["price_to_book"] == 4.2
    assert metrics["total_revenue"] == 7_000_000_000
    assert metrics["net_margin"] == 0.18
    assert metrics["free_cash_flow"] == 900_000_000
    assert metrics["operating_cash_flow"] == 1_100_000_000
    assert metrics["capital_expenditures"] == 200_000_000


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


def test_default_option_symbols_cover_full_equity_universe(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    config = SimpleNamespace(
        data_sources=SimpleNamespace(tradingview=SimpleNamespace(options_symbols=[])),
        watchlist=[],
    )
    with db(db_path) as con:
        for index in range(15):
            upsert_instrument(con, {"symbol": f"T{index:02d}", "name": f"Ticker {index}", "asset_class": "equity"})

        symbols = option_symbols(con, config)

    assert len(symbols) == 15
    assert symbols[0] == "T00"
    assert symbols[-1] == "T14"


def test_selected_option_expiries_includes_near_term_and_radar_leaps() -> None:
    rows = [
        {"expiry": "2028-11-18", "dte": 900, "contracts_count": 240},
        {"expiry": "2026-06-05", "dte": 3, "contracts_count": 120},
        {"expiry": "2027-06-02", "dte": 365, "contracts_count": 180},
        {"expiry": "2027-12-17", "dte": 563, "contracts_count": 180},
        {"expiry": "2029-01-19", "dte": 962, "contracts_count": 180},
    ]

    expiries = selected_option_expiries(rows, "2026-06-02T15:30:00Z")

    assert expiries == ["2026-06-05", "2027-06-02", "2028-11-18"]


def test_selected_option_expiries_computes_missing_dte() -> None:
    rows = [
        {"expiry": "2028-06-02", "contracts_count": 180},
        {"expiry": "2026-06-05", "contracts_count": 120},
        {"expiry": "2027-06-02", "contracts_count": 180},
    ]

    expiries = selected_option_expiries(rows, "2026-06-02T15:30:00Z")

    assert expiries == ["2026-06-05", "2027-06-02", "2028-06-02"]


def test_tradingview_options_refresh_fetches_radar_leap_expiries(tmp_path: Path, monkeypatch) -> None:
    chain_calls: list[str] = []

    class MultiExpiryOptionsProvider:
        def __init__(self, _runner: object) -> None:
            pass

        def status(self) -> list[dict[str, object]]:
            return [{"connected": False}]

        def quote(self, _symbol: str) -> dict[str, object]:
            return {"symbol": "NASDAQ:TSLA", "close": 100, "currency": "USD"}

        def screener(self, limit: int = 50) -> list[dict[str, object]]:
            return []

        def news(self, symbol: str | None = None, limit: int = 50) -> list[dict[str, object]]:
            return []

        def options_expiries(self, _symbol: str) -> list[dict[str, object]]:
            return [
                {"expiry": "2026-06-05", "dte": 3, "contracts_count": 120},
                {"expiry": "2027-06-02", "dte": 365, "contracts_count": 180},
                {"expiry": "2027-12-17", "dte": 563, "contracts_count": 180},
                {"expiry": "2028-11-18", "dte": 900, "contracts_count": 240},
            ]

        def options_chain(self, _symbol: str, expiry: str | None = None, strikes_around_spot: int = 6) -> list[dict[str, object]]:
            assert expiry is not None
            chain_calls.append(expiry)
            return [{"expiry": expiry, "strike": 120, "type": "call", "bid": 4.8, "ask": 5.2, "mid": 5.0, "iv": 0.42, "delta": 0.31}]

    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    config = SimpleNamespace(
        data_sources=SimpleNamespace(
            opencli=SimpleNamespace(enabled=True, command="opencli", timeout_seconds=1),
            tradingview=SimpleNamespace(
                enabled=True,
                options_symbols=["TSLA"],
                screener_limit=0,
                news_limit=0,
                strikes_around_spot=6,
                personal_surfaces_enabled=False,
            ),
        ),
        watchlist=[],
    )
    monkeypatch.setattr(free_sources_core, "TradingViewProvider", MultiExpiryOptionsProvider)
    with db(db_path) as con:
        upsert_instrument(con, {"symbol": "TSLA", "name": "Tesla", "asset_class": "equity"})

        result = free_sources_core.update_tradingview_sources(con, config)
        rows = query_rows(con, "SELECT expiry, count(*) AS count FROM options_chain GROUP BY expiry ORDER BY expiry")

    assert chain_calls == ["2026-06-05", "2027-06-02", "2028-11-18"]
    assert result["chain_expiries"] == 3
    assert result["chains"] == 3
    assert rows == [
        {"expiry": date(2026, 6, 5), "count": 1},
        {"expiry": date(2027, 6, 2), "count": 1},
        {"expiry": date(2028, 11, 18), "count": 1},
    ]


def test_options_provider_error_does_not_clear_existing_signal(tmp_path: Path, monkeypatch) -> None:
    class ErroringOptionsProvider:
        def __init__(self, _runner: object) -> None:
            pass

        def status(self) -> list[dict[str, object]]:
            return [{"connected": False}]

        def quote(self, _symbol: str) -> dict[str, object]:
            return {}

        def screener(self, limit: int = 50) -> list[dict[str, object]]:
            return []

        def news(self, symbol: str | None = None, limit: int = 50) -> list[dict[str, object]]:
            return []

        def options_expiries(self, _symbol: str) -> list[dict[str, object]]:
            raise OpenCliError("scanner 429")

    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    config = SimpleNamespace(
        data_sources=SimpleNamespace(
            opencli=SimpleNamespace(enabled=True, command="opencli", timeout_seconds=1),
            tradingview=SimpleNamespace(
                enabled=True,
                options_symbols=[],
                screener_limit=0,
                news_limit=0,
                strikes_around_spot=6,
                personal_surfaces_enabled=False,
            ),
        ),
        watchlist=[{"symbol": "TSLA", "asset_class": "equity"}],
    )
    monkeypatch.setattr(free_sources_core, "TradingViewProvider", ErroringOptionsProvider)
    with db(db_path) as con:
        upsert_instrument(con, {"symbol": "TSLA", "name": "Tesla", "asset_class": "equity"})
        con.execute("INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-02T15:30:00Z', 100, 1, 1, 'USD', 'tradingview', '{}')")
        store_expiries(con, "TSLA", "2026-06-02T15:30:00Z", [{"expiry": "2026-06-05", "dte": 3, "contracts_count": 2}])
        store_options_chain(
            con,
            "TSLA",
            "2026-06-02T15:30:00Z",
            [
                {"expiry": "2026-06-05", "strike": 100, "type": "put", "bid": 3.9, "ask": 4.1, "mid": 4, "iv": 0.36, "delta": -0.48},
                {"expiry": "2026-06-05", "strike": 100, "type": "call", "bid": 4.9, "ask": 5.1, "mid": 5, "iv": 0.34, "delta": 0.52},
            ],
        )
        refresh_options_intelligence(con, ["TSLA"], reference_date="2026-06-02")

        result = free_sources_core.update_tradingview_sources(con, config)
        rows = query_rows(con, "SELECT symbol FROM options_ticker_signals WHERE symbol = 'TSLA'")

    assert result["option_error_count"] == 4
    assert rows == [{"symbol": "TSLA"}]


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


def test_valuation_models_use_yfinance_fundamental_fallback(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        for symbol, price, market_cap, revenue in [
            ("LLY", 1000, 900_000_000_000, 45_000_000_000),
            ("MSFT", 420, 3_100_000_000_000, 280_000_000_000),
        ]:
            upsert_instrument(con, {"symbol": symbol, "name": symbol, "asset_class": "equity", "category": "owned-portfolio"})
            upsert_quote(con, symbol, "2026-05-20T12:00:00Z", {"symbol": symbol, "close": price, "change": 0})
            store_yfinance_market_snapshot(
                con,
                f"run-{symbol}",
                symbol,
                "2026-05-20T12:00:00Z",
                {
                    "shortName": symbol,
                    "marketCap": market_cap,
                    "regularMarketPrice": price,
                    "totalRevenue": revenue,
                    "revenueGrowth": 0.1,
                    "profitMargins": 0.2,
                    "totalCash": 10_000_000_000,
                    "totalDebt": 5_000_000_000,
                    "quoteType": "EQUITY",
                },
            )

        assert store_valuation_models(con, ["LLY", "MSFT"]) == 6
        rows = query_rows(con, "SELECT symbol, method, diagnostics FROM valuation_models ORDER BY symbol, method")

    assert {row["symbol"] for row in rows} == {"LLY", "MSFT"}
    assert any("yfinance_info" in json.loads(row["diagnostics"])["note"] for row in rows if row["method"] == "dcf_base_case")


def test_free_source_rows_and_analyses_round_trip(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        upsert_instrument(con, {"symbol": "NVDA", "name": "NVIDIA", "asset_class": "equity", "category": "ai"})
        upsert_instrument(con, {"symbol": "SPY", "name": "SPY", "asset_class": "etf", "category": "market"})
        upsert_prices(con, fixture_prices("NVDA", 260))
        upsert_prices(con, fixture_prices("SPY", 260))
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


def test_update_free_sources_promotes_universe_before_enrichment(tmp_path: Path, monkeypatch) -> None:
    cfg = SimpleNamespace(
        database=SimpleNamespace(duckdb_path=tmp_path / "investment.duckdb"),
        nas=SimpleNamespace(status_dir=tmp_path / "status"),
        watchlist=[],
    )
    calls: list[str] = []

    monkeypatch.setattr(update_free_sources, "load_config", lambda _path=None: cfg)
    monkeypatch.setattr(update_free_sources.update_equity_data, "run", lambda _path=None: calls.append("equity") or {"status": "ok"})
    monkeypatch.setattr(update_free_sources, "update_tradingview_sources", lambda _con, _config: calls.append("tradingview") or {"status": "ok"})
    monkeypatch.setattr(update_free_sources, "update_yfinance_sources", lambda _con, _config: calls.append("yfinance") or {"status": "ok"})
    monkeypatch.setattr(update_free_sources, "run_all_analyses", lambda _con, _config: calls.append("analysis") or {"status": "ok"})
    monkeypatch.setattr(update_free_sources, "refresh_decision_read_models", lambda _con, _watchlist: calls.append("decision") or {"status": "ok"})

    result = update_free_sources.run("config.yaml")

    assert calls == ["decision", "equity", "tradingview", "decision", "yfinance", "analysis", "decision"]
    assert result["preflight_decision_models"] == {"status": "ok"}
    assert result["post_tradingview_decision_models"] == {"status": "ok"}
    assert result["equity_data"] == {"status": "ok"}
