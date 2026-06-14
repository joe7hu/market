"""Ticker dossier routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app import deps

router = APIRouter()


@router.get("/api/tickers/{ticker}")
def ticker_detail(ticker: str) -> dict[str, Any]:
    config = deps.load_config()
    with deps._CONTEXT_LOCK:
        panel_data = deps.load_ticker_panel_data(config, ticker)
    return deps.ticker_payload(panel_data, ticker)


@router.get("/api/tickers/{ticker}/decision-snapshot")
def ticker_decision_snapshot(ticker: str) -> dict[str, Any]:
    _, panel_data = deps._context()
    normalized = ticker.upper()
    rows = [
        row
        for row in deps.table_payload(panel_data, "symbol_decision_snapshot")["rows"]
        if str(row.get("symbol") or "").upper() == normalized
    ]
    if rows:
        return rows[0]
    return {"symbol": normalized, "found": False}
