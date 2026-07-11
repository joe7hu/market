"""Portfolio, portfolio-risk, and watchlist management routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app import deps

router = APIRouter()


@router.get("/api/portfolio")
def portfolio() -> dict[str, Any]:
    return deps.user_state_table_payload(deps.portfolio_rows(deps.load_config()))


@router.post("/api/portfolio/positions")
def save_position(position: deps.PortfolioPositionInput) -> dict[str, Any]:
    config = deps.load_config()
    try:
        saved = deps.save_portfolio_position(config, position.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = deps.portfolio_rows(config)
    return {"position": saved, "portfolio": deps.user_state_table_payload(rows)}


@router.delete("/api/portfolio/positions/{symbol}")
def delete_position(symbol: str) -> dict[str, Any]:
    config = deps.load_config()
    try:
        deleted = deps.delete_portfolio_position(config, symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = deps.portfolio_rows(config)
    return {"position": deleted, "portfolio": deps.user_state_table_payload(rows)}


@router.get("/api/portfolio-risk/exposure-clusters")
def portfolio_risk_exposure_clusters() -> dict[str, Any]:
    return deps._table_payload("exposure_clusters")


@router.get("/api/portfolio-risk/correlation-edges")
def portfolio_risk_correlation_edges() -> dict[str, Any]:
    return deps._table_payload("correlation_edges")


@router.get("/api/portfolio-risk/cards")
def portfolio_risk_cards() -> dict[str, Any]:
    return deps._table_payload("portfolio_risk_cards")


@router.get("/api/portfolio-risk/review-actions")
def portfolio_risk_review_actions() -> dict[str, Any]:
    return deps._table_payload("review_actions")


@router.get("/api/watchlist-screen")
def watchlist_screen() -> dict[str, Any]:
    return deps._table_payload("universe_screen")


@router.get("/api/watchlist/symbols")
def watchlist_symbols() -> dict[str, Any]:
    return deps.user_state_table_payload(deps.watchlist_rows(deps.load_config()))


@router.post("/api/watchlist/symbols")
def save_watchlist_symbol_endpoint(item: deps.WatchlistSymbolInput, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    config = deps.load_config()
    try:
        saved = deps.save_watchlist_symbol(config, item.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    refresh_result = {
        "status": "pending_source_refresh",
        "symbol": saved["symbol"],
        "reason": "PostgreSQL source ingestion is queued for the next migration slice",
    }
    rows = deps.watchlist_rows(config)
    return {
        "watchlist_symbol": saved,
        "data_refresh": refresh_result,
        "watchlist": deps.user_state_table_payload(rows),
    }


@router.delete("/api/watchlist/symbols/{symbol}")
def delete_watchlist_symbol_endpoint(symbol: str, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    config = deps.load_config()
    try:
        deleted = deps.delete_watchlist_symbol(config, symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = deps.watchlist_rows(config)
    return {"watchlist_symbol": deleted, "watchlist": deps.user_state_table_payload(rows)}
