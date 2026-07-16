"""Portfolio, portfolio-risk, and watchlist management routes."""
from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request

from app import deps

router = APIRouter()


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
    config = deps.load_config()
    symbol = position.symbol.strip().upper()
    if any(str(row.get("symbol") or "") == symbol for row in deps.portfolio_rows(config)):
        raise HTTPException(status_code=409, detail="position already exists; record a buy or sell transaction")
    try:
        executed_at = (
            datetime.combine(
                date.fromisoformat(position.purchase_date.strip()[:10]),
                time(12),
                tzinfo=ZoneInfo("America/New_York"),
            ).isoformat()
            if position.purchase_date
            else datetime.now(UTC).isoformat()
        )
        saved = deps.record_portfolio_transaction(
            config,
            {
                "symbol": symbol,
                "transaction_type": "opening_balance",
                "quantity": position.quantity,
                "price": position.avg_cost,
                "fees": 0,
                "executed_at": executed_at,
                "notes": position.notes,
                "idempotency_key": (
                    f"position-import:{symbol}:{executed_at}:{position.quantity:g}:{position.avg_cost:g}"
                ),
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = deps.portfolio_rows(config)
    return {"transaction": saved, "portfolio": deps.user_state_table_payload(rows)}


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
        return deps.preview_portfolio_transaction(deps.load_config(), transaction.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/portfolio/transactions")
def create_portfolio_transaction(transaction: deps.PortfolioTransactionInput, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    config = deps.load_config()
    try:
        saved = deps.record_portfolio_transaction(config, transaction.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    return {
        "transaction": saved,
        "portfolio": deps.user_state_table_payload(deps.portfolio_rows(config)),
    }


@router.post("/api/portfolio/transactions/{transaction_id}/reverse")
def reverse_transaction(
    transaction_id: str,
    reversal: deps.PortfolioTransactionReversalInput,
    request: Request,
) -> dict[str, Any]:
    deps._require_local_request(request)
    config = deps.load_config()
    try:
        saved = deps.reverse_portfolio_transaction(
            config,
            transaction_id,
            idempotency_key=reversal.idempotency_key,
            notes=reversal.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    return {
        "transaction": saved,
        "portfolio": deps.user_state_table_payload(deps.portfolio_rows(config)),
    }


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
    config = deps.load_config()
    try:
        saved = deps.save_watchlist_symbol(config, item.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    refresh_result = deps.populate_watchlist_symbol_data(
        config,
        saved["symbol"],
        saved.get("asset_class"),
    )
    deps._invalidate_context_cache()
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
