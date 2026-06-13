"""Provider-run and source-health recording."""

from __future__ import annotations
from datetime import date, datetime
from typing import Any
from investment_panel.core.db import json_dumps, query_rows



def record_provider_run(
    con: Any,
    run_id: str,
    provider: str,
    capability: str,
    started_at: str,
    status: str,
    detail: str,
    raw: Any,
) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO provider_runs
        (id, provider, capability, started_at, finished_at, status, detail, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [run_id, provider, capability, started_at, datetime.utcnow().isoformat(), status, detail, json_dumps(raw)],
    )




def record_source_health(con: Any, source: str, status: str, detail: str, source_url: str) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO source_health
        (source, checked_at, status, detail, source_url)
        VALUES (?, ?, ?, ?, ?)
        """,
        [source, datetime.utcnow().isoformat(), status, detail, source_url],
    )
