"""Portfolio, portfolio-risk, and watchlist management routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app import deps
from app.actions.portfolio import PortfolioActions

router = APIRouter()


def _actions() -> PortfolioActions:
    return PortfolioActions(
        deps.load_config(),
        portfolio_rows=deps.portfolio_rows,
        table_payload=deps.user_state_table_payload,
        preview_transaction=deps.preview_portfolio_transaction,
        record_transaction=deps.record_portfolio_transaction,
        reverse_transaction=deps.reverse_portfolio_transaction,
        watchlist_rows=deps.watchlist_rows,
        save_watchlist=deps.save_watchlist_symbol,
        populate_watchlist=deps.populate_watchlist_symbol_data,
        delete_watchlist=deps.delete_watchlist_symbol,
    )


@router.get("/api/portfolio")
def portfolio() -> dict[str, Any]:
    return deps.user_state_table_payload(deps.portfolio_rows(deps.load_config()))


@router.get("/api/portfolio/summary")
def portfolio_summary() -> dict[str, Any]:
    return deps.portfolio_summary(deps.load_config())


@router.get("/api/portfolio/performance")
def portfolio_performance() -> dict[str, Any]:
    return deps.user_state_table_payload(deps.portfolio_performance_rows(deps.load_config()))


@router.post("/api/portfolio/positions")
def save_position(position: deps.PortfolioPositionInput, request: Request) -> dict[str, Any]:
    """Compatibility import path for a new opening balance; subsequent changes must be trades."""
    deps._require_local_request(request)
    try:
        result = _actions().import_position(position.model_dump())
    except ValueError as exc:
        status = 409 if "already exists" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    return result


@router.delete("/api/portfolio/positions/{symbol}")
def delete_position(symbol: str) -> dict[str, Any]:
    raise HTTPException(
        status_code=410,
        detail=f"direct deletion is retired; record a sell or transfer-out transaction for {symbol.upper()}",
    )


@router.get("/api/portfolio/transactions")
def portfolio_transactions(limit: int = 100) -> dict[str, Any]:
    return deps.user_state_table_payload(deps.portfolio_transaction_rows(deps.load_config(), limit=limit))


@router.post("/api/portfolio/transactions/preview")
def preview_transaction(transaction: deps.PortfolioTransactionInput, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    try:
        return _actions().preview_transaction(transaction.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/portfolio/transactions")
def create_portfolio_transaction(transaction: deps.PortfolioTransactionInput, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    try:
        result = _actions().record_transaction(transaction.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    return result


@router.post("/api/portfolio/transactions/{transaction_id}/reverse")
def reverse_transaction(
    transaction_id: str,
    reversal: deps.PortfolioTransactionReversalInput,
    request: Request,
) -> dict[str, Any]:
    deps._require_local_request(request)
    try:
        result = _actions().reverse_transaction(transaction_id, reversal.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    return result


@router.get("/api/portfolio-risk/exposure-clusters")
def portfolio_risk_exposure_clusters() -> dict[str, Any]:
    return deps.user_state_table_payload(deps.portfolio_exposure_rows(deps.load_config()))


@router.get("/api/portfolio-risk/correlation-edges")
def portfolio_risk_correlation_edges() -> dict[str, Any]:
    return deps.user_state_table_payload(deps.portfolio_correlation_rows(deps.load_config()))


@router.get("/api/portfolio-risk/cards")
def portfolio_risk_cards() -> dict[str, Any]:
    return deps.user_state_table_payload(deps.portfolio_risk_rows(deps.load_config()))


@router.get("/api/portfolio-risk/review-actions")
def portfolio_risk_review_actions() -> dict[str, Any]:
    return deps.user_state_table_payload(deps.portfolio_review_action_rows(deps.load_config()))


@router.get("/api/watchlist-screen")
def watchlist_screen() -> dict[str, Any]:
    return deps._table_payload("universe_screen")


@router.get("/api/watchlist/symbols")
def watchlist_symbols() -> dict[str, Any]:
    return deps.user_state_table_payload(deps.watchlist_rows(deps.load_config()))


@router.post("/api/watchlist/symbols")
def save_watchlist_symbol_endpoint(item: deps.WatchlistSymbolInput, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    try:
        result = _actions().save_watchlist_symbol(item.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    return result


@router.delete("/api/watchlist/symbols/{symbol}")
def delete_watchlist_symbol_endpoint(symbol: str, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    try:
        result = _actions().delete_watchlist_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    return result
