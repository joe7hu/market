"""Portfolio transaction and watchlist mutation routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app import panel_snapshot
from app.actions.portfolio import PortfolioActions
from app.actions.options import OptionsActions
from app import dependencies
from app.contracts import (
    OptionsHistoryToggleInput,
    PortfolioTransactionInput,
    PortfolioTransactionReversalInput,
    WatchlistSymbolInput,
)
from app.data_access import loaders, payloads
from app.response_contracts import (
    OptionsHistoryPolicyResponse,
    PortfolioTransactionPreviewResponse,
    PortfolioTransactionResultResponse,
    TablePayloadResponse,
    WatchlistMutationResponse,
)
from investment_panel.core.config import AppConfig

router = APIRouter()


@router.get("/api/portfolio/transactions", response_model=TablePayloadResponse, response_model_exclude_unset=True)
def portfolio_transactions(
    limit: int = 100,
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    _, panel_data = panel_snapshot.context(
        cache_key=f"table:portfolio_transactions:{limit}",
        loader=lambda config: loaders.load_panel_data(
            config,
            table_names=("portfolio_transactions",),
            query_row_limits={"portfolio_transactions": max(1, min(limit, 500))},
        ),
        config_loader=lambda: config,
    )
    return payloads.table_payload(panel_data, "portfolio_transactions")


@router.post("/api/portfolio/transactions/preview", response_model=PortfolioTransactionPreviewResponse, response_model_exclude_unset=True)
def preview_transaction(
    transaction: PortfolioTransactionInput,
    actions: PortfolioActions = Depends(dependencies.get_portfolio_actions),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    try:
        return actions.preview_transaction(transaction.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/portfolio/transactions", response_model=PortfolioTransactionResultResponse, response_model_exclude_unset=True)
def create_portfolio_transaction(
    transaction: PortfolioTransactionInput,
    actions: PortfolioActions = Depends(dependencies.get_portfolio_actions),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    try:
        result = actions.record_transaction(transaction.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


@router.post("/api/portfolio/transactions/{transaction_id}/reverse", response_model=PortfolioTransactionResultResponse, response_model_exclude_unset=True)
def reverse_transaction(
    transaction_id: str,
    reversal: PortfolioTransactionReversalInput,
    actions: PortfolioActions = Depends(dependencies.get_portfolio_actions),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    try:
        result = actions.reverse_transaction(transaction_id, reversal.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


@router.post("/api/watchlist/symbols", response_model=WatchlistMutationResponse, response_model_exclude_unset=True)
def save_watchlist_symbol_endpoint(
    item: WatchlistSymbolInput,
    actions: PortfolioActions = Depends(dependencies.get_portfolio_actions),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    try:
        result = actions.save_watchlist_symbol(item.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


@router.delete("/api/watchlist/symbols/{symbol}", response_model=WatchlistMutationResponse, response_model_exclude_unset=True)
def delete_watchlist_symbol_endpoint(
    symbol: str,
    actions: PortfolioActions = Depends(dependencies.get_portfolio_actions),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    try:
        result = actions.delete_watchlist_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


@router.patch("/api/watchlist/symbols/{symbol}/options-history", response_model=OptionsHistoryPolicyResponse, response_model_exclude_unset=True)
def set_watchlist_options_history_endpoint(
    symbol: str,
    payload: OptionsHistoryToggleInput,
    actions: OptionsActions = Depends(dependencies.get_options_actions),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    try:
        result = actions.set_history_requested_state(symbol, payload.model_dump())
    except Exception as exc:
        if actions.is_policy_conflict(exc):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


__all__ = ["router"]
