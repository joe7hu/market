"""Agent control-plane routes: overview (config + queue + cost + history) and
on-demand per-ticker analysis. The Agent page is the single control surface; edits
to the agent config go through /api/settings/agents (see system.py)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app.actions.agents import AgentActions
from app import job_control, panel_snapshot
from app import dependencies
from app.contracts import AgentAnalyzeInput
from app.data_access import loaders
from app.response_contracts import (
    AgentAnalyzeResponse,
    AgentExperimentResponse,
    AgentOverviewResponse,
    AgentResearchPromptResponse,
)
from investment_panel.core.config import AppConfig
from investment_panel.core.daily_research_prompt import build_daily_research_prompt

router = APIRouter()


@router.get("/api/agent", response_model=AgentOverviewResponse, response_model_exclude_unset=True)
def agent_overview(actions: AgentActions = Depends(dependencies.get_agent_actions)) -> dict[str, Any]:
    return actions.overview()


@router.get("/api/agent/experiments/current", response_model=AgentExperimentResponse, response_model_exclude_unset=True)
def current_agent_experiment(actions: AgentActions = Depends(dependencies.get_agent_actions)) -> dict[str, Any]:
    """Expose only the experiment conclusion and gates, never batch rows."""

    return actions.current_experiment()


@router.get("/api/agent/research-prompt", response_model=AgentResearchPromptResponse, response_model_exclude_unset=True)
def agent_research_prompt(config: AppConfig = Depends(dependencies.get_config)) -> dict[str, Any]:
    _, research_data = panel_snapshot.context(
        cache_key="agent:daily-research",
        loader=loaders.load_daily_research_panel_data,
        config_loader=lambda: config,
    )
    research_prompt = build_daily_research_prompt(
        research_data.tables,
        status={
            "ready": research_data.status.ready,
            "message": research_data.status.message,
            "source": research_data.status.source,
            "metadata": research_data.metadata,
        },
    )
    return research_prompt


@router.post("/api/agent/analyze", response_model=AgentAnalyzeResponse, response_model_exclude_unset=True)
def analyze_ticker(
    payload: AgentAnalyzeInput,
    background_tasks: BackgroundTasks,
    actions: AgentActions = Depends(dependencies.get_agent_actions),
    config: AppConfig = Depends(dependencies.get_config),
    _request=Depends(dependencies.get_authorized_request),
) -> dict[str, Any]:
    try:
        result = actions.queue_analysis(payload.ticker, prompt=payload.prompt or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job = result["job"]
    if job.get("created"):
        background_tasks.add_task(
            job_control.execute_background_refresh_job,
            job["id"],
            "run_option_agents_ondemand",
            config.database.url,
        )
    panel_snapshot.invalidate_context_cache()
    return result
