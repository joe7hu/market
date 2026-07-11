"""Ticker dossier routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app import deps

router = APIRouter()


@router.get("/api/tickers/{ticker}")
def ticker_detail(ticker: str) -> dict[str, Any]:
    normalized = ticker.strip().upper()
    with deps._CONTEXT_LOCK:
        _, panel_data = deps._context(
            cache_key=f"ticker:{normalized}",
            loader=lambda config: deps.load_ticker_panel_data(config, normalized),
        )
    return deps.ticker_payload(panel_data, normalized)


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
