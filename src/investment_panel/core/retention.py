"""PostgreSQL retention facade used by refresh workflows."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from investment_panel.database.authority import database_url, runtime_for_url
from investment_panel.database.retention import RetentionRepository


def prune_operational_tables(
    database: Any,
    *,
    now: datetime | None = None,
    option_days: int = 120,
    analysis_days: int = 365,
    publication_days: int = 90,
    refresh_job_days: int = 30,
    **_legacy_arguments: Any,
) -> dict[str, int]:
    dsn = database if isinstance(database, str) and database.startswith("postgresql") else database_url(database)
    return RetentionRepository(runtime_for_url(dsn)).prune(
        now=now,
        option_days=option_days,
        analysis_days=analysis_days,
        publication_days=publication_days,
        job_days=refresh_job_days,
    )
