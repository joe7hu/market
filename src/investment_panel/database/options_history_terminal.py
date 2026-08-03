"""Terminal option-history capture state transitions."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


def fail_capture(
    runtime: DatabaseRuntime,
    *,
    source_id: str,
    symbol: str,
    slot_at: datetime,
    run_id: UUID,
    error: Exception | str,
    collection_profile: str = "history_full",
    universe: str | None = None,
) -> None:
    detail = str(error)
    with runtime.transaction(JOB_PROFILE) as connection:
        generation = _running_generation(
            connection,
            source_id=source_id,
            symbol=symbol,
            slot_at=slot_at,
            run_id=run_id,
            collection_profile=collection_profile,
            universe=universe,
        )
        if generation is None:
            return
        connection.execute(
            "UPDATE raw.option_capture_generation SET capture_state = 'failed', capture_finished_at = now(), terminal_error = %s WHERE id = %s",
            [detail, generation["id"]],
        )
        connection.execute(
            "UPDATE raw.option_snapshot SET capture_state = 'failed', capture_finished_at = now() WHERE id = %s",
            [generation["snapshot_id"]],
        )


def defer_capture(
    runtime: DatabaseRuntime,
    *,
    source_id: str,
    symbol: str,
    slot_at: datetime,
    run_id: UUID,
    reason: str,
    collection_profile: str = "history_full",
    universe: str | None = None,
) -> None:
    with runtime.transaction(JOB_PROFILE) as connection:
        generation = _running_generation(
            connection,
            source_id=source_id,
            symbol=symbol,
            slot_at=slot_at,
            run_id=run_id,
            collection_profile=collection_profile,
            universe=universe,
        )
        if generation is None:
            return
        connection.execute(
            "UPDATE raw.option_capture_generation SET capture_state = 'deferred', capture_finished_at = now(), terminal_error = %s WHERE id = %s",
            [reason, generation["id"]],
        )
        connection.execute(
            "UPDATE raw.option_snapshot SET capture_state = 'deferred', capture_finished_at = now() WHERE id = %s",
            [generation["snapshot_id"]],
        )


def _running_generation(
    connection: Any,
    *,
    source_id: str,
    symbol: str,
    slot_at: datetime,
    run_id: UUID,
    collection_profile: str,
    universe: str | None,
) -> Any | None:
    return connection.execute(
        """
        SELECT generation.id, snapshot.id AS snapshot_id
        FROM raw.option_capture_generation generation
        JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
        WHERE snapshot.source_id = %s AND snapshot.history_symbol = %s AND snapshot.slot_at = %s
          AND snapshot.collection_profile = %s
          AND snapshot.universe = %s
          AND generation.ingest_run_id = %s AND generation.capture_state = 'running'
        FOR UPDATE
        """,
        [
            source_id,
            symbol.upper(),
            slot_at,
            collection_profile,
            universe or f"{collection_profile}:{symbol.upper()}",
            run_id,
        ],
    ).fetchone()
