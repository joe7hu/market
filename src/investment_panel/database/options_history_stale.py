"""Stale option-history capture reconciliation."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


def defer_stale_running_captures(
    runtime: DatabaseRuntime,
    *,
    source_id: str,
    collection_profile: str,
    stale_after: timedelta,
    reason: str = "collector_orphaned_after_shutdown",
    now: datetime | None = None,
) -> int:
    """Close orphaned running slots after their provider lease window has passed."""

    as_of = now or datetime.now(UTC)
    cutoff = as_of - stale_after
    with runtime.transaction(JOB_PROFILE) as connection:
        rows = connection.execute(
            """
            SELECT generation.id AS generation_id, generation.ingest_run_id,
                   snapshot.id AS snapshot_id, snapshot.history_symbol
            FROM raw.option_capture_generation generation
            JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
            WHERE snapshot.source_id = %s
              AND snapshot.collection_profile = %s
              AND generation.capture_state = 'running'
              AND snapshot.capture_state = 'running'
              AND generation.capture_started_at <= %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM ops.provider_lease lease
                  WHERE lease.provider = snapshot.source_id
                    AND lease.workload = 'option_history'
                    AND lease.symbol = snapshot.history_symbol
                    AND lease.expires_at > %s
              )
            FOR UPDATE OF generation, snapshot
            """,
            [source_id, collection_profile, cutoff, as_of],
        ).fetchall()
        if not rows:
            return 0
        generation_ids = [row["generation_id"] for row in rows]
        snapshot_ids = [row["snapshot_id"] for row in rows]
        run_ids = [row["ingest_run_id"] for row in rows]
        connection.execute(
            """
            UPDATE raw.option_capture_generation
            SET capture_state = 'deferred', capture_finished_at = %s, terminal_error = %s
            WHERE id = ANY(%s)
            """,
            [as_of, reason, generation_ids],
        )
        connection.execute(
            """
            UPDATE raw.option_snapshot
            SET capture_state = 'deferred', capture_finished_at = %s
            WHERE id = ANY(%s)
            """,
            [as_of, snapshot_ids],
        )
        connection.execute(
            """
            UPDATE ingest.run
            SET status = 'failed', finished_at = %s, failure_detail = %s,
                summary = summary || %s
            WHERE id = ANY(%s) AND status = 'running'
            """,
            [as_of, reason, Jsonb({"reason": reason}), run_ids],
        )
    return len(rows)
