"""Persisted local refresh-job launcher for the API."""

from __future__ import annotations

import argparse
from datetime import timedelta
import json
import os
import subprocess
import sys
import traceback
from typing import Any, Callable
from investment_panel.core.config import load_config
from investment_panel.database.authority import database_url, runtime_for_url
from investment_panel.database.jobs import JobRepository
from investment_panel.jobs import (
    postgres_refresh,
    refresh_options_radar,
    run_option_agents,
    snapshot_database,
    update_ibkr_options,
    update_broker_sources,
    update_market_data,
    update_robinhood_options,
)
from investment_panel.database.retention import RetentionRepository


JobRunner = Callable[[str | None], dict[str, Any]]

JOB_TIMEOUT_SECONDS: dict[str, int] = {
    "options_radar_hard_refresh": 5400,
}


def _job_timeout_seconds(job_name: str) -> int | None:
    env_key = f"MARKET_REFRESH_JOB_TIMEOUT_{job_name.upper()}"
    raw = os.environ.get(env_key)
    if raw is not None:
        try:
            value = int(raw.strip())
            return value if value > 0 else None
        except ValueError:
            pass
    return JOB_TIMEOUT_SECONDS.get(job_name)


def run_options_radar_hard_refresh(config_path: str | None = "config.yaml") -> dict[str, Any]:
    """Pull fresh option chains, then rematerialize the visible radar snapshot."""

    source = update_robinhood_options.run(config_path)
    source_status = str(source.get("status") or "").strip().lower()
    if source_status not in {"ok", "partial"}:
        return {
            "ok": False,
            "status": "failed",
            "failedStep": "update_robinhood_options",
            "error": f"Robinhood option refresh returned {source_status or 'unknown'}",
            "source": source,
        }
    source_symbols = source.get("symbols")
    radar_symbols = [str(symbol).upper() for symbol in source_symbols if symbol] if isinstance(source_symbols, list) else None
    if radar_symbols == []:
        radar = {"status": "skipped", "reason": "no_incremental_symbols", "source": "robinhood"}
    else:
        radar = refresh_options_radar.run_signal_only(config_path, symbols=radar_symbols, source="robinhood")
    return {
        "ok": True,
        "status": "succeeded",
        "source": source,
        "options_radar": radar,
    }


ALLOWLIST: dict[str, JobRunner] = {
    "full_market_refresh": lambda config_path: postgres_refresh.full(config_path, continue_on_error=True),
    "hourly_options_radar": lambda config_path: refresh_options_radar.run_signal_only(config_path),
    "premarket_options_intelligence": lambda config_path: postgres_refresh.premarket(config_path),
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
    "options_radar_hard_refresh": run_options_radar_hard_refresh,
    "refresh_options_radar_learning_marks": lambda config_path: refresh_options_radar.run_learning_marks(config_path),
    "run_option_agents": lambda config_path: run_option_agents.run(config_path),
    # Manual run: forces the consolidated agent over the full open queue whenever a
    # command is configured, independent of the auto-run (enabled) toggle.
    "run_option_agents_force": lambda config_path: run_option_agents.run(config_path, force=True),
    # On-demand run: processes only user-requested (ondemand:) thesis requests.
    "run_option_agents_ondemand": lambda config_path: run_option_agents.run(config_path, ondemand=True),
    "update_broker_sources": lambda config_path: update_broker_sources.run(config_path),
    "update_market_data": lambda config_path: update_market_data.run(config_path),
    # Preserve the established UI/automation job names while routing them to
    # PostgreSQL-native implementations.
    "update_free_sources": lambda config_path: update_market_data.run(config_path),
    "update_free_sources_radar": lambda config_path: update_market_data.run(config_path),
    "update_market_environment": lambda config_path: update_market_data.run(config_path),
    "postgres_retention": lambda config_path: RetentionRepository(
        runtime_for_url(database_url(load_config(config_path)))
    ).prune(),
    "snapshot_database": lambda config_path: snapshot_database.run(config_path),
}


def refresh_job_rows(db_path: Any) -> list[dict[str, Any]]:
    repository = _job_repository(db_path)
    repository.mark_stale()
    return repository.rows()


def fail_running_jobs(db_path: Any, reason: str) -> int:
    return _job_repository(db_path).fail_all_running(reason)


def mark_stale_running_jobs(
    db_path: Any,
    *,
    stale_after: timedelta = timedelta(hours=3),
    retries: int = 30,
) -> int:
    return _job_repository(db_path).mark_stale(stale_after=stale_after)


def start_refresh_job(job_name: str, db_path: Any) -> dict[str, Any]:
    if job_name not in ALLOWLIST:
        allowed = ", ".join(sorted(ALLOWLIST))
        raise ValueError(f"refresh job is not allowlisted: {job_name}. Allowed jobs: {allowed}")

    return _job_repository(db_path).start(job_name)


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
    repository = _job_repository(db_path, config_path)
    try:
        summary = ALLOWLIST[job_name](config_path)
    except Exception as exc:
        error = f"{exc}\n{traceback.format_exc()}"
        repository.finish(job_id, "failed", error=error, summary={"error": str(exc)})
        if raise_on_error:
            raise
        return {"id": job_id, "job_name": job_name, "status": "failed", "error": str(exc)}

    failure = summary_failure_message(summary)
    if failure:
        repository.finish(job_id, "failed", error=failure, summary=summary)
        return {"id": job_id, "job_name": job_name, "status": "failed", "error": failure, "summary": summary}

    repository.finish(job_id, "succeeded", summary=summary)
    return {"id": job_id, "job_name": job_name, "status": "succeeded", "summary": summary}


def finish_refresh_job_failed(job_id: str, job_name: str, db_path: Any, error: str) -> dict[str, Any]:
    _job_repository(db_path).finish(job_id, "failed", error=error)
    return {"id": job_id, "job_name": job_name, "status": "failed", "error": error}


def execute_refresh_job_subprocess(
    job_id: str,
    job_name: str,
    db_path: Any,
    config_path: str | None = "config.yaml",
) -> dict[str, Any]:
    repository = _job_repository(db_path, config_path)
    command = [
        sys.executable,
        "-m",
        "investment_panel.core.refresh_jobs",
        job_name,
        "--job-id",
        job_id,
        "--config",
        config_path or "config.yaml",
    ]
    child_environment = {**os.environ, "MARKET_DATABASE_URL": repository.runtime.dsn}
    timeout_seconds = _job_timeout_seconds(job_name)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            env=child_environment,
        )
    except subprocess.TimeoutExpired:
        return finish_refresh_job_failed(
            job_id,
            job_name,
            db_path,
            f"refresh subprocess timed out after {timeout_seconds}s",
        )
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        detail = stderr or stdout or f"refresh subprocess exited with code {completed.returncode}"
        return finish_refresh_job_failed(
            job_id,
            job_name,
            db_path,
            f"refresh subprocess exited with code {completed.returncode}: {detail[-2000:]}",
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"id": job_id, "job_name": job_name, "status": "succeeded", "summary": {"stdout": stdout[-2000:]}}


def run_refresh_job(job_name: str, db_path: Any, config_path: str | None = "config.yaml") -> dict[str, Any]:
    job = start_refresh_job(job_name, db_path)
    if not job.get("created"):
        return job
    return execute_refresh_job(job["id"], job_name, db_path, config_path)


def _job_repository(database: Any, config_path: str | None = "config.yaml") -> JobRepository:
    if isinstance(database, str) and database.startswith(("postgresql://", "postgresql+psycopg://")):
        dsn = database
    elif isinstance(database, dict) or getattr(database, "database", None) is not None:
        dsn = database_url(database)
    else:
        dsn = load_config(config_path).database.url
    return JobRepository(runtime_for_url(dsn))


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_name")
    parser.add_argument("--db-path", help="Deprecated non-secret database reference")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--job-id")
    args = parser.parse_args(argv)

    if args.job_id:
        result = execute_refresh_job(args.job_id, args.job_name, args.db_path, args.config, raise_on_error=False)
    else:
        result = run_refresh_job(args.job_name, args.db_path, args.config)
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
