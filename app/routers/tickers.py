"""Ticker dossier routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app import panel_snapshot
from app.data_access import loaders, payloads
from app.response_contracts import TickerDecisionSnapshotResponse, TickerDetailResponse

router = APIRouter()


@router.get("/api/tickers/{ticker}", response_model=TickerDetailResponse, response_model_exclude_unset=True)
def ticker_detail(ticker: str) -> dict[str, Any]:
    normalized = ticker.strip().upper()
    _, panel_data = panel_snapshot.context(
        cache_key=f"ticker:{normalized}",
        loader=lambda config: loaders.load_ticker_panel_data(config, normalized),
    )
    return payloads.ticker_payload(panel_data, normalized)


@router.get("/api/tickers/{ticker}/decision-snapshot", response_model=TickerDecisionSnapshotResponse, response_model_exclude_unset=True)
def ticker_decision_snapshot(ticker: str) -> dict[str, Any]:
    _, panel_data = panel_snapshot.context()
    normalized = ticker.upper()
    rows = [
        row
        for row in payloads.table_payload(panel_data, "symbol_decision_snapshot")["rows"]
        if str(row.get("symbol") or "").upper() == normalized
    ]
    if rows:
        return rows[0]
    return {"symbol": normalized, "found": False}


__all__ = ["router"]
