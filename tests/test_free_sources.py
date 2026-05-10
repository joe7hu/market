from __future__ import annotations

from pathlib import Path
from typing import Any

from investment_panel.analysis import run_all_analyses
from investment_panel.analysis.valuation import metrics_pass_sanity_checks
from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db, query_rows, upsert_instrument
from investment_panel.core.free_sources import infer_event_date, store_expiries, store_news_rows, store_options_chain, store_screener_rows, upsert_quote
from investment_panel.core.prices import sample_prices, upsert_prices
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
        store_screener_rows(con, "run1", "2026-05-10T12:00:00Z", FakeRunner().read_json(["tradingview", "screener"]))
        store_expiries(con, "NVDA", "2026-05-10T12:00:00Z", FakeRunner().read_json(["tradingview", "options-expiries"]))
        store_options_chain(con, "NVDA", "2026-05-10T12:00:00Z", FakeRunner().read_json(["tradingview", "options-chain"]))
        store_news_rows(con, FakeRunner().read_json(["tradingview", "news"]), "tradingview")
        result = run_all_analyses(con, load_config())

        assert result["sepa_rows"] >= 1
        assert result["liquidity_rows"] >= 1
        assert result["correlation_runs"] >= 1
        assert query_rows(con, "SELECT * FROM quotes_intraday WHERE symbol = 'NVDA'")
        assert query_rows(con, "SELECT * FROM options_chain WHERE symbol = 'NVDA'")
        assert query_rows(con, "SELECT * FROM news_items WHERE id = 'n1'")
