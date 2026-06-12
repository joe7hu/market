"""Retention helpers for operational tables."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from investment_panel.core.db import db, init_db


def prune_operational_tables(
    db_path: str | Path,
    *,
    now: datetime | None = None,
    provider_run_days: int = 30,
    source_run_days: int = 30,
    refresh_job_days: int = 14,
    keep_recent: int = 200,
) -> dict[str, int]:
    init_db(db_path)
    checked_at = now or datetime.now(UTC)
    with db(db_path, read_only=False) as con:
        return prune_operational_tables_for_connection(
            con,
            now=checked_at,
            provider_run_days=provider_run_days,
            source_run_days=source_run_days,
            refresh_job_days=refresh_job_days,
            keep_recent=keep_recent,
        )


def prune_operational_tables_for_connection(
    con: Any,
    *,
    now: datetime | None = None,
    provider_run_days: int = 30,
    source_run_days: int = 30,
    refresh_job_days: int = 14,
    keep_recent: int = 200,
) -> dict[str, int]:
    checked_at = now or datetime.now(UTC)
    specs = [
        ("provider_runs", "id", "COALESCE(finished_at, started_at)", checked_at - timedelta(days=provider_run_days)),
        ("refresh_jobs", "id", "COALESCE(finished_at, started_at)", checked_at - timedelta(days=refresh_job_days)),
    ]
    counts: dict[str, int] = {}
    for table, key_column, time_expr, cutoff in specs:
        counts[table] = _delete_stale_single_key(con, table, key_column, time_expr, cutoff, keep_recent)
    counts["source_runs"] = _delete_stale_source_runs(con, checked_at - timedelta(days=source_run_days), keep_recent)
    return counts


def _delete_stale_single_key(con: Any, table: str, key_column: str, time_expr: str, cutoff: datetime, keep_recent: int) -> int:
    result = con.execute(
        f"""
        DELETE FROM {table}
        WHERE {key_column} IN (
            SELECT {key_column}
            FROM (
                SELECT {key_column},
                       row_number() OVER (ORDER BY {time_expr} DESC NULLS LAST, {key_column}) AS row_rank,
                       {time_expr} AS observed_at
                FROM {table}
            )
            WHERE row_rank > ?
              AND observed_at < ?
        )
        """,
        [keep_recent, cutoff],
    )
    return int(result.fetchone()[0] if result.description else 0)


def _delete_stale_source_runs(con: Any, cutoff: datetime, keep_recent: int) -> int:
    result = con.execute(
        """
        DELETE FROM source_runs
        WHERE (source_id, run_id) IN (
            SELECT source_id, run_id
            FROM (
                SELECT source_id, run_id,
                       row_number() OVER (ORDER BY COALESCE(finished_at, started_at) DESC NULLS LAST, source_id, run_id) AS row_rank,
                       COALESCE(finished_at, started_at) AS observed_at
                FROM source_runs
            )
            WHERE row_rank > ?
              AND observed_at < ?
        )
        """,
        [keep_recent, cutoff],
    )
    return int(result.fetchone()[0] if result.description else 0)
