"""Broker status, agent recommendations, and paper-order routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app import deps

router = APIRouter()


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
    return deps._table_payload("agent_recommendations")


@router.post("/api/agent/review")
def run_agent_review(request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    config = deps.load_core_config("config.yaml")
    from investment_panel.database.authority import runtime_for_config
    from investment_panel.database.brokers import BrokerRepository

    rows = BrokerRepository(runtime_for_config(config)).build_recommendations()
    deps._invalidate_context_cache()
    return {"status": "ok", "count": len(rows), "rows": rows[:25]}


@router.get("/api/paper-orders")
def paper_orders() -> dict[str, Any]:
    return deps._table_payload("paper_orders")


@router.post("/api/paper-orders")
def stage_paper_order_endpoint(payload: deps.PaperOrderInput, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    config = deps.load_core_config("config.yaml")
    from investment_panel.database.authority import runtime_for_config
    from investment_panel.database.brokers import BrokerRepository

    try:
        result = BrokerRepository(runtime_for_config(config)).stage_paper_order(payload.recommendation_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    return result
