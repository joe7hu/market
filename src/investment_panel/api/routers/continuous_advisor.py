"""Decision-first read surface and narrow controls for the continuous advisor."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from investment_panel.api import dependencies
from investment_panel.application.read_models import panel_snapshot
from investment_panel.api.contracts import ContinuousAdvisorSettingsInput
from investment_panel.api.data_access import continuous_advisor as continuous_owner
from investment_panel.api.data_access import settings as settings_owner
from investment_panel.api.response_contracts import (
    ContinuousAdvisorPromptStatusResponse,
    ContinuousAdvisorReplayResponse,
    ContinuousAdvisorResponse,
    ContinuousAdvisorRunDetailResponse,
    ContinuousAdvisorRunsResponse,
    ContinuousAdvisorTickerResponse,
    ContinuousAdvisorTickersResponse,
)
from investment_panel.settings import AppConfig


router = APIRouter()


@router.get("/api/continuous-advisor", response_model=ContinuousAdvisorResponse, response_model_exclude_unset=True)
def continuous_advisor(
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    return continuous_owner.overview(config)


@router.get("/api/continuous-advisor/tickers", response_model=ContinuousAdvisorTickersResponse, response_model_exclude_unset=True)
def continuous_advisor_tickers(
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    return continuous_owner.tickers(config)


@router.get("/api/continuous-advisor/tickers/{symbol}", response_model=ContinuousAdvisorTickerResponse, response_model_exclude_unset=True)
def continuous_advisor_ticker(
    symbol: str,
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    try:
        normalized = continuous_owner.normalize_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    brief = continuous_owner.ticker(config, normalized)
    if brief is None:
        raise HTTPException(status_code=404, detail=f"No continuous-advisor brief for {normalized}")
    return brief


@router.get("/api/continuous-advisor/runs", response_model=ContinuousAdvisorRunsResponse, response_model_exclude_unset=True)
def continuous_advisor_runs(
    symbol: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    try:
        normalized = continuous_owner.normalize_symbol(symbol) if symbol is not None else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return continuous_owner.runs(config, symbol=normalized, limit=limit)


@router.get("/api/continuous-advisor/replay", response_model=ContinuousAdvisorReplayResponse, response_model_exclude_unset=True)
def continuous_advisor_replay(
    prompt_version: str | None = None,
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    return continuous_owner.replay(config, prompt_version=prompt_version)


@router.get("/api/continuous-advisor/runs/{task_id}", response_model=ContinuousAdvisorRunDetailResponse, response_model_exclude_unset=True)
def continuous_advisor_run_detail(
    task_id: str,
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    detail = continuous_owner.run_detail(config, task_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Continuous-advisor run not found")
    return detail


@router.get("/api/continuous-advisor/prompts", response_model=ContinuousAdvisorPromptStatusResponse, response_model_exclude_unset=True)
def continuous_advisor_prompts(
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    return continuous_owner.prompts(config)


@router.patch("/api/continuous-advisor/settings", response_model=ContinuousAdvisorResponse, response_model_exclude_unset=True)
def update_continuous_advisor_settings(
    payload: ContinuousAdvisorSettingsInput,
    config: AppConfig = Depends(dependencies.get_config),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    update = payload.model_dump(exclude_none=True)
    thesis_update = {
        key: value for source, target in (("enabled", "continuous_enabled"), ("cadence_minutes", "continuous_cadence_minutes"), ("budget_usd", "continuous_budget_usd"))
        if (value := update.get(source)) is not None
        for key in (target,)
    }
    if thesis_update:
        try:
            settings_owner.persist_setting_section(config, "agents", {"thesis_monitor": thesis_update})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    panel_snapshot.invalidate_context_cache()
    active = replace(
        config,
        agents=replace(
            config.agents,
            thesis_monitor=replace(config.agents.thesis_monitor, **thesis_update),
        ),
    )
    return continuous_owner.overview(active)


__all__ = ["router"]
