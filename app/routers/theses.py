"""Thesis, fundamentals, disclosures, and catalyst read-model routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app import deps

router = APIRouter()


@router.get("/api/theses")
def theses() -> dict[str, Any]:
    return deps.user_state_table_payload(deps.thesis_rows(deps.load_config()))


@router.get("/api/thesis-monitor")
def thesis_monitor() -> dict[str, Any]:
    return deps.thesis_monitor_payload(deps.load_config())


@router.put("/api/theses/{symbol}")
def save_thesis_endpoint(symbol: str, payload: deps.ThesisInput, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    config = deps.load_config()
    try:
        saved = deps.save_thesis(config, symbol, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = deps.thesis_monitor_rows(config)
    return {"thesis": saved, "thesis_monitor": deps.user_state_table_payload(rows)}


@router.post("/api/theses/{symbol}/review")
def review_thesis_endpoint(symbol: str, request: Request, payload: deps.ThesisReviewInput | None = None) -> dict[str, Any]:
    deps._require_local_request(request)
    config = deps.load_config()
    try:
        reviewed = deps.record_thesis_review(config, symbol, (payload or deps.ThesisReviewInput()).model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = deps.thesis_monitor_rows(config)
    return {"review": reviewed, "thesis_monitor": deps.user_state_table_payload(rows)}


@router.get("/api/theses/{symbol}/history")
def thesis_history_endpoint(symbol: str) -> dict[str, Any]:
    return deps.thesis_history(deps.load_config(), symbol)


@router.get("/api/trader-twins")
def trader_twins() -> dict[str, Any]:
    return deps._table_payload("trader_twins")


@router.get("/api/catalysts")
def catalysts() -> dict[str, Any]:
    return deps._table_payload("catalysts")


@router.get("/api/fundamentals")
def fundamentals() -> dict[str, Any]:
    return deps._table_payload("fundamentals")


@router.get("/api/disclosures")
def disclosures() -> dict[str, Any]:
    return deps._table_payload("disclosures")
