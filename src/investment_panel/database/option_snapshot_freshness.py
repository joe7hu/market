"""Bounded option-snapshot freshness reads for incremental collectors."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from investment_panel.database.runtime import DatabaseRuntime


def latest_option_snapshot_by_symbol(
    runtime: DatabaseRuntime,
    source_id: str,
    symbols: Sequence[str],
) -> dict[str, datetime]:
    normalized = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
    if not normalized:
        return {}
    with runtime.read() as connection:
        rows = connection.execute(
            """
            SELECT requested.symbol, max(snapshot.observed_at) AS observed_at
            FROM raw.option_snapshot snapshot
            JOIN ingest.run ingest_run ON ingest_run.id = snapshot.ingest_run_id
            CROSS JOIN LATERAL jsonb_array_elements_text(
                COALESCE(ingest_run.summary->'symbols_requested', '[]'::jsonb)
            ) requested(symbol)
            WHERE snapshot.source_id = %s
              AND requested.symbol = ANY(%s)
            GROUP BY requested.symbol
            """,
            [source_id, normalized],
        ).fetchall()
    return {str(row["symbol"]): row["observed_at"] for row in rows}
