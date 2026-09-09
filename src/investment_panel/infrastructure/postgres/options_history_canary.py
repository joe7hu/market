"""Post-fix QQQ option-history canary accounting.

The canary intentionally counts captured regular-session *dates*, never rows or
intraday snapshots.  Replay is useful correctness evidence but cannot advance
the live canary because the capture must finish after its revision's start.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from typing import Any


SCHEDULED_REGULAR_SLOTS = 27  # 09:30 through 16:00 ET, inclusive, every 15m.
STANDARD_SCHEDULED_REGULAR_SLOTS = 8  # 09:30-15:30 hourly plus official close.
REQUIRED_SLOT_COVERAGE = 0.95
REQUIRED_CONTRACT_COVERAGE = 0.98


def canary_health(connection: Any, *, symbol: str, model_revision: str) -> dict[str, Any]:
    canary = connection.execute(
        """
        SELECT model_revision, started_at
        FROM analysis.option_history_canary
        WHERE model_revision = %s
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        [model_revision],
    ).fetchone()
    if canary is None:
        return _empty(model_revision)
    started_at = canary["started_at"]
    scheduled_slots = _scheduled_regular_slots(connection, symbol=symbol)
    complete_captures = connection.execute(
        """
        SELECT count(*) AS count
        FROM raw.option_capture_generation generation
        JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
        WHERE snapshot.history_symbol = %s AND snapshot.market_session = 'regular'
          AND snapshot.collection_profile = 'history_full'
          AND generation.capture_state = 'complete'
        """,
        [symbol.upper()],
    ).fetchone()["count"]
    rows = connection.execute(
        """
        SELECT snapshot.id AS snapshot_id, snapshot.trading_date, snapshot.slot_at,
               generation.capture_started_at, generation.capture_finished_at,
               generation.completeness,
               NOT EXISTS (
                   SELECT 1 FROM raw.option_capture_generation other
                   WHERE other.snapshot_id = snapshot.id AND other.capture_state = 'running'
               ) AS terminal_generations,
               EXISTS (
                   SELECT 1 FROM analysis.run run
                   WHERE run.run_type = 'option_history_v3' AND run.status = 'succeeded'
                     AND (run.summary->>'capture_generation_id')::bigint = generation.id
                     AND run.summary->>'model_revision' = %s
               ) AS v3_succeeded
        FROM raw.option_snapshot snapshot
        JOIN raw.option_capture_generation generation
          ON generation.id = snapshot.latest_complete_generation_id
        WHERE snapshot.history_symbol = %s AND snapshot.market_session = 'regular'
          AND snapshot.collection_profile = 'history_full'
          AND generation.capture_state = 'complete'
        ORDER BY snapshot.trading_date, snapshot.slot_at, generation.id
        """,
        [model_revision, symbol.upper()],
    ).fetchall()
    by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        row = dict(raw)
        by_date[row["trading_date"]].append(row)
    sessions: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    required_slots = int(scheduled_slots * REQUIRED_SLOT_COVERAGE + 0.999999)
    for trading_date, session_rows in sorted(by_date.items()):
        slots = {row["slot_at"] for row in session_rows if row.get("slot_at") is not None}
        reasons: list[str] = []
        if len(slots) < required_slots:
            reasons.append("scheduled_slot_coverage_below_95pct")
        if any((row.get("capture_finished_at") or row.get("slot_at")) < started_at for row in session_rows):
            reasons.append("pre_canary_capture")
        if any(float(row["completeness"] or 0.0) < REQUIRED_CONTRACT_COVERAGE for row in session_rows):
            reasons.append("contract_coverage_below_98pct")
        if not all(bool(row["terminal_generations"]) for row in session_rows):
            reasons.append("nonterminal_generation")
        if not all(bool(row["v3_succeeded"]) for row in session_rows):
            reasons.append("v3_analysis_not_succeeded")
        previous_finished = None
        for row in sorted(session_rows, key=lambda item: item.get("capture_started_at") or item.get("slot_at")):
            started, finished = row.get("capture_started_at"), row.get("capture_finished_at")
            if previous_finished is not None and started is not None and previous_finished > started:
                reasons.append("overlapping_capture_groups")
                break
            if finished is not None and (previous_finished is None or finished > previous_finished):
                previous_finished = finished
        unique_reasons = sorted(set(reasons))
        reason_counts.update(unique_reasons)
        sessions.append({
            "trading_date": trading_date,
            "observed_slots": len(slots),
            "scheduled_slots": scheduled_slots,
            "scheduled_slot_coverage": len(slots) / scheduled_slots,
            "minimum_contract_coverage": min((float(row["completeness"] or 0.0) for row in session_rows), default=0.0),
            "qualified": not unique_reasons,
            "disqualification_reasons": unique_reasons,
        })
    qualified = sum(1 for session in sessions if session["qualified"])
    return {
        "complete_captures": int(complete_captures),
        "post_fix_complete_captures": sum(
            1 for row in rows if (row.get("capture_finished_at") or row.get("slot_at")) >= started_at
        ),
        "observed_regular_session_dates": len(sessions),
        "qualified_regular_sessions": qualified,
        "canary_revision": str(canary["model_revision"]),
        "canary_started_at": started_at,
        "required_regular_sessions": 5,
        "sessions": sessions,
        "disqualification_reasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


def _scheduled_regular_slots(connection: Any, *, symbol: str) -> int:
    row = connection.execute(
        """
        SELECT policy.cadence_minutes
        FROM app.option_history_policy policy
        JOIN catalog.instrument instrument ON instrument.id = policy.instrument_id
        WHERE instrument.symbol = %s AND policy.profile = 'history_full'
        """,
        [symbol.upper()],
    ).fetchone()
    cadence = int(row["cadence_minutes"]) if row else 15
    return STANDARD_SCHEDULED_REGULAR_SLOTS if cadence == 60 else SCHEDULED_REGULAR_SLOTS


def _empty(model_revision: str) -> dict[str, Any]:
    return {
        "complete_captures": 0,
        "post_fix_complete_captures": 0,
        "observed_regular_session_dates": 0,
        "qualified_regular_sessions": 0,
        "canary_revision": model_revision,
        "canary_started_at": None,
        "required_regular_sessions": 5,
        "sessions": [],
        "disqualification_reasons": [{"reason": "canary_not_initialized", "count": 1}],
    }
