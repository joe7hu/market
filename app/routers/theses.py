"""Thesis detail and automation routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app import job_control, panel_snapshot
from app.actions.theses import ThesisActions
from app.contracts import ThesisAutomationInput, ThesisInput, ThesisReviewInput
from app.data_access import config as config_owner
from app.request_security import require_local_request
from app.response_contracts import (
    ThesisAutomationResponse,
    ThesisHistoryResponse,
    ThesisMutationResponse,
    ThesisReviewResponse,
)

router = APIRouter()


@router.put("/api/theses/{symbol}", response_model=ThesisMutationResponse, response_model_exclude_unset=True)
def save_thesis_endpoint(symbol: str, payload: ThesisInput, request: Request) -> dict[str, Any]:
    require_local_request(request)
    config = config_owner.load_config()
    try:
        return ThesisActions(config).save(symbol, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/theses/{symbol}/review", response_model=ThesisReviewResponse, response_model_exclude_unset=True)
def review_thesis_endpoint(symbol: str, request: Request, payload: ThesisReviewInput | None = None) -> dict[str, Any]:
    require_local_request(request)
    config = config_owner.load_config()
    try:
        return ThesisActions(config).review(symbol, (payload or ThesisReviewInput()).model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/theses/{symbol}/history", response_model=ThesisHistoryResponse, response_model_exclude_unset=True)
def thesis_history_endpoint(symbol: str) -> dict[str, Any]:
    return ThesisActions(config_owner.load_config()).history(symbol)


@router.post("/api/thesis-monitor/automation", response_model=ThesisAutomationResponse, response_model_exclude_unset=True)
def run_thesis_automation_endpoint(
    payload: ThesisAutomationInput,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    require_local_request(request)
    config = config_owner.load_config()
    if payload.symbols:
        symbols = sorted({str(symbol).upper() for symbol in payload.symbols if str(symbol).strip()})
        if not symbols:
            raise HTTPException(status_code=400, detail="symbols must contain at least one non-empty symbol")
        background_tasks.add_task(
            job_control.execute_thesis_monitor_automation,
            symbols,
            dry_run=payload.dry_run,
            force=payload.force,
        )
        panel_snapshot.invalidate_context_cache()
        return {
            "job": {"job_name": "run_thesis_monitor_ondemand", "status": "accepted", "created": True},
            "symbols": symbols,
            "dry_run": payload.dry_run,
            "force": payload.force,
        }
    job_name = "run_thesis_monitor_preflight" if payload.dry_run else "run_thesis_monitor_force"
    try:
        job = job_control.start_refresh_job(job_name, config_owner.database_url(config))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if job.get("created"):
        background_tasks.add_task(
            job_control.execute_background_refresh_job,
            job["id"],
            job_name,
            config_owner.database_url(config),
        )
    panel_snapshot.invalidate_context_cache()
    return {"job": job, "symbols": "all", "dry_run": payload.dry_run, "force": True}


__all__ = ["router"]
