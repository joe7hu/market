"""Agent control-plane routes: overview (config + queue + cost + history) and
on-demand per-ticker analysis. The Agent page is the single control surface; edits
to the agent config go through /api/settings/agents (see system.py)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app import deps
from investment_panel.core.options_radar import DEFAULT_STRATEGY_VERSION

router = APIRouter()


@router.get("/api/agent")
def agent_overview() -> dict[str, Any]:
    app_config = deps.load_core_config()
    agents = deps.config_to_dict(app_config)["agents"]
    db_path = app_config.database.duckdb_path
    deps.init_db(db_path)
    # NOTE: read_only must match the always-on writer connection's config — DuckDB
    # rejects a different-config connection to the same file within the process.
    with deps.db(db_path, read_only=False) as con:
        queue = _queue_stats(con)
        runs = deps.query_rows(con, "SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT 50")
        cost = _cost_summary(con)
    return {
        "config": agents.get("option_agent", {}),
        "pricing": agents.get("pricing", {}),
        "queue": queue,
        "runs": runs,
        "cost": cost,
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
    db_path = app_config.database.duckdb_path
    deps.init_db(db_path)
    with deps.db(db_path, read_only=False) as con:
        req = build_ondemand_request(con, ticker, payload.prompt, option_agent)
    # On-demand pass: runs only the user-requested ticker(s), records an agent_runs
    # row (trigger=ondemand) and the resulting thesis — like the auto-run pass.
    database_url = app_config.database.url
    job = deps.start_refresh_job("run_option_agents_ondemand", database_url)
    if job.get("created"):
        background_tasks.add_task(deps._execute_background_refresh_job, job["id"], "run_option_agents_ondemand", database_url)
    deps._invalidate_context_cache()
    return {"ticker": ticker, "request_id": req["request_id"], "job": job}


def build_ondemand_request(con: Any, ticker: str, prompt: str | None, option_agent: Any) -> dict[str, Any]:
    return deps.build_ondemand_agent_request(
        con,
        ticker,
        strategy_version=DEFAULT_STRATEGY_VERSION,
        custom_prompt=prompt or "",
        context_sources=dict(option_agent.context_sources),
    )


def _queue_stats(con: Any) -> dict[str, Any]:
    thesis = deps.query_rows(con, "SELECT count(*) AS c, min(created_at) AS oldest FROM agent_thesis_request WHERE status = 'open'")[0]
    postmortem = deps.query_rows(con, "SELECT count(*) AS c, min(created_at) AS oldest FROM agent_postmortem_request WHERE status = 'open'")[0]
    thesis_open = int(thesis.get("c") or 0)
    postmortem_open = int(postmortem.get("c") or 0)
    oldest = min([d for d in (thesis.get("oldest"), postmortem.get("oldest")) if d], default=None)
    return {
        "thesis_open": thesis_open,
        "postmortem_open": postmortem_open,
        "total_open": thesis_open + postmortem_open,
        "oldest_open_at": oldest,
    }


def _cost_summary(con: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "today": _cost_window(con, now - timedelta(hours=now.hour, minutes=now.minute, seconds=now.second)),
        "last_7d": _cost_window(con, now - timedelta(days=7)),
    }


def _cost_window(con: Any, since: datetime) -> dict[str, Any]:
    row = deps.query_rows(
        con,
        """
        SELECT count(*) AS runs,
               coalesce(sum(input_tokens), 0) AS input_tokens,
               coalesce(sum(output_tokens), 0) AS output_tokens,
               coalesce(sum(est_cost_usd), 0) AS est_cost_usd
        FROM agent_runs
        WHERE started_at >= ?
        """,
        [since],
    )[0]
    return {
        "runs": int(row.get("runs") or 0),
        "input_tokens": int(row.get("input_tokens") or 0),
        "output_tokens": int(row.get("output_tokens") or 0),
        "est_cost_usd": round(float(row.get("est_cost_usd") or 0.0), 4),
    }


def _scheduler_agent_seconds(app_config: Any) -> int:
    import os

    configured = int(app_config.agents.option_agent.auto_run_seconds or 0)
    if configured > 0:
        return configured
    try:
        return int(os.environ.get("MARKET_AGENT_REFRESH_SECONDS", "0") or 0)
    except ValueError:
        return 0
