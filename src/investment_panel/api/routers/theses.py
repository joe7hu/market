"""Thesis detail and automation routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from investment_panel.api import job_control
from investment_panel.application.read_models import panel_snapshot
from investment_panel.api import dependencies
from investment_panel.workflows.theses import ThesisActions
from investment_panel.api.contracts import ThesisAutomationInput, ThesisInput, ThesisReviewInput
from investment_panel.api.response_contracts import (
    ThesisAutomationResponse,
    ThesisHistoryResponse,
    ThesisMutationResponse,
    ThesisReviewResponse,
)
from investment_panel.settings import AppConfig

router = APIRouter()


@router.put("/api/theses/{symbol}", response_model=ThesisMutationResponse, response_model_exclude_unset=True)
def save_thesis_endpoint(
    symbol: str,
    payload: ThesisInput,
    actions: ThesisActions = Depends(dependencies.get_thesis_actions),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    try:
        return actions.save(symbol, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/theses/{symbol}/review", response_model=ThesisReviewResponse, response_model_exclude_unset=True)
def review_thesis_endpoint(
    symbol: str,
    payload: ThesisReviewInput | None = None,
    actions: ThesisActions = Depends(dependencies.get_thesis_actions),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    try:
        return actions.review(symbol, (payload or ThesisReviewInput()).model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/theses/{symbol}/history", response_model=ThesisHistoryResponse, response_model_exclude_unset=True)
def thesis_history_endpoint(
    symbol: str,
    actions: ThesisActions = Depends(dependencies.get_thesis_actions),
) -> dict[str, Any]:
    return actions.history(symbol)


@router.post("/api/thesis-monitor/automation", response_model=ThesisAutomationResponse, response_model_exclude_unset=True)
def run_thesis_automation_endpoint(
    payload: ThesisAutomationInput,
    background_tasks: BackgroundTasks,
    config: AppConfig = Depends(dependencies.get_config),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
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
        job = job_control.start_refresh_job(job_name, config.database.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job_control.schedule_created_job(background_tasks, job, job_name, config.database.url)
    panel_snapshot.invalidate_context_cache()
    return {"job": job, "symbols": "all", "dry_run": payload.dry_run, "force": True}


__all__ = ["router"]
