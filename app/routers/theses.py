"""Thesis, fundamentals, disclosures, and catalyst read-model routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

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


@router.post("/api/thesis-monitor/automation")
def run_thesis_automation_endpoint(
    payload: deps.ThesisAutomationInput,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    deps._require_local_request(request)
    config = deps.load_config()
    if payload.symbols:
        symbols = sorted({str(symbol).upper() for symbol in payload.symbols if str(symbol).strip()})
        if not symbols:
            raise HTTPException(status_code=400, detail="symbols must contain at least one non-empty symbol")
        background_tasks.add_task(
            deps._execute_thesis_monitor_automation,
            symbols,
            dry_run=payload.dry_run,
            force=payload.force,
        )
        deps._invalidate_context_cache()
        return {
            "job": {"job_name": "run_thesis_monitor_ondemand", "status": "accepted", "created": True},
            "symbols": symbols,
            "dry_run": payload.dry_run,
            "force": payload.force,
        }
    job_name = "run_thesis_monitor_preflight" if payload.dry_run else "run_thesis_monitor_force"
    try:
        job = deps.start_refresh_job(job_name, deps.database_url(config))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if job.get("created"):
        background_tasks.add_task(deps._execute_background_refresh_job, job["id"], job_name, deps.database_url(config))
    deps._invalidate_context_cache()
    return {"job": job, "symbols": "all", "dry_run": payload.dry_run, "force": True}


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
