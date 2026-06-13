from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from investment_panel.core import free_sources as free_sources_core
from investment_panel.analysis import run_all_analyses
from investment_panel.analysis.earnings_setup import analyze_earnings_setup
from investment_panel.analysis.options_payoff import OptionLeg, evaluate_strategy
from investment_panel.analysis.valuation import metrics_pass_sanity_checks, store_valuation_models
from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db, query_rows, upsert_instrument
from investment_panel.core import fundamentals as fundamentals_core
from investment_panel.core.fundamentals import cik_from_sec_filing_url, metrics_from_company_facts
from investment_panel.core.free_sources import (
    infer_event_date,
    option_chain_strikes_around_spot,
    option_symbols,
    selected_option_expiries,
    store_expiries,
    store_news_rows,
    store_options_chain,
    store_screener_rows,
    store_yfinance_market_snapshot,
    update_yfinance_options_chains,
    update_yfinance_options_liquidity,
    upsert_quote,
)
from investment_panel.core.free_sources import OPTION_RATE_LIMIT_CIRCUIT_BREAKER, _yfinance_enrichment_status
from investment_panel.core.status import write_source_status
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


def test_equity_fundamentals_resolves_cik_for_unconfigured_equity(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    monkeypatch.setattr(
        fundamentals_core,
        "company_tickers",
        lambda _user_agent: {"0": {"ticker": "MSFT", "cik_str": 789019}},
    )
    monkeypatch.setattr(fundamentals_core, "company_facts", lambda cik, _user_agent: {"cik": cik})
    monkeypatch.setattr(
        fundamentals_core,
        "metrics_from_company_facts",
        lambda _payload: {
            "status": "ok",
            "period_end": "2025-06-30",
            "filing_date": "2025-07-30",
            "form_type": "10-K",
            "revenue": 281_724_000_000,
            "revenue_growth": 0.1493,
        },
    )

    with db(db_path) as con:
        rows = fundamentals_core.update_equity_fundamentals(
            con,
            [{"symbol": "MSFT", "asset_class": "equity"}],
            "market-test@example.com",
        )
        stored = query_rows(con, "SELECT symbol, form_type, metrics, source_url FROM equity_fundamentals WHERE symbol = 'MSFT'")

    assert rows == 1
    assert stored[0]["form_type"] == "10-K"
    assert json.loads(stored[0]["metrics"])["revenue_growth"] == 0.1493
    assert stored[0]["source_url"].endswith("CIK0000789019.json")


def test_equity_fundamentals_uses_default_cik_when_sec_ticker_map_is_blocked(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    monkeypatch.setattr(fundamentals_core, "company_tickers", lambda _user_agent: (_ for _ in ()).throw(RuntimeError("403 Forbidden")))
    monkeypatch.setattr(fundamentals_core, "company_facts", lambda cik, _user_agent: {"cik": cik})
    monkeypatch.setattr(
        fundamentals_core,
        "metrics_from_company_facts",
        lambda _payload: {
            "status": "ok",
            "period_end": "2025-06-30",
            "filing_date": "2025-07-30",
            "form_type": "10-K",
            "revenue": 281_724_000_000,
        },
    )

    with db(db_path) as con:
        rows = fundamentals_core.update_equity_fundamentals(
            con,
            [{"symbol": "MSFT", "asset_class": "equity"}],
            "market-test@example.com",
        )
        stored = query_rows(con, "SELECT source_url FROM equity_fundamentals WHERE symbol = 'MSFT'")

    assert rows == 1
    assert stored[0]["source_url"].endswith("CIK0000789019.json")


def test_equity_fundamentals_does_not_store_error_rows(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    monkeypatch.setattr(fundamentals_core, "company_tickers", lambda _user_agent: (_ for _ in ()).throw(RuntimeError("403 Forbidden")))
    monkeypatch.setattr(fundamentals_core, "company_facts", lambda _cik, _user_agent: (_ for _ in ()).throw(RuntimeError("provider down")))

    with db(db_path) as con:
        rows = fundamentals_core.update_equity_fundamentals(
            con,
            [{"symbol": "MSFT", "asset_class": "equity"}],
            "market-test@example.com",
        )
        stored = query_rows(con, "SELECT symbol, metrics FROM equity_fundamentals WHERE symbol = 'MSFT'")

    assert rows == 0
    assert stored == []


def test_equity_fundamentals_resolves_cik_from_yfinance_sec_filings(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)

    class FakeYFinanceProvider:
        def sec_filings(self, _symbol: str) -> list[dict[str, object]]:
            return [
                {
                    "edgarUrl": "https://finance.yahoo.com/sec-filing/AAPL/0001140361-26-023149_320193",
                    "exhibits": {
                        "10-K": "https://cdn.yahoofinance.com/prod/sec-filings/0000320193/000032019325000079/aapl-20250927.htm"
                    },
                }
            ]

    monkeypatch.setattr(fundamentals_core, "company_tickers", lambda _user_agent: (_ for _ in ()).throw(RuntimeError("403 Forbidden")))
    monkeypatch.setattr(fundamentals_core, "YFinanceProvider", FakeYFinanceProvider)
    monkeypatch.setattr(fundamentals_core, "company_facts", lambda cik, _user_agent: {"cik": cik})
    monkeypatch.setattr(
        fundamentals_core,
        "metrics_from_company_facts",
        lambda _payload: {
            "status": "ok",
            "period_end": "2025-09-27",
            "filing_date": "2025-10-31",
            "form_type": "10-K",
            "revenue": 416_161_000_000,
        },
    )

    with db(db_path) as con:
        rows = fundamentals_core.update_equity_fundamentals(
            con,
            [{"symbol": "AAPL", "asset_class": "equity"}],
            "market-test@example.com",
        )
        stored = query_rows(con, "SELECT symbol, source_url FROM equity_fundamentals WHERE symbol = 'AAPL'")

    assert rows == 1
    assert stored[0]["source_url"].endswith("CIK0000320193.json")


def test_cik_from_sec_filing_url_supports_yahoo_url_formats() -> None:
    assert cik_from_sec_filing_url("https://finance.yahoo.com/sec-filing/MSFT/0001193125-26-258667_789019") == "0000789019"
    assert cik_from_sec_filing_url("https://cdn.yahoofinance.com/prod/sec-filings/0001780312/000149315226023915/forms-8.htm") == "0001780312"


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


def test_default_option_symbols_use_ranked_decision_queue_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    config = SimpleNamespace(
        data_sources=SimpleNamespace(tradingview=SimpleNamespace(options_symbols=[], option_scan_limit=12)),
        watchlist=[{"symbol": "KEEP", "asset_class": "equity"}],
    )
    with db(db_path) as con:
        upsert_instrument(con, {"symbol": "KEEP", "name": "Keep", "asset_class": "equity"})
        for index in range(30):
            upsert_instrument(con, {"symbol": f"EQ{index:02d}", "name": f"Equity {index}", "asset_class": "equity"})
        for index in range(20):
            action_grade = "Reject" if index == 0 else "Stale" if index == 1 else "Watch"
            con.execute(
                """
                INSERT INTO decision_queue
                (symbol, as_of, rank, action_grade, score, decision_score, action_score)
                VALUES (?, current_timestamp, ?, ?, ?, ?, ?)
                """,
                [f"DQ{index:02d}", index + 1, action_grade, 100 - index, 90 - index, 80 - index],
            )

        symbols = option_symbols(con, config)

    assert len(symbols) == 12
    assert symbols[0] == "KEEP"
    assert "DQ00" not in symbols
    assert "DQ01" not in symbols
    assert symbols[1:4] == ["DQ02", "DQ03", "DQ04"]
    assert "EQ00" not in symbols


def test_default_option_symbols_include_manual_watchlist_before_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    config = SimpleNamespace(
        data_sources=SimpleNamespace(tradingview=SimpleNamespace(options_symbols=[], option_scan_limit=3)),
        watchlist=[{"symbol": "KEEP", "asset_class": "equity"}],
    )
    with db(db_path) as con:
        con.execute(
            """
            INSERT INTO manual_watchlist (symbol, name, asset_class, notes, created_at, updated_at, watch_state)
            VALUES ('MANUAL', 'Manual', 'equity', NULL, current_timestamp, current_timestamp, 'watched')
            """
        )
        con.execute(
            """
            INSERT INTO manual_watchlist (symbol, name, asset_class, notes, created_at, updated_at, watch_state)
            VALUES ('DROP', 'Drop', 'equity', NULL, current_timestamp, current_timestamp, 'excluded')
            """
        )
        for index in range(6):
            con.execute(
                """
                INSERT INTO decision_queue
                (symbol, as_of, rank, action_grade, score, decision_score, action_score)
                VALUES (?, current_timestamp, ?, 'Watch', ?, ?, ?)
                """,
                [f"DQ{index:02d}", index + 1, 100 - index, 90 - index, 80 - index],
            )

        symbols = option_symbols(con, config)

    assert symbols[:2] == ["KEEP", "MANUAL"]
    assert "DROP" not in symbols
    assert len(symbols) == 3


def test_yfinance_options_refresh_persists_primary_chains(tmp_path: Path) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    config = SimpleNamespace(data_sources=SimpleNamespace(tradingview=SimpleNamespace(strikes_around_spot=2)))

    class FakeYFinanceOptionsProvider:
        def options_expiries(self, symbol: str) -> list[dict[str, object]]:
            assert symbol == "RBLX"
            return [
                {"expiry": "2026-06-19", "dte": 17},
                {"expiry": "2027-06-18", "dte": 381},
                {"expiry": "2028-01-21", "dte": 598},
            ]

        def options_chain(self, symbol: str, expiry: str) -> list[dict[str, object]]:
            assert symbol == "RBLX"
            return [
                {
                    "expiry": expiry,
                    "type": "call",
                    "strike": strike,
                    "bid": 4.0,
                    "ask": 4.5,
                    "mid": 4.25,
                    "last": 4.2,
                    "iv": 0.35,
                    "volume": 12,
                    "open_interest": 140,
                    "symbol": f"RBLX{expiry.replace('-', '')}C{strike}",
                }
                for strike in [70, 80, 90, 100, 110, 120, 130]
            ]

    with db(db_path) as con:
        upsert_instrument(con, {"symbol": "RBLX", "name": "Roblox", "asset_class": "equity"})
        con.execute("INSERT INTO quotes_intraday VALUES ('RBLX', '2026-06-02T20:00:00Z', 100, 1, 1, 'USD', 'test', '{}')")

        result = update_yfinance_options_chains(
            con,
            FakeYFinanceOptionsProvider(),  # type: ignore[arg-type]
            ["RBLX"],
            "2026-06-02T20:05:00Z",
            "run1",
            config,  # type: ignore[arg-type]
        )
        expiries = query_rows(con, "SELECT symbol, source, count(*) AS rows FROM options_expiries GROUP BY symbol, source")
        chains = query_rows(con, "SELECT symbol, source, count(*) AS rows, min(strike) AS min_strike, max(strike) AS max_strike FROM options_chain GROUP BY symbol, source")

    assert result["options_expiries"] == 3
    assert result["options_chain_expiries"] == 3
    assert result["options_chain_symbols"] == 1
    assert result["options_chains"] == 19
    assert expiries == [{"symbol": "RBLX", "source": "yfinance", "rows": 3}]
    assert chains == [{"symbol": "RBLX", "source": "yfinance", "rows": 19, "min_strike": 70.0, "max_strike": 130.0}]


def _liquidity_chain_rows(expiry: str) -> list[dict[str, object]]:
    return [
        {"expiry": expiry, "type": "call", "strike": 100.0, "bid": 4.0, "ask": 4.5, "mid": 4.25, "last": 4.2,
         "iv": 0.35, "symbol": f"X{expiry.replace('-', '')}C100", "contract_symbol": f"X{expiry.replace('-', '')}C100"}
    ]


def test_yfinance_options_liquidity_skips_expired_expiries(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    monkeypatch.setattr(free_sources_core.yfinance_sources, "YFINANCE_OPTION_THROTTLE_SECONDS", 0)
    today = date.today()
    past = (today - timedelta(days=5)).isoformat()
    future = (today + timedelta(days=400)).isoformat()

    class RecordingLiquidityProvider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def options_chain_liquidity(self, symbol: str, expiry: str) -> list[dict[str, object]]:
            self.calls.append((symbol, expiry))
            return _liquidity_chain_rows(expiry)

    provider = RecordingLiquidityProvider()
    with db(db_path) as con:
        store_options_chain(con, "AAPL", "2026-06-11T20:00:00Z", _liquidity_chain_rows(past))
        store_options_chain(con, "AAPL", "2026-06-11T20:00:00Z", _liquidity_chain_rows(future))
        update_yfinance_options_liquidity(con, provider, ["AAPL"], "2026-06-11T20:05:00Z", "run1")  # type: ignore[arg-type]

    # The expired expiry must never reach the upstream provider.
    assert (("AAPL", past)) not in provider.calls
    assert (("AAPL", future)) in provider.calls


def test_yfinance_options_liquidity_circuit_breaks_on_rate_limit(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    monkeypatch.setattr(free_sources_core.yfinance_sources, "YFINANCE_OPTION_THROTTLE_SECONDS", 0)
    future = (date.today() + timedelta(days=400)).isoformat()
    symbols = [f"SYM{i}" for i in range(OPTION_RATE_LIMIT_CIRCUIT_BREAKER + 4)]

    class RateLimitedProvider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def options_chain_liquidity(self, symbol: str, expiry: str) -> list[dict[str, object]]:
            self.calls.append((symbol, expiry))
            raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")

    provider = RateLimitedProvider()
    with db(db_path) as con:
        for symbol in symbols:
            store_options_chain(con, symbol, "2026-06-11T20:00:00Z", _liquidity_chain_rows(future))
        result = update_yfinance_options_liquidity(con, provider, symbols, "2026-06-11T20:05:00Z", "run1")  # type: ignore[arg-type]

    # The breaker stops after N consecutive rate-limited calls instead of grinding
    # the whole universe and deepening the throttle.
    assert len(provider.calls) == OPTION_RATE_LIMIT_CIRCUIT_BREAKER
    assert "options_liquidity_circuit_breaker" in result


def test_yfinance_options_chains_circuit_breaks_on_rate_limit(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    monkeypatch.setattr(free_sources_core.yfinance_sources, "YFINANCE_OPTION_THROTTLE_SECONDS", 0)
    config = SimpleNamespace(data_sources=SimpleNamespace(tradingview=SimpleNamespace(strikes_around_spot=2)))
    symbols = [f"SYM{i}" for i in range(OPTION_RATE_LIMIT_CIRCUIT_BREAKER + 4)]

    class RateLimitedChainsProvider:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def options_expiries(self, symbol: str) -> list[dict[str, object]]:
            self.calls.append(symbol)
            raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")

        def options_chain(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
            return []

    provider = RateLimitedChainsProvider()
    with db(db_path) as con:
        result = update_yfinance_options_chains(con, provider, symbols, "2026-06-02T20:00:00Z", "run1", config)  # type: ignore[arg-type]

    # The chains loop shares Yahoo's limiter with the liquidity job, so it must also
    # stop once saturated rather than bursting the whole universe.
    assert len(provider.calls) == OPTION_RATE_LIMIT_CIRCUIT_BREAKER
    assert "options_chain_circuit_breaker" in result


def test_yfinance_enrichment_status_reflects_real_health() -> None:
    assert _yfinance_enrichment_status({"options_chains": 10, "options_liquidity": 5}) == "ok"
    # Errors with nothing produced -> error (not a green "ok").
    assert _yfinance_enrichment_status({"options_chains": 0, "options_liquidity": 0, "options_liquidity_error_count": 80}) == "error"
    # Errors but some data produced -> partial.
    assert _yfinance_enrichment_status({"options_chains": 10, "options_liquidity_error_count": 3}) == "partial"
    # A tripped circuit breaker is never "ok".
    assert _yfinance_enrichment_status({"market_snapshots": 5, "options_chain_circuit_breaker": "stopped"}) == "partial"


def test_write_source_status_ok_reflects_job_status(tmp_path: Path) -> None:
    config = SimpleNamespace(nas=SimpleNamespace(status_dir=tmp_path))
    ok_path = write_source_status(config, "job-ok", {"status": "ok", "rows": 5})  # type: ignore[arg-type]
    bad_path = write_source_status(config, "job-bad", {"status": "gateway_offline", "errors": ["ibkr_connect_failed"]})  # type: ignore[arg-type]
    assert json.loads(Path(ok_path).read_text())["ok"] is True
    assert json.loads(Path(bad_path).read_text())["ok"] is False


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


def test_option_chain_strikes_around_spot_widens_only_radar_leaps() -> None:
    rows = [
        {"expiry": "2026-06-05", "dte": 3, "contracts_count": 120},
        {"expiry": "2027-06-02", "dte": 365, "contracts_count": 180},
        {"expiry": "2028-11-18", "dte": 900, "contracts_count": 240},
    ]

    near_term = option_chain_strikes_around_spot("2026-06-05", rows, "2026-06-02T15:30:00Z", configured=6)
    leap = option_chain_strikes_around_spot("2027-06-02", rows, "2026-06-02T15:30:00Z", configured=6)
    far_leap = option_chain_strikes_around_spot("2028-11-18", rows, "2026-06-02T15:30:00Z", configured=6)

    assert near_term == 6
    assert leap == 24
    assert far_leap == 24


def test_tradingview_options_refresh_fetches_radar_leap_expiries(tmp_path: Path, monkeypatch) -> None:
    chain_calls: list[tuple[str, int]] = []
    quote_calls: list[str] = []

    class MultiExpiryOptionsProvider:
        def __init__(self, _runner: object) -> None:
            pass

        def status(self) -> list[dict[str, object]]:
            return [{"connected": False}]

        def quote(self, _symbol: str) -> dict[str, object]:
            quote_calls.append(_symbol)
            return {"symbol": "NASDAQ:TSLA", "close": 100, "currency": "USD"}

        def screener(self, limit: int = 50) -> list[dict[str, object]]:
            raise AssertionError("targeted options refresh should not run screener")

        def news(self, symbol: str | None = None, limit: int = 50) -> list[dict[str, object]]:
            raise AssertionError("targeted options refresh should not run news")

        def options_expiries(self, _symbol: str) -> list[dict[str, object]]:
            return [
                {"expiry": "2026-06-05", "dte": 3, "contracts_count": 120},
                {"expiry": "2027-06-02", "dte": 365, "contracts_count": 180},
                {"expiry": "2027-12-17", "dte": 563, "contracts_count": 180},
                {"expiry": "2028-11-18", "dte": 900, "contracts_count": 240},
            ]

        def options_chain(self, _symbol: str, expiry: str | None = None, strikes_around_spot: int = 6) -> list[dict[str, object]]:
            assert expiry is not None
            chain_calls.append((expiry, strikes_around_spot))
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
    monkeypatch.setattr(free_sources_core.tradingview_sources, "TradingViewProvider",MultiExpiryOptionsProvider)
    with db(db_path) as con:
        upsert_instrument(con, {"symbol": "TSLA", "name": "Tesla", "asset_class": "equity"})
        upsert_instrument(con, {"symbol": "NVDA", "name": "NVIDIA", "asset_class": "equity"})

        result = free_sources_core.update_tradingview_sources(con, config, symbols=["TSLA"])
        rows = query_rows(con, "SELECT expiry, count(*) AS count FROM options_chain GROUP BY expiry ORDER BY expiry")

    assert quote_calls == ["TSLA"]
    assert chain_calls == [("2026-06-05", 6), ("2027-06-02", 24), ("2028-11-18", 24)]
    assert result["target_symbols"] == ["TSLA"]
    assert result["chain_expiries"] == 3
    assert result["radar_chain_expiries"] == 2
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
    monkeypatch.setattr(free_sources_core.tradingview_sources, "TradingViewProvider",ErroringOptionsProvider)
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


def test_screener_rate_limit_does_not_abort_option_ingestion(tmp_path: Path, monkeypatch) -> None:
    """A scanner 429 on the discovery screener must not stop option chains.

    The screener is a discovery surface; option chains are the only source of
    fresh radar snapshots. A rate limit on the former should be recorded but
    leave the latter intact.
    """

    class ScreenerRateLimitedProvider:
        def __init__(self, _runner: object) -> None:
            pass

        def status(self) -> list[dict[str, object]]:
            return [{"connected": False}]

        def quote(self, _symbol: str) -> dict[str, object]:
            return {"symbol": "NASDAQ:TSLA", "close": 100, "currency": "USD"}

        def screener(self, limit: int = 50) -> list[dict[str, object]]:
            raise OpenCliError("scanner 429: rate limited")

        def news(self, symbol: str | None = None, limit: int = 50) -> list[dict[str, object]]:
            raise OpenCliError("scanner 429: rate limited")

        def options_expiries(self, _symbol: str) -> list[dict[str, object]]:
            return [{"expiry": "2027-06-02", "dte": 365, "contracts_count": 180}]

        def options_chain(self, _symbol: str, expiry: str | None = None, strikes_around_spot: int = 6) -> list[dict[str, object]]:
            return [{"expiry": expiry, "strike": 120, "type": "call", "bid": 4.8, "ask": 5.2, "mid": 5.0, "iv": 0.42, "delta": 0.31}]

    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    config = SimpleNamespace(
        data_sources=SimpleNamespace(
            opencli=SimpleNamespace(enabled=True, command="opencli", timeout_seconds=1),
            tradingview=SimpleNamespace(
                enabled=True,
                options_symbols=["TSLA"],
                screener_limit=50,
                news_limit=50,
                strikes_around_spot=6,
                personal_surfaces_enabled=False,
            ),
        ),
        watchlist=[{"symbol": "TSLA", "asset_class": "equity"}],
    )
    monkeypatch.setattr(free_sources_core.tradingview_sources, "TradingViewProvider",ScreenerRateLimitedProvider)
    with db(db_path) as con:
        upsert_instrument(con, {"symbol": "TSLA", "name": "Tesla", "asset_class": "equity"})
        result = free_sources_core.update_tradingview_sources(con, config)
        chain_rows = query_rows(con, "SELECT count(*) AS count FROM options_chain")

    assert result["status"] == "ok"  # refresh survived the screener rate limit
    assert "screener_error" in result
    assert "news_error" in result
    assert result["chains"] == 1  # option chains still ingested
    assert chain_rows == [{"count": 1}]


def test_sustained_rate_limit_trips_option_scan_circuit_breaker(tmp_path: Path, monkeypatch) -> None:
    """A saturated upstream limiter must stop the scan early, not crawl the universe."""

    from investment_panel.providers import OpenCliRateLimitError

    attempted: list[str] = []

    class AlwaysRateLimitedProvider:
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

        def options_expiries(self, symbol: str) -> list[dict[str, object]]:
            attempted.append(symbol)
            raise OpenCliRateLimitError("scanner 429: rate limited")

        def options_chain(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return []

    universe = [f"SYM{i}" for i in range(20)]
    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    config = SimpleNamespace(
        data_sources=SimpleNamespace(
            opencli=SimpleNamespace(enabled=True, command="opencli", timeout_seconds=1),
            tradingview=SimpleNamespace(
                enabled=True,
                options_symbols=universe,
                screener_limit=0,
                news_limit=0,
                strikes_around_spot=6,
                personal_surfaces_enabled=False,
            ),
        ),
        watchlist=[],
    )
    monkeypatch.setattr(free_sources_core.tradingview_sources, "TradingViewProvider",AlwaysRateLimitedProvider)
    with db(db_path) as con:
        for symbol in universe:
            upsert_instrument(con, {"symbol": symbol, "name": symbol, "asset_class": "equity"})
        result = free_sources_core.update_tradingview_sources(con, config)

    # The breaker stops after 4 consecutive rate-limited symbols rather than
    # attempting all 20. Each symbol is tried under a few exchange-prefixed
    # candidates, so count distinct base symbols reached.
    base_symbols = {entry.split(":")[-1] for entry in attempted}
    assert "options_circuit_breaker" in result
    assert len(base_symbols) <= 5
    assert len(base_symbols) < len(universe)


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


@pytest.mark.slow
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
    tradingview_symbols: list[str] | None = None
    yfinance_symbols: list[str] | None = None

    def fake_update_tradingview_sources(_con: object, _config: object, **kwargs: object) -> dict[str, str]:
        nonlocal tradingview_symbols
        calls.append("tradingview")
        symbols = kwargs.get("symbols")
        tradingview_symbols = list(symbols) if isinstance(symbols, list) else None
        return {"status": "ok"}

    def fake_update_yfinance_sources(_con: object, _config: object, **kwargs: object) -> dict[str, str]:
        nonlocal yfinance_symbols
        calls.append("yfinance")
        symbols = kwargs.get("symbols")
        yfinance_symbols = list(symbols) if isinstance(symbols, list) else None
        return {"status": "ok"}

    monkeypatch.setattr(update_free_sources, "load_config", lambda _path=None: cfg)
    monkeypatch.setattr(update_free_sources.update_equity_data, "run", lambda _path=None: calls.append("equity") or {"status": "ok"})
    monkeypatch.setattr(update_free_sources, "update_tradingview_sources", fake_update_tradingview_sources)
    monkeypatch.setattr(update_free_sources, "update_yfinance_sources", fake_update_yfinance_sources)
    monkeypatch.setattr(update_free_sources, "run_all_analyses", lambda _con, _config: calls.append("analysis") or {"status": "ok"})
    monkeypatch.setattr(update_free_sources, "refresh_decision_read_models", lambda _con, _watchlist: calls.append("decision") or {"status": "ok"})

    result = update_free_sources.run("config.yaml", symbols=["TSLA", "NVDA"])

    assert calls == ["decision", "equity", "tradingview", "decision", "yfinance", "analysis", "decision"]
    assert tradingview_symbols == ["TSLA", "NVDA"]
    assert yfinance_symbols == ["TSLA", "NVDA"]
    assert result["preflight_decision_models"] == {"status": "ok"}
    assert result["post_tradingview_decision_models"] == {"status": "ok"}
    assert result["equity_data"] == {"status": "ok"}
