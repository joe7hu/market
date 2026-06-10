"""In-process continuous refresh scheduler.

The API process already owns the DuckDB writer, so the deterministic options
radar is refreshed here on a timer instead of by an external launchd job that
skipped whenever the app was running. Closing the browser does not stop this:
it lives in the long-running uvicorn process, not the frontend.

Two cadences, both agent-free (Codex thesis/postmortem workers stay on the
daily premarket job):

- ``update_free_sources`` pulls fresh option chains / quotes (rate-limited
  upstream, so it runs on a slower cadence).
- ``refresh_options_radar_deterministic`` rematerializes option math, gates,
  ranking, and opportunities from whatever chains are present (cheap, frequent).

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


def job_intervals() -> dict[str, int]:
    """Job name -> minimum seconds between runs.

    The deterministic refresh is the continuous loop (cheap, in-process, no DB
    lock contention). The source pull is heavier — it fetches option chains over
    the universe and holds the DuckDB write lock for the duration — so it runs
    hourly and can be disabled with ``MARKET_SOURCE_REFRESH_SECONDS=0`` for users
    who rely on the daily full refresh for source freshness instead.
    """

    # Option source for the radar: 'ibkr' (default — reliable OPRA chains with
    # OI/volume/greeks) or 'free' (legacy TradingView+yfinance fallback).
    option_source = os.environ.get("MARKET_RADAR_OPTION_SOURCE", "ibkr").strip().lower()
    if option_source == "free":
        signal_job, source_job = "refresh_options_radar_signal", "update_free_sources_radar"
    else:
        signal_job, source_job = "refresh_options_radar_signal_ibkr", "update_ibkr_options"

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
    # Daily agent thesis/postmortem pass (Codex workers, LLM-backed). Off by default
    # so the always-on app process never makes surprise LLM runs. Set to 86400 to
    # keep theses fresh in-process while the app stays up 24/7 (the launchd premarket
    # job only runs when the app is down). 0 disables.
    agent_seconds = _env_int("MARKET_AGENT_REFRESH_SECONDS", 0, allow_zero=True)
    if agent_seconds > 0:
        intervals["run_option_agents"] = agent_seconds
    return intervals


async def run_scheduler(db_path: Path, config_path: str = "config.yaml") -> None:
    intervals = job_intervals()
    warmup = _env_int("MARKET_SCHEDULER_WARMUP_SECONDS", 20)
    logger.info("market scheduler starting (warmup=%ss, intervals=%s)", warmup, intervals)

    # Stagger first runs after warmup so the source pull lands before the first
    # deterministic rematerialization, and the two jobs do not contend at t0.
    start = time.monotonic() + warmup
    next_due: dict[str, float] = {job: start + offset * 5.0 for offset, job in enumerate(intervals)}

    try:
        while True:
            now = time.monotonic()
            for job, interval in intervals.items():
                if now >= next_due.get(job, 0.0):
                    await _dispatch(job, db_path, config_path)
                    # Schedule the next run from completion, never from start, so a
                    # long job cannot create a backlog of overlapping runs.
                    next_due[job] = time.monotonic() + interval
            await asyncio.sleep(TICK_SECONDS)
    except asyncio.CancelledError:
        logger.info("market scheduler stopping")
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
