"""Refresh-job and settings routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app import job_control, panel_snapshot
from app.contracts import AgentSettingsInput, ResearchSourcesInput
from app.data_access import config as config_owner
from app.data_access import loaders, settings as settings_owner
from app.request_security import require_local_request

router = APIRouter()


@router.get("/api/refresh-jobs")
def refresh_jobs() -> dict[str, Any]:
    config = config_owner.load_config()
    rows = job_control.refresh_job_rows(config_owner.database_url(config))
    return {
        "rows": rows,
        "count": len(rows),
        "allowlist": sorted(job_control.ALLOWLIST),
        "latest_status": panel_snapshot.full_market_refresh_status(config),
    }


@router.post("/api/refresh-jobs/{job_name}")
def launch_refresh_job(job_name: str, request: Request) -> dict[str, Any]:
    require_local_request(request)
    config = config_owner.load_config()
    try:
        result = job_control.run_refresh_job(job_name, config_owner.database_url(config), "config.yaml")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


@router.post("/api/refresh-jobs/{job_name}/background")
def launch_refresh_job_background(job_name: str, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    require_local_request(request)
    config = config_owner.load_config()
    dsn = config_owner.database_url(config)
    try:
        job = job_control.start_refresh_job(job_name, dsn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if job.get("created"):
        background_tasks.add_task(job_control.execute_background_refresh_job, job["id"], job_name, dsn)
    return job


@router.get("/api/settings")
def settings() -> dict[str, Any]:
    config, panel_data = _settings_context()
    return settings_owner.settings_payload(config, panel_data)


@router.patch("/api/settings/agents")
def update_agent_settings(payload: AgentSettingsInput, request: Request) -> dict[str, Any]:
    require_local_request(request)
    try:
        settings_owner.persist_setting_section(config_owner.load_config(), "agents", payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    config, panel_data = _settings_context()
    return settings_owner.settings_payload(config, panel_data)


@router.patch("/api/settings/research-sources")
def update_research_sources(payload: ResearchSourcesInput, request: Request) -> dict[str, Any]:
    require_local_request(request)
    try:
        settings_owner.persist_setting_section(config_owner.load_config(), "research_sources", payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    config, panel_data = _settings_context()
    return settings_owner.settings_payload(config, panel_data)


def _settings_context() -> tuple[dict[str, Any], Any]:
    """Load only source-run rows required by Settings."""

    return panel_snapshot.context(
        cache_key="settings",
        loader=lambda config: loaders.load_panel_data(
            config,
            table_names=("source_runs",),
            query_row_limits={"source_runs": 200},
        ),
    )


__all__ = ["router"]
