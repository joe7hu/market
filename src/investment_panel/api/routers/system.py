"""Refresh-job and settings routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from investment_panel.api import job_control
from investment_panel.application.read_models import panel_snapshot
from investment_panel.api import dependencies
from investment_panel.api.contracts import AgentSettingsInput, ResearchSourcesInput
from investment_panel.application.read_models import loaders
from investment_panel.api.data_access import settings as settings_owner
from investment_panel.api.response_contracts import DecisionFunnelResponse, RefreshJobResponse, RefreshJobsResponse, SettingsResponse
from investment_panel.settings import AppConfig, load_config

router = APIRouter()


@router.get("/api/decision-funnel", response_model=DecisionFunnelResponse)
def decision_funnel(
    runtime=Depends(dependencies.get_runtime),
) -> dict[str, Any]:
    return loaders.load_decision_funnel(runtime)


@router.get("/api/refresh-jobs", response_model=RefreshJobsResponse, response_model_exclude_unset=True)
def refresh_jobs(config: AppConfig = Depends(dependencies.get_config)) -> dict[str, Any]:
    rows = job_control.refresh_job_rows(config.database.url)
    return {
        "rows": rows,
        "count": len(rows),
        "allowlist": sorted(job_control.ALLOWLIST),
    }


@router.post("/api/refresh-jobs/{job_name}", response_model=RefreshJobResponse, response_model_exclude_unset=True)
def launch_refresh_job(
    job_name: str,
    config: AppConfig = Depends(dependencies.get_config),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    try:
        result = job_control.run_refresh_job(job_name, config.database.url, "config.yaml")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    return result


@router.post("/api/refresh-jobs/{job_name}/background", response_model=RefreshJobResponse, response_model_exclude_unset=True)
def launch_refresh_job_background(
    job_name: str,
    background_tasks: BackgroundTasks,
    config: AppConfig = Depends(dependencies.get_config),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    dsn = config.database.url
    try:
        job = job_control.start_refresh_job(job_name, dsn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job_control.schedule_created_job(background_tasks, job, job_name, dsn)
    return job


@router.get("/api/settings", response_model=SettingsResponse, response_model_exclude_unset=True)
def settings(config: AppConfig = Depends(dependencies.get_config)) -> dict[str, Any]:
    config, panel_data = _settings_context(config, reload_config=True)
    return settings_owner.settings_payload(config, panel_data)


@router.patch("/api/settings/agents", response_model=SettingsResponse, response_model_exclude_unset=True)
def update_agent_settings(
    payload: AgentSettingsInput,
    config: AppConfig = Depends(dependencies.get_config),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    try:
        settings_owner.persist_setting_section(config, "agents", payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    config, panel_data = _settings_context(config, reload_config=True)
    return settings_owner.settings_payload(config, panel_data)


@router.patch("/api/settings/research-sources", response_model=SettingsResponse, response_model_exclude_unset=True)
def update_research_sources(
    payload: ResearchSourcesInput,
    config: AppConfig = Depends(dependencies.get_config),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    try:
        settings_owner.persist_setting_section(config, "research_sources", payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    config, panel_data = _settings_context(config, reload_config=True)
    return settings_owner.settings_payload(config, panel_data)


def _settings_context(config: AppConfig, *, reload_config: bool = False) -> tuple[AppConfig, Any]:
    """Load only source-run rows required by Settings."""

    return panel_snapshot.context(
        cache_key="settings",
        loader=lambda config: loaders.load_panel_data(
            config,
            table_names=("source_runs",),
            query_row_limits={"source_runs": 200},
        ),
        config_loader=load_config if reload_config else lambda: config,
    )


__all__ = ["router"]
