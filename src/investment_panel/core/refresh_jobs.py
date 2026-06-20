"""Persisted local refresh-job launcher for the API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import traceback
from typing import Any, Callable
from uuid import uuid4

from investment_panel.core.db import db, init_db, json_dumps, query_rows
from investment_panel.jobs import (
    daily_screen,
    full_market_refresh,
    hourly_options_radar,
    premarket_options_intelligence,
    refresh_options_radar,
    run_option_agents,
    refresh_decision_models,
    update_arco_data,
    update_disclosures,
    update_broker_sources,
    update_event_calendar,
    update_free_sources,
    update_ibkr_options,
    update_market_environment,
    update_preopen_daily_brief,
    update_research_sources,
    update_robinhood_options,
    update_social_sources,
)


JobRunner = Callable[[str | None], dict[str, Any]]


ALLOWLIST: dict[str, JobRunner] = {
    "full_market_refresh": lambda config_path: full_market_refresh.run(config_path, continue_on_error=True),
    "refresh_decision_models": lambda config_path: refresh_decision_models.run(config_path),
    "daily_screen": lambda config_path: daily_screen.run(config_path, online_check=False),
    "hourly_options_radar": lambda config_path: hourly_options_radar.run(config_path),
    "premarket_options_intelligence": lambda config_path: premarket_options_intelligence.run(config_path),
    "update_arco_data": lambda config_path: update_arco_data.run(config_path),
    "update_free_sources": lambda config_path: update_free_sources.run(config_path, analyses=True),
    "update_market_environment": lambda config_path: update_market_environment.run(config_path),
    "update_preopen_daily_brief": lambda config_path: update_preopen_daily_brief.run(config_path),
    # Radar-focused source pull for the continuous scheduler: TradingView option
    # chains/quotes plus yfinance option liquidity (open interest / volume), which
    # the radar data contract requires for trade-readiness. The equity-price
    # refresh and run_all_analyses pass are skipped — they are not needed for
    # radar option freshness and run in the daily full_market_refresh.
    "update_free_sources_radar": lambda config_path: update_free_sources.run(
        config_path, equity_data=False, tradingview=True, yfinance=True, analyses=False
    ),
    # IBKR option chains (price/greeks/OI/volume) persisted as source='ibkr' — the
    # reliable option source replacing the rate-limited TradingView+yfinance combo.
    "update_ibkr_options": lambda config_path: update_ibkr_options.run(config_path),
    # Robinhood option chains (price/greeks/OI/volume) persisted as
    # source='robinhood'. This is market-data only; no account or order tools.
    "update_robinhood_options": lambda config_path: update_robinhood_options.run(config_path),
    "refresh_options_radar": lambda config_path: refresh_options_radar.run(config_path),
    # Agent-free rematerialization for the in-process continuous scheduler. Codex
    # thesis/postmortem workers stay on the daily premarket cadence; this path
    # only recomputes deterministic option math, gates, and ranking.
    "refresh_options_radar_deterministic": lambda config_path: refresh_options_radar.run_deterministic_only(config_path),
    # Fast fresh-signal rematerialization (no agents, no heavy learning pass) for
    # the continuous 15-min loop; the full deterministic refresh (with learning)
    # runs on a slower cadence.
    "refresh_options_radar_signal": lambda config_path: refresh_options_radar.run_signal_only(config_path),
    # IBKR-scoped fast signal refresh for the cutover: rematerializes from the
    # reliable source='ibkr' chains only (clean OI/volume/greeks, no peer conflict).
    "refresh_options_radar_signal_ibkr": lambda config_path: refresh_options_radar.run_signal_only(config_path, source="ibkr"),
    "refresh_options_radar_signal_robinhood": lambda config_path: refresh_options_radar.run_signal_only(config_path, source="robinhood"),
    "run_option_agents": lambda config_path: run_option_agents.run(config_path),
    # Manual run: forces the consolidated agent over the full open queue whenever a
    # command is configured, independent of the auto-run (enabled) toggle.
    "run_option_agents_force": lambda config_path: run_option_agents.run(config_path, force=True),
    # On-demand run: processes only user-requested (ondemand:) thesis requests.
    "run_option_agents_ondemand": lambda config_path: run_option_agents.run(config_path, ondemand=True),
    "update_broker_sources": lambda config_path: update_broker_sources.run(config_path),
    # Live opencli social (X list + per-account fallback) and research (news +
    # blogs) ingestion. Both record source_runs with ok/rate_limited/failed.
    "update_social_sources": lambda config_path: update_social_sources.run(config_path),
    "update_research_sources": lambda config_path: update_research_sources.run(config_path),
    "update_disclosures": lambda config_path: update_disclosures.run(config_path, online_check=False, max_filings=3, fetch_holdings=False),
    "update_event_calendar": lambda config_path: update_event_calendar.run(config_path),
}


def refresh_job_rows(db_path: Any) -> list[dict[str, Any]]:
    try:
        init_db(db_path)
        mark_stale_running_jobs(db_path)
        read_only = False
    except Exception as exc:
        if "different configuration than existing connections" not in str(exc):
            raise
        read_only = True
    with db(db_path, read_only=read_only) as con:
        rows = query_rows(
            con,
            """
            SELECT id, job_name, status, started_at, finished_at, error, summary
            FROM refresh_jobs
            ORDER BY started_at DESC
            LIMIT 50
            """,
        )
    for row in rows:
        row["summary"] = parse_json(row.get("summary"))
    return rows


def fail_running_jobs(db_path: Any, reason: str) -> int:
    init_db(db_path)
    finished_at = datetime.now(UTC)
    with db(db_path, read_only=False) as con:
        result = con.execute(
            """
            UPDATE refresh_jobs
            SET status = 'failed', finished_at = ?, error = ?, summary = ?
            WHERE status = 'running'
            """,
            [finished_at, reason, json_dumps({"error": reason})],
        )
        return int(result.fetchone()[0] if result.description else 0)


def mark_stale_running_jobs(db_path: Any, *, stale_after: timedelta = timedelta(hours=3)) -> int:
    cutoff = datetime.now(UTC) - stale_after
    reason = f"Refresh job did not finish within {stale_after}."
    with db(db_path, read_only=False) as con:
        result = con.execute(
            """
            UPDATE refresh_jobs
            SET status = 'failed', finished_at = ?, error = ?, summary = ?
            WHERE status = 'running'
              AND started_at < ?
            """,
            [datetime.now(UTC), reason, json_dumps({"error": reason}), cutoff],
        )
        return int(result.fetchone()[0] if result.description else 0)


def start_refresh_job(job_name: str, db_path: Any) -> dict[str, Any]:
    if job_name not in ALLOWLIST:
        allowed = ", ".join(sorted(ALLOWLIST))
        raise ValueError(f"refresh job is not allowlisted: {job_name}. Allowed jobs: {allowed}")

    init_db(db_path)
    mark_stale_running_jobs(db_path)
    job_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{job_name}-{uuid4().hex[:8]}"
    started_at = datetime.now(UTC)
    with db(db_path, read_only=False) as con:
        existing = query_rows(
            con,
            """
            SELECT id, job_name, status, started_at
            FROM refresh_jobs
            WHERE job_name = ?
              AND status = 'running'
            ORDER BY started_at DESC
            LIMIT 1
            """,
            [job_name],
        )
        if existing:
            return {**existing[0], "created": False}
        con.execute(
            """
            INSERT INTO refresh_jobs (id, job_name, status, started_at, finished_at, error, summary)
            VALUES (?, ?, 'running', ?, NULL, NULL, '{}')
            """,
            [job_id, job_name, started_at],
        )
    return {"id": job_id, "job_name": job_name, "status": "running", "started_at": started_at, "created": True}


def execute_refresh_job(
    job_id: str,
    job_name: str,
    db_path: Any,
    config_path: str | None = "config.yaml",
    *,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    if job_name not in ALLOWLIST:
        allowed = ", ".join(sorted(ALLOWLIST))
        raise ValueError(f"refresh job is not allowlisted: {job_name}. Allowed jobs: {allowed}")
    try:
        summary = ALLOWLIST[job_name](config_path)
    except Exception as exc:
        error = f"{exc}\n{traceback.format_exc()}"
        with db(db_path, read_only=False) as con:
            con.execute(
                """
                UPDATE refresh_jobs
                SET status = 'failed', finished_at = ?, error = ?, summary = ?
                WHERE id = ?
                """,
                [datetime.now(UTC), error, json_dumps({"error": str(exc)}), job_id],
            )
        if raise_on_error:
            raise
        return {"id": job_id, "job_name": job_name, "status": "failed", "error": str(exc)}

    failure = summary_failure_message(summary)
    if failure:
        with db(db_path, read_only=False) as con:
            con.execute(
                """
                UPDATE refresh_jobs
                SET status = 'failed', finished_at = ?, error = ?, summary = ?
                WHERE id = ?
                """,
                [datetime.now(UTC), failure, json_dumps(summary), job_id],
            )
        return {"id": job_id, "job_name": job_name, "status": "failed", "error": failure, "summary": summary}

    with db(db_path, read_only=False) as con:
        con.execute(
            """
            UPDATE refresh_jobs
            SET status = 'succeeded', finished_at = ?, error = NULL, summary = ?
            WHERE id = ?
            """,
            [datetime.now(UTC), json_dumps(summary), job_id],
        )
    return {"id": job_id, "job_name": job_name, "status": "succeeded", "summary": summary}


def run_refresh_job(job_name: str, db_path: Any, config_path: str | None = "config.yaml") -> dict[str, Any]:
    job = start_refresh_job(job_name, db_path)
    if not job.get("created"):
        return job
    return execute_refresh_job(job["id"], job_name, db_path, config_path)


def parse_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def summary_failure_message(summary: Any) -> str | None:
    if not isinstance(summary, dict):
        return None
    if summary.get("ok") is not False and summary.get("status") != "failed":
        return None
    error = summary.get("error")
    if isinstance(error, str) and error:
        return error
    source_errors = summary.get("source_errors")
    if isinstance(source_errors, list):
        failed_sources = [
            str(item.get("name") or "").strip()
            for item in source_errors
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        if failed_sources:
            return f"Refresh failed for sources: {', '.join(failed_sources[:3])}"
    failed_step = summary.get("failedStep")
    if isinstance(failed_step, str) and failed_step:
        return f"Refresh failed at {failed_step}"
    return "Refresh failed"
