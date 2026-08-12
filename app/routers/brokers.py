"""Broker status and paper-order journal routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app import deps
from app.actions.paper_orders import PaperOrderActions

router = APIRouter()


def _paper_actions(config: dict[str, Any]) -> PaperOrderActions:
    return PaperOrderActions(config)


@router.get("/api/broker/status")
def broker_status() -> dict[str, Any]:
    return deps._table_payload("broker_status")


@router.get("/api/broker/accounts")
def broker_accounts() -> dict[str, Any]:
    return deps._table_payload("broker_accounts")


@router.get("/api/broker/positions")
def broker_positions() -> dict[str, Any]:
    return deps._table_payload("broker_positions")


@router.get("/api/agent/recommendations")
def agent_recommendations() -> dict[str, Any]:
    raise HTTPException(
        status_code=410,
        detail="Retired: this route was not agent-authored. Use /api/panel-snapshot?scope=options-radar or /api/options/tickets/{decision_id}.",
    )


@router.post("/api/agent/review")
def run_agent_review(request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    raise HTTPException(
        status_code=410,
        detail="Retired: canonical option recommendations are published by the options-radar pipeline.",
    )


@router.get("/api/paper-orders")
def paper_orders(
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None, max_length=256),
) -> dict[str, Any]:
    config = deps.load_config()
    # Legacy read-only test callers do not configure the PostgreSQL authority.
    # Keep their retired cache fixture path harmless; production always takes
    # the PostgreSQL-backed, bounded cursor path below.
    if not str((config.get("database") or {}).get("url") or "").strip():
        return deps._table_payload("paper_orders")
    try:
        return _paper_actions(config).rows(limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/paper-orders")
def stage_paper_order_endpoint(request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    raise HTTPException(
        status_code=410,
        detail="Retired: stage only an immutable READY option ticket through /api/options-radar/signals/{decision_id}/paper-entry.",
    )
