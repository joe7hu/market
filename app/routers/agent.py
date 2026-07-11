"""Agent control-plane routes: overview (config + queue + cost + history) and
on-demand per-ticker analysis. The Agent page is the single control surface; edits
to the agent config go through /api/settings/agents (see system.py)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app import deps

router = APIRouter()


@router.get("/api/agent")
def agent_overview() -> dict[str, Any]:
    app_config = deps.load_core_config()
    agents = deps.config_to_dict(app_config)["agents"]
    from investment_panel.database.agents import AgentRepository
    from investment_panel.database.authority import runtime_for_config

    overview = AgentRepository(runtime_for_config(app_config)).overview()
    return {
        "config": agents.get("option_agent", {}),
        "pricing": agents.get("pricing", {}),
        "queue": overview["queue"],
        "runs": overview["runs"],
        "cost": overview["cost"],
        "scheduler": {"agent_refresh_seconds": _scheduler_agent_seconds(app_config)},
    }


@router.post("/api/agent/analyze")
def analyze_ticker(payload: deps.AgentAnalyzeInput, request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    deps._require_local_request(request)
    app_config = deps.load_core_config()
    ticker = str(payload.ticker or "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")
    option_agent = app_config.agents.option_agent
    # On-demand only needs a configured command — it does NOT depend on the auto-run
    # (enabled) toggle.
    if not option_agent.command:
        raise HTTPException(status_code=400, detail="Set the option agent command before running on-demand analysis.")
    from investment_panel.database.agents import AgentRepository
    from investment_panel.database.authority import runtime_for_config

    req = AgentRepository(runtime_for_config(app_config)).queue_thesis(
        ticker, prompt=payload.prompt or "", trigger="ondemand"
    )
    # On-demand pass: runs only the user-requested ticker(s), records an agent_runs
    # row (trigger=ondemand) and the resulting thesis — like the auto-run pass.
    database_url = app_config.database.url
    job = deps.start_refresh_job("run_option_agents_ondemand", database_url)
    if job.get("created"):
        background_tasks.add_task(deps._execute_background_refresh_job, job["id"], "run_option_agents_ondemand", database_url)
    deps._invalidate_context_cache()
    return {"ticker": ticker, "request_id": req["request_id"], "job": job}


def _scheduler_agent_seconds(app_config: Any) -> int:
    import os

    configured = int(app_config.agents.option_agent.auto_run_seconds or 0)
    if configured > 0:
        return configured
    try:
        return int(os.environ.get("MARKET_AGENT_REFRESH_SECONDS", "0") or 0)
    except ValueError:
        return 0
