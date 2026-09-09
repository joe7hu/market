"""In-process continuous refresh scheduler.

Job cadence and identity come from ``core.job_policy``.  This adapter owns only
the async scheduling loop; process execution is delegated separately.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
import os
import time
from typing import Any

from investment_panel.core.job_policy import (
    STAGGER_SECONDS,
    initial_delay_seconds,
    scheduler_enabled,
    scheduler_intervals as job_intervals,
    scheduler_status as _configured_scheduler_status,
)
from investment_panel.core.job_execution import RefreshProcessSpec, execute_async, terminate_process
from investment_panel.settings import AppConfig, load_config
from investment_panel.core.refresh_jobs import (
    execute_refresh_job,
    finish_refresh_job_failed,
    mark_stale_running_jobs,
    start_refresh_job,
)
from investment_panel.domain.decision import MARKET_TZ, is_market_open, is_us_market_day, market_session_bounds
from investment_panel.infrastructure.postgres.source_health import overdue_source_refresh_jobs

logger = logging.getLogger("market.scheduler")

TICK_SECONDS = 15
CONTINUOUS_SETTINGS_REFRESH_SECONDS = 60
SCHEDULER_CAPACITY = 2
FAST_DATABASE_JOBS = frozenset({"process_options_paper_orders", "sync_decision_inbox"})
_scheduler_semaphore: asyncio.Semaphore | None = None
_active_jobs: dict[str, float] = {}
_deferred_jobs = 0
# Both recovery inputs are point-in-time tapes.  Keep their dispatches on their
# logical slots instead of letting ordinary completion-time recurrence drift a
# five-minute detector into the next observation bucket.
SLOT_ALIGNED_JOBS = frozenset({"robinhood_option_history", "detect_option_events"})
SLOT_ALIGNMENT_TOLERANCE_SECONDS = 30.0
CONTINUOUS_ADVISOR_JOBS = frozenset(
    {"run_continuous_advisor", "run_continuous_advisor_replay", "run_continuous_advisor_evolution"}
)

__all__ = [
    "STAGGER_SECONDS",
    "job_intervals",
    "run_scheduler",
    "scheduler_enabled",
    "scheduler_status",
    "scheduler_runtime_health",
]


def scheduler_runtime_health() -> dict[str, Any]:
    now = time.monotonic()
    oldest = min((now - started for started in _active_jobs.values()), default=0.0)
    return {
        "active_count": len(_active_jobs),
        "capacity": SCHEDULER_CAPACITY,
        "job_names": sorted(_active_jobs),
        "oldest_runtime_seconds": round(oldest, 3),
        "deferred_job_count": max(0, _deferred_jobs),
    }


def scheduler_status(config: AppConfig | None = None) -> dict[str, Any]:
    status = _configured_scheduler_status(config)
    status["runtime"] = scheduler_runtime_health()
    status["capacity"] = SCHEDULER_CAPACITY
    return status


def _initial_delay_seconds(
    job: str,
    interval: int,
    offset: int,
    *,
    reference_time: datetime | None = None,
) -> float:
    if job == "run_continuous_advisor":
        reference = (reference_time or datetime.now(MARKET_TZ)).astimezone(UTC)
        return max(0.0, (_next_market_open_at(reference) - reference).total_seconds())
    if job in SLOT_ALIGNED_JOBS:
        reference = (reference_time or datetime.now(MARKET_TZ)).astimezone(MARKET_TZ)
        elapsed = reference.timestamp()
        remainder = elapsed % interval
        return 0.0 if remainder == 0 else float(interval - remainder)
    return initial_delay_seconds(job, interval, offset, stagger_seconds=STAGGER_SECONDS)


def _startup_delay_seconds(
    job: str,
    interval: int,
    offset: int,
    *,
    overdue_jobs: set[str] | frozenset[str] = frozenset(),
    reference_time: datetime | None = None,
) -> float:
    """Stagger overdue source catch-up while preserving normal first-run waits."""

    if job in overdue_jobs:
        return float(offset * STAGGER_SECONDS)
    return _initial_delay_seconds(job, interval, offset, reference_time=reference_time)


def _is_slot_boundary(job: str, interval: int, reference_time: datetime | None = None) -> bool:
    if job not in SLOT_ALIGNED_JOBS:
        return True
    reference = (reference_time or datetime.now(MARKET_TZ)).astimezone(MARKET_TZ)
    elapsed = reference.timestamp()
    return elapsed % interval < SLOT_ALIGNMENT_TOLERANCE_SECONDS


def _recurring_delay_seconds(
    job: str,
    interval: int,
    *,
    reference_time: datetime | None = None,
) -> float:
    """Return the delay after a completed (or skipped) run.

    Initial staggering exists only to spread process startup.  Reusing it for a
    recurrence turns staggered jobs into a tight loop, so completion always
    waits a full configured interval.  History collection is intentionally
    calendar-aligned; its next recurrence is the *next* quarter-hour slot.
    """
    if job == "run_continuous_advisor":
        reference = (reference_time or datetime.now(MARKET_TZ)).astimezone(UTC)
        target = reference + timedelta(seconds=interval)
        return max(0.0, (_next_market_open_at(target) - reference).total_seconds())
    if job not in SLOT_ALIGNED_JOBS:
        return float(interval)
    reference = (reference_time or datetime.now(MARKET_TZ)).astimezone(MARKET_TZ)
    elapsed = reference.timestamp()
    remainder = elapsed % interval
    # A completed run five seconds into a slot must wait to the *next* slot;
    # the scheduler's broader boundary tolerance is only for dispatch jitter.
    return float(interval if remainder < 0.001 else interval - remainder)


def _next_market_open_at(reference: datetime) -> datetime:
    """Return the next regular US session open, including the current open session."""

    reference = reference.astimezone(UTC)
    local = reference.astimezone(MARKET_TZ)
    if is_market_open(reference):
        return reference
    day = local.date()
    for _ in range(8):
        if is_us_market_day(day):
            open_at, _close_at = market_session_bounds(day)
            candidate = open_at.astimezone(UTC)
            if candidate >= reference:
                return candidate
        day += timedelta(days=1)
    return reference + timedelta(days=8)


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


def _refresh_continuous_advisor_intervals(
    config_path: str,
    intervals: dict[str, int],
    next_due: dict[str, float],
    next_due_wall: dict[str, datetime],
    in_flight: dict[str, asyncio.Task],
    *,
    now: float,
    wall_now: datetime,
) -> None:
    """Apply live advisor enable/cadence changes without restarting the app."""

    try:
        configured = job_intervals(load_config(config_path))
    except Exception:
        logger.exception("could not refresh live continuous-advisor settings")
        return

    current = {job: configured[job] for job in CONTINUOUS_ADVISOR_JOBS if job in configured}
    previous = {job: intervals.get(job) for job in CONTINUOUS_ADVISOR_JOBS if job in intervals}
    if current == previous:
        return

    for job in CONTINUOUS_ADVISOR_JOBS:
        interval = current.get(job)
        if interval is None:
            intervals.pop(job, None)
            if job not in in_flight:
                next_due.pop(job, None)
                next_due_wall.pop(job, None)
            continue
        intervals[job] = interval
        if job not in in_flight:
            delay = _initial_delay_seconds(job, interval, 0, reference_time=wall_now)
            next_due[job] = now + delay
            next_due_wall[job] = wall_now + timedelta(seconds=delay)


async def run_scheduler(db_path: str, config_path: str = "config.yaml") -> None:
    intervals = job_intervals(load_config(config_path))
    warmup = _env_int("MARKET_SCHEDULER_WARMUP_SECONDS", 20, allow_zero=True)
    logger.info("market scheduler starting (warmup=%ss, intervals=%s)", warmup, intervals)

    # Reconcile stale single-flight records before scheduling.  This also
    # releases jobs stranded by a prior process exit without touching a healthy
    # heartbeat.
    try:
        await asyncio.to_thread(mark_stale_running_jobs, db_path)
    except Exception:
        logger.exception("could not reconcile stale refresh jobs")
    try:
        overdue_jobs = await asyncio.to_thread(overdue_source_refresh_jobs, db_path)
    except Exception:
        overdue_jobs = set()
        logger.exception("could not determine overdue source refresh jobs")
    start = time.monotonic() + warmup
    start_wall_time = datetime.now(MARKET_TZ) + timedelta(seconds=warmup)
    next_due: dict[str, float] = {
        job: start + _startup_delay_seconds(
            job,
            interval,
            offset,
            overdue_jobs=overdue_jobs,
            reference_time=start_wall_time,
        )
        for offset, (job, interval) in enumerate(intervals.items())
    }
    next_due_wall: dict[str, datetime] = {
        job: start_wall_time + timedelta(
            seconds=_startup_delay_seconds(
                job,
                interval,
                offset,
                overdue_jobs=overdue_jobs,
                reference_time=start_wall_time,
            )
        )
        for offset, (job, interval) in enumerate(intervals.items())
    }
    next_continuous_settings_refresh = time.monotonic() + CONTINUOUS_SETTINGS_REFRESH_SECONDS
    in_flight: dict[str, asyncio.Task] = {}
    global _scheduler_semaphore
    global _deferred_jobs
    previous_semaphore = _scheduler_semaphore
    _scheduler_semaphore = asyncio.Semaphore(SCHEDULER_CAPACITY)
    _deferred_jobs = 0

    try:
        while True:
            now = time.monotonic()
            if now >= next_continuous_settings_refresh:
                await asyncio.to_thread(
                    _refresh_continuous_advisor_intervals,
                    config_path,
                    intervals,
                    next_due,
                    next_due_wall,
                    in_flight,
                    now=now,
                    wall_now=datetime.now(MARKET_TZ),
                )
                next_continuous_settings_refresh = time.monotonic() + CONTINUOUS_SETTINGS_REFRESH_SECONDS
                now = time.monotonic()
            for job, task in list(in_flight.items()):
                if task.done():
                    in_flight.pop(job, None)
                    interval = intervals.get(job)
                    if interval is None:
                        next_due.pop(job, None)
                        next_due_wall.pop(job, None)
                        continue
                    delay = _recurring_delay_seconds(job, interval)
                    next_due[job] = now + delay
                    next_due_wall[job] = datetime.now(MARKET_TZ) + timedelta(seconds=delay)
            for job, interval in intervals.items():
                if now >= next_due.get(job, 0.0) and job not in in_flight:
                    if not _is_slot_boundary(job, interval):
                        delay = _initial_delay_seconds(job, interval, 0)
                        next_due[job] = time.monotonic() + delay
                        next_due_wall[job] = datetime.now(MARKET_TZ) + timedelta(seconds=delay)
                        continue
                    in_flight[job] = asyncio.create_task(
                        _dispatch(
                            job,
                            db_path,
                            config_path,
                            due_at=next_due_wall.get(job),
                        )
                    )
            slot_due = [next_due[job] for job in SLOT_ALIGNED_JOBS.intersection(intervals) if job not in in_flight]
            sleep_seconds = min(TICK_SECONDS, max(0.05, min(slot_due) - time.monotonic())) if slot_due else TICK_SECONDS
            await asyncio.sleep(sleep_seconds)
    except asyncio.CancelledError:
        logger.info("market scheduler stopping")
        for task in in_flight.values():
            task.cancel()
        if in_flight:
            await asyncio.gather(*in_flight.values(), return_exceptions=True)
        _scheduler_semaphore = previous_semaphore
        _deferred_jobs = 0
        raise


async def _dispatch(
    job: str,
    db_path: str,
    config_path: str,
    *,
    due_at: datetime | None = None,
) -> None:
    semaphore = _scheduler_semaphore
    if semaphore is None:
        await _dispatch_once(job, db_path, config_path, due_at=due_at)
        return
    global _deferred_jobs
    was_busy = semaphore.locked()
    if was_busy:
        _deferred_jobs += 1
    acquired = False
    try:
        await semaphore.acquire()
        acquired = True
    finally:
        if was_busy and not acquired:
            _deferred_jobs = max(0, _deferred_jobs - 1)
    if was_busy:
        _deferred_jobs = max(0, _deferred_jobs - 1)
    _active_jobs[job] = time.monotonic()
    try:
        await _dispatch_once(job, db_path, config_path, due_at=due_at)
    finally:
        _active_jobs.pop(job, None)
        semaphore.release()


async def _dispatch_once(
    job: str,
    db_path: str,
    config_path: str,
    *,
    due_at: datetime | None = None,
) -> None:
    started_job_id: str | None = None
    try:
        start_kwargs: dict[str, Any] = {}
        if due_at is not None:
            start_kwargs = {
                "scheduled_due_at": due_at.astimezone(UTC),
                "dispatched_at": datetime.now(UTC),
            }
        started: Any = await asyncio.to_thread(start_refresh_job, job, db_path, **start_kwargs)
        if isinstance(started, dict) and started.get("created"):
            started_job_id = str(started["id"])
            if job in FAST_DATABASE_JOBS:
                result = await asyncio.to_thread(
                    execute_refresh_job,
                    started_job_id,
                    job,
                    db_path,
                    config_path,
                    raise_on_error=False,
                )
            else:
                if job == "run_continuous_advisor" and due_at is not None:
                    result = await _execute_started_refresh_job(
                        job, started_job_id, db_path, config_path, due_at=due_at
                    )
                else:
                    result = await _execute_started_refresh_job(job, started_job_id, db_path, config_path)
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


async def _execute_started_refresh_job(
    job: str,
    job_id: str,
    db_path: str,
    config_path: str,
    *,
    due_at: datetime | None = None,
) -> dict[str, Any]:
    spec = RefreshProcessSpec(
        job_id=job_id,
        job_name=job,
        database_url=str(db_path),
        config_path=config_path,
        scheduled_due_at=due_at.astimezone(UTC).isoformat() if due_at is not None else None,
    )

    async def fail(error: str) -> dict[str, Any]:
        return await asyncio.to_thread(finish_refresh_job_failed, job_id, job, db_path, error)

    return await execute_async(spec, fail)


async def _terminate_refresh_subprocess(proc: asyncio.subprocess.Process) -> None:
    await terminate_process(proc)
