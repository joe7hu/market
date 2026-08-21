"""Portfolio transaction and watchlist mutation routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app import panel_snapshot
from app.actions.options import OptionsActions
from app.actions.portfolio import PortfolioActions
from app.contracts import (
    OptionsHistoryToggleInput,
    PortfolioTransactionInput,
    PortfolioTransactionReversalInput,
    WatchlistSymbolInput,
)
from app.data_access import config as config_owner
from app.data_access import loaders, mutations, payloads
from app.request_security import require_local_request

router = APIRouter()


def _actions() -> PortfolioActions:
    return PortfolioActions(
        config_owner.load_config(),
        save_watchlist=mutations.save_watchlist_symbol,
        populate_watchlist=mutations.populate_watchlist_symbol_data,
        delete_watchlist=mutations.delete_watchlist_symbol,
    )


@router.get("/api/portfolio/transactions")
def portfolio_transactions(limit: int = 100) -> dict[str, Any]:
    _, panel_data = panel_snapshot.context(
        cache_key=f"table:portfolio_transactions:{limit}",
        loader=lambda config: loaders.load_panel_data(
            config,
            table_names=("portfolio_transactions",),
            query_row_limits={"portfolio_transactions": max(1, min(limit, 500))},
        ),
    )
    return payloads.table_payload(panel_data, "portfolio_transactions")


@router.post("/api/portfolio/transactions/preview")
def preview_transaction(transaction: PortfolioTransactionInput, request: Request) -> dict[str, Any]:
    require_local_request(request)
    try:
        return _actions().preview_transaction(transaction.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/portfolio/transactions")
def create_portfolio_transaction(transaction: PortfolioTransactionInput, request: Request) -> dict[str, Any]:
    require_local_request(request)
    try:
        result = _actions().record_transaction(transaction.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


@router.post("/api/portfolio/transactions/{transaction_id}/reverse")
def reverse_transaction(
    transaction_id: str,
    reversal: PortfolioTransactionReversalInput,
    request: Request,
) -> dict[str, Any]:
    require_local_request(request)
    try:
        result = _actions().reverse_transaction(transaction_id, reversal.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


@router.post("/api/watchlist/symbols")
def save_watchlist_symbol_endpoint(item: WatchlistSymbolInput, request: Request) -> dict[str, Any]:
    require_local_request(request)
    try:
        result = _actions().save_watchlist_symbol(item.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


@router.delete("/api/watchlist/symbols/{symbol}")
def delete_watchlist_symbol_endpoint(symbol: str, request: Request) -> dict[str, Any]:
    require_local_request(request)
    try:
        result = _actions().delete_watchlist_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


@router.patch("/api/watchlist/symbols/{symbol}/options-history")
def set_watchlist_options_history_endpoint(
    symbol: str,
    payload: OptionsHistoryToggleInput,
    request: Request,
) -> dict[str, Any]:
    require_local_request(request)
    actions = OptionsActions(config_owner.load_config())
    try:
        result = actions.set_history_requested_state(symbol, payload.model_dump())
    except Exception as exc:
        if actions.is_policy_conflict(exc):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


__all__ = ["router"]
