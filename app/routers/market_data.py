"""Quotes, screener, news, TradingView, and analysis read-model routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app import deps

router = APIRouter()


@router.get("/api/quotes")
def quotes() -> dict[str, Any]:
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
