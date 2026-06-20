"""In-process continuous refresh scheduler.

The API process already owns the DuckDB writer, so the deterministic options
radar is refreshed here on a timer instead of by an external launchd job that
skipped whenever the app was running. Closing the browser does not stop this:
it lives in the long-running uvicorn process, not the frontend.

Core market-data cadences run here so they continue while the app is open:

- ``update_free_sources`` pulls fresh option chains / quotes (rate-limited
  upstream, so it runs on a slower cadence).
- ``refresh_options_radar_deterministic`` rematerializes option math, gates,
  ranking, and opportunities from whatever chains are present (cheap, frequent).
- ``update_market_environment`` keeps the broad-market valuation charts and
  asset matrix current for the Market page.

Job execution reuses ``run_refresh_job``, which records job rows and refuses to
start a second copy of a job that is already running, so overlapping ticks (or
multiple uvicorn workers) cannot pile up duplicate refreshes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from investment_panel.core.refresh_jobs import run_refresh_job

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


def scheduler_enabled() -> bool:
    return os.environ.get("MARKET_SCHEDULER_ENABLED", "1").strip().lower() not in _TRUTHY_OFF


def job_intervals(config: Any | None = None) -> dict[str, int]:
    """Job name -> minimum seconds between runs.

    The deterministic refresh is the continuous loop (cheap, in-process, no DB
    lock contention). The source pull is heavier — it fetches option chains over
    the universe and holds the DuckDB write lock for the duration — so it runs
    hourly and can be disabled with ``MARKET_SOURCE_REFRESH_SECONDS=0`` for users
    who rely on the daily full refresh for source freshness instead.
    """

    # Option source for the radar: 'robinhood' (default — read-only MCP chains
    # with OI/volume/greeks), 'ibkr' (Gateway fallback), or 'free' (legacy
    # TradingView+yfinance fallback).
    option_source = os.environ.get("MARKET_RADAR_OPTION_SOURCE", "robinhood").strip().lower()
    if option_source == "free":
        signal_job, source_job = "refresh_options_radar_signal", "update_free_sources_radar"
    elif option_source == "ibkr":
        signal_job, source_job = "refresh_options_radar_signal_ibkr", "update_ibkr_options"
    else:
        signal_job, source_job = "refresh_options_radar_signal_robinhood", "update_robinhood_options"

    # Fast fresh-signal rematerialization (no heavy learning pass) — runs often so
    # the radar reflects the latest chains and ranking quickly without reprocessing
    # the full event-sourced history each cycle.
    intervals: dict[str, int] = {signal_job: _env_int("MARKET_RADAR_REFRESH_SECONDS", 900)}
    # Source pull (option chains). Hourly by default; 0 disables (e.g. rely on the
    # daily full_market_refresh for source freshness).
    source_seconds = _env_int("MARKET_SOURCE_REFRESH_SECONDS", 3600, allow_zero=True)
    if source_seconds > 0:
        intervals[source_job] = source_seconds
    # Full deterministic refresh incl. learning/attribution/cohorts — heavy, so it
    # runs on a slow cadence (default 6h). 0 disables (daily full_market_refresh
    # also covers it).
    learning_seconds = _env_int("MARKET_LEARNING_REFRESH_SECONDS", 21600, allow_zero=True)
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
    agent_seconds = _env_int("MARKET_AGENT_REFRESH_SECONDS", 86400, allow_zero=True)
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
    # Live opencli social (X) ingestion — conservative ~30 min by default; 0 disables.
    social_seconds = _env_int("MARKET_SOCIAL_REFRESH_SECONDS", 1800, allow_zero=True)
    if social_seconds > 0:
        intervals["update_social_sources"] = social_seconds
    # Live opencli research (news + blogs) ingestion — hourly by default; 0 disables.
    research_seconds = _env_int("MARKET_RESEARCH_REFRESH_SECONDS", 3600, allow_zero=True)
    if research_seconds > 0:
        intervals["update_research_sources"] = research_seconds
    # Broad-market valuation and asset-matrix inputs for /market. These are small
    # enough to refresh hourly and need to run while the API owns the DuckDB writer.
    market_environment_seconds = _env_int("MARKET_ENVIRONMENT_REFRESH_SECONDS", 3600, allow_zero=True)
    if market_environment_seconds > 0:
        intervals["update_market_environment"] = market_environment_seconds
    # Pre-open macro / key-events brief for /today. This is a frequent gated
    # check, not a 24h-from-process-start refresh: the job writes only once
    # during the New York pre-open window and otherwise returns skipped.
    preopen_brief_seconds = _env_int("MARKET_PREOPEN_BRIEF_REFRESH_SECONDS", 300, allow_zero=True)
    if preopen_brief_seconds > 0:
        intervals["update_preopen_daily_brief_scheduled"] = preopen_brief_seconds
    return intervals


def scheduler_status(config: Any | None = None) -> dict[str, Any]:
    """Expose the actual scheduler plan in the shape the UI already consumes."""

    intervals = job_intervals(config)
    option_source = os.environ.get("MARKET_RADAR_OPTION_SOURCE", "robinhood").strip().lower()
    return {
        "enabled": os.environ.get("MARKET_SCHEDULER_ENABLED", "1"),
        "jobs": intervals,
        "agent_refresh_seconds": str(intervals.get("run_option_agents", 0)),
        "radar_refresh_seconds": str(_first_interval(intervals, "refresh_options_radar_signal")),
        "source_refresh_seconds": str(_first_interval(intervals, "update_free_sources_radar", "update_ibkr_options", "update_robinhood_options")),
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


async def run_scheduler(db_path: Path, config_path: str = "config.yaml") -> None:
    intervals = job_intervals()
    warmup = _env_int("MARKET_SCHEDULER_WARMUP_SECONDS", 20, allow_zero=True)
    logger.info("market scheduler starting (warmup=%ss, intervals=%s)", warmup, intervals)

    # Stagger first runs after warmup so the source pull lands before the first
    # deterministic rematerialization, and the two jobs do not contend at t0.
    start = time.monotonic() + warmup
    next_due: dict[str, float] = {job: start + offset * STAGGER_SECONDS for offset, job in enumerate(intervals)}
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


async def _dispatch(job: str, db_path: Path, config_path: str) -> None:
    try:
        result: Any = await asyncio.to_thread(run_refresh_job, job, db_path, config_path)
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
