"""Settings and refresh-job orchestration routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app import deps

router = APIRouter()


@router.get("/api/refresh-jobs")
def refresh_jobs() -> dict[str, Any]:
    config = deps.load_config()
    rows = deps.refresh_job_rows(deps.database_url(config))
    return {"rows": rows, "count": len(rows), "allowlist": sorted(deps.ALLOWLIST), "latest_status": deps._full_market_refresh_status(config)}


@router.post("/api/refresh-jobs/{job_name}")
def launch_refresh_job(job_name: str, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    config = deps.load_config()
    try:
        result = deps.run_refresh_job(job_name, deps.database_url(config), "config.yaml")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    return result


@router.post("/api/refresh-jobs/{job_name}/background")
def launch_refresh_job_background(job_name: str, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    deps._require_local_request(request)
    config = deps.load_config()
    database_url = deps.database_url(config)
    try:
        job = deps.start_refresh_job(job_name, database_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if job.get("created"):
        background_tasks.add_task(deps._execute_background_refresh_job, job["id"], job_name, database_url)
    return job


@router.get("/api/settings")
def settings() -> dict[str, Any]:
    config, panel_data = deps._context()
    return deps.settings_payload(config, panel_data)


@router.patch("/api/settings/agents")
def update_agent_settings(payload: deps.AgentSettingsInput, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    try:
        deps.update_agent_settings_config("config.yaml", payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    config, panel_data = deps._context()
    return deps.settings_payload(config, panel_data)


@router.patch("/api/settings/research-sources")
def update_research_sources(payload: deps.ResearchSourcesInput, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    try:
        deps.update_research_sources_config("config.yaml", payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    config, panel_data = deps._context()
    return deps.settings_payload(config, panel_data)
