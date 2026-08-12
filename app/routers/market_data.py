"""Quotes, screener, news, TradingView, and analysis read-model routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app import deps

router = APIRouter()


@router.get("/api/quotes")
def quotes(symbols: str | None = Query(default=None, description="Comma-separated symbols, maximum 100.")) -> dict[str, Any]:
    requested = {
        symbol.strip().upper()
        for symbol in (symbols or "").split(",")
        if symbol.strip()
    }
    if len(requested) > 100:
        return {"status": "invalid", "error": "symbols supports at most 100 symbols", "rows": []}
    if requested:
        cache_key = "table:quotes:" + ",".join(sorted(requested))
        _, panel_data = deps._context(
            cache_key=cache_key,
            loader=lambda config: deps.load_panel_data(
                config,
                table_names=("quotes",),
                query_symbol_filter=requested,
                query_row_limits={"quotes": len(requested)},
            ),
        )
        return deps.table_payload(panel_data, "quotes")
    return deps._table_payload("quotes")


@router.get("/api/screener")
def screener() -> dict[str, Any]:
    return deps._table_payload("screener")


@router.get("/api/news")
def news() -> dict[str, Any]:
    return deps._table_payload("news")


@router.get("/api/tradingview-symbol-search")
def tradingview_symbol_search() -> dict[str, Any]:
    return deps._table_payload("tradingview_symbol_search")


@router.get("/api/instrument-market-identity")
def instrument_market_identity() -> dict[str, Any]:
    return deps._table_payload("instrument_market_identity")


@router.get("/api/tradingview-watchlists")
def tradingview_watchlists() -> dict[str, Any]:
    return deps._table_payload("tradingview_watchlists")


@router.get("/api/tradingview-alerts")
def tradingview_alerts() -> dict[str, Any]:
    return deps._table_payload("tradingview_alerts")


@router.get("/api/tradingview-chart-state")
def tradingview_chart_state() -> dict[str, Any]:
    return deps._table_payload("tradingview_chart_state")


@router.get("/api/sepa")
def sepa() -> dict[str, Any]:
    return deps._table_payload("sepa")


@router.get("/api/liquidity")
def liquidity() -> dict[str, Any]:
    return deps._table_payload("liquidity")


@router.get("/api/correlations")
def correlations() -> dict[str, Any]:
    return deps._table_payload("correlations")


@router.get("/api/etf-premiums")
def etf_premiums() -> dict[str, Any]:
    return deps._table_payload("etf_premiums")


@router.get("/api/analyst-estimates")
def analyst_estimates() -> dict[str, Any]:
    return deps._table_payload("analyst_estimates")


@router.get("/api/earnings")
def earnings() -> dict[str, Any]:
    return deps._table_payload("earnings")


@router.get("/api/earnings-setups")
def earnings_setups() -> dict[str, Any]:
    return deps._table_payload("earnings_setups")


@router.get("/api/valuations")
def valuations() -> dict[str, Any]:
    return deps._table_payload("valuations")


@router.get("/api/technicals")
def technicals() -> dict[str, Any]:
    return deps._table_payload("technicals")


@router.get("/api/research-packets")
def research_packets() -> dict[str, Any]:
    return deps._table_payload("research_packets")


@router.get("/api/memos")
def memos() -> dict[str, Any]:
    return deps._table_payload("ticker_memos")


@router.get("/api/provider-runs")
def provider_runs() -> dict[str, Any]:
    return deps._table_payload("provider_runs")
