"""Immutable capture writing for PostgreSQL option-history profiles."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import JOB_PROFILE


HISTORY_PROFILE = "history_full"


def store_capture(
    repository: Any,
    *,
    run_id: UUID,
    source_id: str,
    symbol: str,
    slot_at: datetime,
    captured: dict[str, Any],
    minimum_completeness: float = 0.98,
    collection_profile: str = HISTORY_PROFILE,
    universe: str | None = None,
    materialize: bool = True,
) -> dict[str, Any]:
    """Store one profile-scoped capture without leaking it into full history."""

    rows = list(captured.get("rows") or [])
    expected = int(captured.get("expected_contract_count") or 0)
    received = int(captured.get("received_contract_count") or len(rows))
    completeness = (received / expected) if expected else 0.0
    errors = [str(error) for error in captured.get("errors") or []]
    complete = expected > 0 and completeness >= minimum_completeness and not errors and not captured.get("timed_out")
    state = "complete" if complete else "partial"
    quote_diagnostics = _jsonable(dict(captured.get("quote_diagnostics") or {}))
    if captured.get("event_strip_diagnostics") is not None:
        quote_diagnostics["event_strip"] = _jsonable(dict(captured["event_strip_diagnostics"]))
    snapshot_universe = universe or _universe_for_profile(collection_profile, symbol)
    generation = repository._generation_for_run(
        source_id=source_id,
        symbol=symbol,
        slot_at=slot_at,
        run_id=run_id,
        collection_profile=collection_profile,
        universe=snapshot_universe,
    )
    if generation is None:
        raise ValueError("capture generation was not claimed")
    started_at = _as_utc(captured.get("capture_started_at")) or slot_at
    finished_at = _as_utc(captured.get("capture_finished_at")) or datetime.now(UTC)
    for row in rows:
        option_type = str(row.get("option_type") or row.get("type") or "").lower()
        expiration = str(row.get("expiration") or row.get("expiry") or "")[:10]
        row.setdefault("capture_group_key", f"{expiration}:{option_type}")
        row.setdefault("group_started_at", started_at)
        row.setdefault("group_finished_at", finished_at)
        row.setdefault("available_at", finished_at)
        row.setdefault("provider_observed_at", row.get("provider_updated_at") or finished_at)
        row.setdefault("underlying_observed_at", finished_at)
        row.setdefault("underlying_available_at", finished_at)
    snapshot = repository.ingestion.store_option_snapshot(
        run_id,
        source_id=source_id,
        observed_at=slot_at,
        market_session="regular",
        universe=snapshot_universe,
        rows=rows,
        completeness=completeness,
        collection_profile=collection_profile,
        history_symbol=symbol,
        slot_at=slot_at,
        capture_started_at=started_at,
        capture_finished_at=finished_at,
        expected_contract_count=expected,
        received_contract_count=received,
        capture_state=state,
        capture_generation_id=generation,
        quote_observed_at=finished_at + timedelta(microseconds=generation),
    )
    with repository.runtime.transaction(JOB_PROFILE) as connection:
        connection.execute(
            """
            UPDATE raw.option_capture_generation
            SET capture_state = %s, expected_contract_count = %s, received_contract_count = %s,
                completeness = %s, capture_started_at = %s, capture_finished_at = %s,
                terminal_error = %s, diagnostics = diagnostics || %s
            WHERE id = %s AND capture_state = 'running'
            """,
            [
                state,
                expected,
                received,
                completeness,
                started_at,
                finished_at,
                "; ".join(errors) or None,
                Jsonb({"quote_diagnostics": quote_diagnostics}),
                generation,
            ],
        )
        connection.execute(
            """
            UPDATE raw.option_snapshot
            SET capture_state = %s, capture_finished_at = %s, completeness = %s,
                expected_contract_count = %s, received_contract_count = %s, contract_count = %s,
                latest_complete_generation_id = CASE WHEN %s = 'complete' THEN %s ELSE latest_complete_generation_id END
            WHERE id = %s
            """,
            [
                state,
                finished_at,
                completeness,
                expected,
                received,
                len(rows),
                state,
                generation,
                snapshot["snapshot_id"],
            ],
        )
    result = {
        **snapshot,
        "symbol": symbol,
        "slot_at": slot_at.isoformat(),
        "expected_contract_count": expected,
        "received_contract_count": received,
        "completeness": completeness,
        "capture_state": state,
        "capture_generation_id": generation,
        "capture_started_at": started_at.isoformat(),
        "capture_finished_at": finished_at.isoformat(),
        "collection_profile": collection_profile,
        "universe": snapshot_universe,
        "errors": errors,
        "quote_diagnostics": quote_diagnostics,
    }
    if complete and materialize:
        result.update(repository.materialize_snapshot(int(snapshot["snapshot_id"]), mode="live_lifecycle"))
    return result


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _universe_for_profile(collection_profile: str, symbol: str) -> str:
    return f"history_full:{symbol.upper()}" if collection_profile == HISTORY_PROFILE else f"{collection_profile}:{symbol.upper()}"
