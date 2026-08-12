"""Agent control-plane routes: overview (config + queue + cost + history) and
on-demand per-ticker analysis. The Agent page is the single control surface; edits
to the agent config go through /api/settings/agents (see system.py)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app import deps
from app.actions.agents import AgentActions

router = APIRouter()


def _actions() -> AgentActions:
    # Route actions must share the app's request-config seam so test and
    # alternate-runtime callers never fall through to the host's live database.
    return AgentActions(deps.load_config(), deps.start_refresh_job)


@router.get("/api/agent")
def agent_overview() -> dict[str, Any]:
    return _actions().overview()


@router.get("/api/agent/experiments/current")
def current_agent_experiment() -> dict[str, Any]:
    """Expose only the experiment conclusion and gates, never batch rows."""

    return _actions().current_experiment()


@router.get("/api/agent/research-prompt")
def agent_research_prompt() -> dict[str, Any]:
    _, research_data = deps._context(
        cache_key="agent:daily-research",
        loader=deps.load_daily_research_panel_data,
    )
    research_prompt = deps.build_daily_research_prompt(
        research_data.tables,
        status={
            "ready": research_data.status.ready,
            "message": research_data.status.message,
            "source": research_data.status.source,
            "metadata": research_data.metadata,
        },
    )
    return research_prompt


@router.post("/api/agent/analyze")
def analyze_ticker(payload: deps.AgentAnalyzeInput, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    deps._require_local_request(request)
    actions = _actions()
    try:
        result = actions.queue_analysis(payload.ticker, prompt=payload.prompt or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job = result["job"]
    if job.get("created"):
        background_tasks.add_task(
            deps._execute_background_refresh_job,
            job["id"],
            "run_option_agents_ondemand",
            actions.config.database.url,
        )
    deps._invalidate_context_cache()
    return result
