"""In-process continuous refresh scheduler.

Closing the browser does not stop this scheduler: it lives in the long-running
API process. Each due job is handed to a subprocess, while PostgreSQL job rows
and a partial unique index provide cross-process single-flight execution.
Independent jobs can run concurrently without blocking API readers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

from investment_panel.core.refresh_jobs import finish_refresh_job_failed, start_refresh_job

logger = logging.getLogger("market.scheduler")

TICK_SECONDS = 15
STAGGER_SECONDS = 5.0
_TRUTHY_OFF = {"0", "false", "off", "no"}


def _env_int(name: str, default: int, *, allow_zero: bool = False) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return default
    if value < 0:
        return default
    if value == 0:
        return 0 if allow_zero else default
    return value


def _env_int_optional(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value if value > 0 else 0


def _heavy_refresh_enabled() -> bool:
    return os.environ.get("MARKET_IN_PROCESS_HEAVY_REFRESH", "0").strip().lower() not in _TRUTHY_OFF


def scheduler_enabled() -> bool:
    return os.environ.get("MARKET_SCHEDULER_ENABLED", "1").strip().lower() not in _TRUTHY_OFF


def job_intervals(config: Any | None = None) -> dict[str, int]:
    """Job name -> minimum seconds between runs.

    The deterministic refresh is the continuous loop (cheap, in-process, no DB
    lock contention). The source pull is heavier because it fetches option chains
    over the universe, so it runs hourly and can be disabled with
    ``MARKET_SOURCE_REFRESH_SECONDS=0`` for users
    who rely on the daily full refresh for source freshness instead.
    """

    # Option source for the radar: Robinhood by default, or IBKR as a gateway
    # fallback. The former wide free-source collector is intentionally retired.
    option_source = os.environ.get("MARKET_RADAR_OPTION_SOURCE", "robinhood").strip().lower()
    if option_source == "ibkr":
        signal_job, source_job = "refresh_options_radar_signal_ibkr", "update_ibkr_options"
    else:
        option_source = "robinhood"
        signal_job, source_job = "refresh_options_radar_signal_robinhood", "update_robinhood_options"

    heavy_refresh = _heavy_refresh_enabled()
    intervals: dict[str, int] = {}
    if option_source == "robinhood":
        # Default self-healing path: incrementally pull stale Robinhood chains
        # and rebuild only those tickers' radar rows. This keeps /options-radar
        # fresh without the old full-universe writer lock.
        hard_seconds = _env_int_optional("MARKET_OPTIONS_RADAR_HARD_REFRESH_SECONDS")
        if hard_seconds is None:
            # Daily/premarket is the default product cadence. Opt into intraday
            # chain pulls explicitly when they are actually useful.
            hard_seconds = 0
        if hard_seconds > 0:
            intervals["options_radar_hard_refresh"] = hard_seconds
    else:
        # Fallback providers still use the split source/signal jobs.
        radar_seconds = _env_int_optional("MARKET_RADAR_REFRESH_SECONDS")
        if radar_seconds is None:
            radar_seconds = 900
        if radar_seconds > 0:
            intervals[signal_job] = radar_seconds
        source_seconds = _env_int_optional("MARKET_SOURCE_REFRESH_SECONDS")
        if source_seconds is None:
            source_seconds = 3600
        if source_seconds > 0:
            intervals[source_job] = source_seconds
    # Explicit split Robinhood jobs remain available for diagnostics/experiments.
    if option_source == "robinhood":
        radar_seconds = _env_int_optional("MARKET_RADAR_REFRESH_SECONDS")
        if radar_seconds and radar_seconds > 0:
            intervals[signal_job] = radar_seconds
        source_seconds = _env_int_optional("MARKET_SOURCE_REFRESH_SECONDS")
        if source_seconds and source_seconds > 0:
            intervals[source_job] = source_seconds
    # Incremental marks refresh for short-horizon learning. Default off in the normal
    # browser API process because it is unnecessary for the daily decision cadence;
    # enable explicitly when running a learning pass.
    learning_mark_seconds = _env_int_optional("MARKET_LEARNING_MARK_REFRESH_SECONDS")
    if learning_mark_seconds is None:
        learning_mark_seconds = 0
    if learning_mark_seconds > 0:
        intervals["refresh_options_radar_learning_marks"] = learning_mark_seconds
    # Full deterministic refresh incl. learning/attribution/cohorts — heavy, so it
    # runs on a slow cadence (default 6h). 0 disables (daily full_market_refresh
    # also covers it).
    learning_seconds = _env_int_optional("MARKET_LEARNING_REFRESH_SECONDS")
    if learning_seconds is None:
        learning_seconds = 21600 if heavy_refresh else 0
    if learning_seconds > 0:
        intervals["refresh_options_radar_deterministic"] = learning_seconds
    # Daily agent thesis/postmortem pass (Codex workers, LLM-backed). Default daily
    # (86400) so theses/postmortems stay fresh and the calibration loop has graded
    # outcomes feeding it; the runner's per-run request cap bounds spend, and it skips
    # when there are no new FIRE/SETUP candidates since the last run. Set 0 to disable
    # (e.g. to rely solely on the launchd premarket job, which only runs when the app
    # is down).
    # Auto-run is the scheduled pass: it requires the agent's `enabled` (auto-run)
    # toggle. On-demand runs use a separate forced job and are never scheduled here.
    agent_seconds = _env_int_optional("MARKET_AGENT_REFRESH_SECONDS")
    if agent_seconds is None:
        agent_seconds = 86400 if heavy_refresh else 0
    auto_run_enabled = True
    try:
        option_agent = _option_agent_config(config)
        auto_run_enabled = bool(_config_value(option_agent, "enabled", True))
        configured = int(_config_value(option_agent, "auto_run_seconds", 0) or 0)
        if configured > 0:
            agent_seconds = configured
    except Exception:  # noqa: BLE001 - config is best-effort; fall back to the env value
        pass
    if auto_run_enabled and agent_seconds > 0:
        intervals["run_option_agents"] = agent_seconds
    social_seconds = _env_int_optional("MARKET_SOCIAL_REFRESH_SECONDS")
    if social_seconds is None:
        social_seconds = 1800 if heavy_refresh else 0
    if social_seconds > 0:
        intervals["update_social_sources"] = social_seconds
    research_seconds = _env_int_optional("MARKET_RESEARCH_REFRESH_SECONDS")
    if research_seconds is None:
        research_seconds = 3600 if heavy_refresh else 0
    if research_seconds > 0:
        intervals["update_research_sources"] = research_seconds
    market_environment_seconds = _env_int("MARKET_ENVIRONMENT_REFRESH_SECONDS", 0, allow_zero=True)
    if market_environment_seconds > 0:
        intervals["update_market_environment"] = market_environment_seconds
    return intervals


def scheduler_status(config: Any | None = None) -> dict[str, Any]:
    """Expose the actual scheduler plan in the shape the UI already consumes."""

    intervals = job_intervals(config)
    option_source = os.environ.get("MARKET_RADAR_OPTION_SOURCE", "robinhood").strip().lower()
    return {
        "enabled": os.environ.get("MARKET_SCHEDULER_ENABLED", "1"),
        "heavy_refresh_enabled": "1" if _heavy_refresh_enabled() else "0",
        "jobs": intervals,
        "agent_refresh_seconds": str(intervals.get("run_option_agents", 0)),
        "radar_refresh_seconds": str(_first_interval(intervals, "options_radar_hard_refresh", "refresh_options_radar_signal")),
        "source_refresh_seconds": str(_first_interval(intervals, "options_radar_hard_refresh", "update_free_sources_radar", "update_ibkr_options", "update_robinhood_options")),
        "options_hard_refresh_seconds": str(intervals.get("options_radar_hard_refresh", 0)),
        "learning_mark_refresh_seconds": str(intervals.get("refresh_options_radar_learning_marks", 0)),
        "learning_refresh_seconds": str(intervals.get("refresh_options_radar_deterministic", 0)),
        "social_refresh_seconds": str(intervals.get("update_social_sources", 0)),
        "research_refresh_seconds": str(intervals.get("update_research_sources", 0)),
        "market_environment_refresh_seconds": str(intervals.get("update_market_environment", 0)),
        "preopen_brief_refresh_seconds": str(intervals.get("update_preopen_daily_brief_scheduled", 0)),
        "radar_option_source": option_source,
    }


def _first_interval(intervals: dict[str, int], *prefixes: str) -> int:
    for prefix in prefixes:
        for job, seconds in intervals.items():
            if job.startswith(prefix):
                return seconds
    return 0


def _option_agent_config(config: Any | None) -> Any:
    if config is None:
        from investment_panel.core.config import load_config

        config = load_config()
    agents = _config_value(config, "agents", {})
    return _config_value(agents, "option_agent", {})


def _config_value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _initial_delay_seconds(job: str, interval: int, offset: int) -> float:
    if job in {"options_radar_hard_refresh", "update_robinhood_options", "update_ibkr_options", "update_free_sources_radar"}:
        return float(interval)
    return float(offset * STAGGER_SECONDS)


async def run_scheduler(db_path: str, config_path: str = "config.yaml") -> None:
    intervals = job_intervals()
    warmup = _env_int("MARKET_SCHEDULER_WARMUP_SECONDS", 20, allow_zero=True)
    logger.info("market scheduler starting (warmup=%ss, intervals=%s)", warmup, intervals)

    # Stagger first runs after warmup so the source pull lands before the first
    # deterministic rematerialization, and the two jobs do not contend at t0.
    start = time.monotonic() + warmup
    next_due: dict[str, float] = {
        job: start + _initial_delay_seconds(job, interval, offset)
        for offset, (job, interval) in enumerate(intervals.items())
    }
    in_flight: dict[str, asyncio.Task] = {}

    try:
        while True:
            now = time.monotonic()
            for job, task in list(in_flight.items()):
                if task.done():
                    in_flight.pop(job, None)
            for job, interval in intervals.items():
                if now >= next_due.get(job, 0.0) and job not in in_flight:
                    in_flight[job] = asyncio.create_task(_dispatch(job, db_path, config_path))
                    # Schedule from dispatch while each job stays single-flight.
                    # This prevents a long radar/source job from starving unrelated
                    # freshness jobs such as update_market_environment.
                    next_due[job] = time.monotonic() + interval
            await asyncio.sleep(TICK_SECONDS)
    except asyncio.CancelledError:
        logger.info("market scheduler stopping")
        for task in in_flight.values():
            task.cancel()
        if in_flight:
            await asyncio.gather(*in_flight.values(), return_exceptions=True)
        raise


async def _dispatch(job: str, db_path: str, config_path: str) -> None:
    try:
        started: Any = await asyncio.to_thread(start_refresh_job, job, db_path)
        if isinstance(started, dict) and started.get("created"):
            result: Any = await _execute_started_refresh_job(job, str(started["id"]), db_path, config_path)
        else:
            result = started
    except Exception:  # noqa: BLE001 - a bad job must never kill the loop
        logger.exception("scheduled job %s raised", job)
        return
    status = result.get("status") if isinstance(result, dict) else None
    if status == "failed":
        logger.warning("scheduled job %s failed: %s", job, result.get("error"))
    elif status == "running":
        logger.debug("scheduled job %s already running; skipped", job)
    else:
        logger.info("scheduled job %s -> %s", job, status)


async def _execute_started_refresh_job(job: str, job_id: str, db_path: str, config_path: str) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "investment_panel.core.refresh_jobs",
        job,
        "--job-id",
        job_id,
        "--db-path",
        str(db_path),
        "--config",
        config_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        await _terminate_refresh_subprocess(proc)
        await asyncio.to_thread(
            finish_refresh_job_failed,
            job_id,
            job,
            db_path,
            "refresh subprocess cancelled during scheduler shutdown/reload",
        )
        raise
    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        detail = stderr_text or stdout_text or f"refresh subprocess exited with code {proc.returncode}"
        error = f"refresh subprocess exited with code {proc.returncode}: {detail[-2000:]}"
        return await asyncio.to_thread(finish_refresh_job_failed, job_id, job, db_path, error)
    try:
        return json.loads(stdout_text)
    except json.JSONDecodeError:
        return {
            "id": job_id,
            "job_name": job,
            "status": "succeeded",
            "summary": {"stdout": stdout_text[-2000:]},
        }


async def _terminate_refresh_subprocess(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
