"""Application job-control boundary.

The canonical refresh repository and allowlist remain in
``investment_panel.core.refresh_jobs``. This module owns only the HTTP
background-task adapter and cache invalidation after a job completes.
"""

from __future__ import annotations

from typing import Any

from app.panel_snapshot import invalidate_context_cache
from investment_panel.core.refresh_jobs import (
    ALLOWLIST,
    execute_refresh_job,
    execute_refresh_job_subprocess,
    refresh_job_rows,
    run_refresh_job,
    start_refresh_job,
)
from investment_panel.jobs import run_thesis_monitor


def execute_background_refresh_job(job_id: str, job_name: str, database_url: str) -> None:
    try:
        execute_refresh_job_subprocess(job_id, job_name, database_url, "config.yaml")
    finally:
        invalidate_context_cache()


def execute_thesis_monitor_automation(symbols: list[str], *, dry_run: bool, force: bool) -> None:
    try:
        run_thesis_monitor.run("config.yaml", symbols=symbols, trigger="ondemand", force=force, dry_run=dry_run)
    finally:
        invalidate_context_cache()


__all__ = [
    "ALLOWLIST",
    "execute_background_refresh_job",
    "execute_refresh_job",
    "execute_refresh_job_subprocess",
    "execute_thesis_monitor_automation",
    "refresh_job_rows",
    "run_refresh_job",
    "start_refresh_job",
]
