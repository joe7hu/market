"""Thesis, fundamentals, disclosures, and catalyst read-model routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app import deps

router = APIRouter()


@router.get("/api/theses")
def theses() -> dict[str, Any]:
    return deps._table_payload("theses")


@router.get("/api/thesis-monitor")
def thesis_monitor() -> dict[str, Any]:
    return deps._table_payload("thesis_monitor")


@router.put("/api/theses/{symbol}")
def save_thesis_endpoint(symbol: str, payload: deps.ThesisInput, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    config = deps.load_config()
    try:
        saved = deps.save_thesis(config, symbol, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    return {"thesis": saved, "thesis_monitor": deps._table_payload("thesis_monitor")}


@router.post("/api/theses/{symbol}/review")
def review_thesis_endpoint(symbol: str, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    config = deps.load_config()
    try:
        reviewed = deps.mark_thesis_reviewed(config, symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    return {"review": reviewed, "thesis_monitor": deps._table_payload("thesis_monitor")}


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
