"""In-process continuous refresh scheduler.

Job cadence and identity come from ``core.job_policy``.  This adapter owns only
the async scheduling loop; process execution is delegated separately.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from investment_panel.core.job_policy import (
    STAGGER_SECONDS,
    initial_delay_seconds,
    scheduler_enabled,
    scheduler_intervals as job_intervals,
    scheduler_status,
)
from investment_panel.core.job_execution import RefreshProcessSpec, execute_async, terminate_process
from investment_panel.core.refresh_jobs import finish_refresh_job_failed, start_refresh_job

logger = logging.getLogger("market.scheduler")

TICK_SECONDS = 15


def _initial_delay_seconds(job: str, interval: int, offset: int) -> float:
    return initial_delay_seconds(job, interval, offset, stagger_seconds=STAGGER_SECONDS)


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


async def run_scheduler(db_path: str, config_path: str = "config.yaml") -> None:
    intervals = job_intervals()
    warmup = _env_int("MARKET_SCHEDULER_WARMUP_SECONDS", 20, allow_zero=True)
    logger.info("market scheduler starting (warmup=%ss, intervals=%s)", warmup, intervals)

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
    started_job_id: str | None = None
    try:
        started: Any = await asyncio.to_thread(start_refresh_job, job, db_path)
        if isinstance(started, dict) and started.get("created"):
            started_job_id = str(started["id"])
            result: Any = await _execute_started_refresh_job(job, started_job_id, db_path, config_path)
        else:
            result = started
    except Exception as exc:
        logger.exception("scheduled job %s raised", job)
        if started_job_id is not None:
            try:
                await asyncio.to_thread(
                    finish_refresh_job_failed,
                    started_job_id,
                    job,
                    db_path,
                    f"scheduler failed before refresh execution completed: {exc}",
                )
            except Exception:
                logger.exception("scheduled job %s could not be marked failed", job)
        return
    status = result.get("status") if isinstance(result, dict) else None
    if status == "failed":
        logger.warning("scheduled job %s failed: %s", job, result.get("error"))
    elif status == "running":
        logger.debug("scheduled job %s already running; skipped", job)
    else:
        logger.info("scheduled job %s -> %s", job, status)


async def _execute_started_refresh_job(job: str, job_id: str, db_path: str, config_path: str) -> dict[str, Any]:
    spec = RefreshProcessSpec(
        job_id=job_id,
        job_name=job,
        database_url=str(db_path),
        config_path=config_path,
    )

    async def fail(error: str) -> dict[str, Any]:
        return await asyncio.to_thread(finish_refresh_job_failed, job_id, job, db_path, error)

    return await execute_async(spec, fail)


async def _terminate_refresh_subprocess(proc: asyncio.subprocess.Process) -> None:
    await terminate_process(proc)
