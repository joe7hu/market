"""Bounded, explanation-only constant-tenor option-surface shifts."""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE

FEATURE_VERSION = "constant-tenor-surface-shift-v1"
TENORS = (7, 14, 30, 60, 90)


def constant_tenor_curve(
    points: Iterable[dict[str, Any]], *, tenors: tuple[int, ...] = TENORS,
    positive_only: bool = True,
) -> dict[int, float | None]:
    """Interpolate within observed DTE bounds; never extrapolate."""
    usable: dict[int, list[float]] = {}
    for point in points:
        try:
            dte = int(point["dte"])
            value = float(point["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if dte < 0 or not isfinite(value) or (positive_only and value <= 0):
            continue
        usable.setdefault(dte, []).append(value)
    curve = {dte: sum(values) / len(values) for dte, values in usable.items()}
    observed = sorted(curve)
    output: dict[int, float | None] = {}
    for tenor in tenors:
        if tenor in curve:
            output[tenor] = curve[tenor]
            continue
        lower = max((dte for dte in observed if dte < tenor), default=None)
        upper = min((dte for dte in observed if dte > tenor), default=None)
        if lower is None or upper is None:
            output[tenor] = None
            continue
        weight = (tenor - lower) / (upper - lower)
        output[tenor] = curve[lower] + weight * (curve[upper] - curve[lower])
    return output


def wasserstein_1_equal_mass(current: Iterable[float], previous: Iterable[float]) -> float | None:
    """W1 for two finite, equal-size empirical samples with equal mass."""
    left = sorted(float(value) for value in current if _finite_positive(value))
    right = sorted(float(value) for value in previous if _finite_positive(value))
    if not left or len(left) != len(right):
        return None
    return sum(abs(a - b) for a, b in zip(left, right, strict=True)) / len(left)


def surface_shift_payload(
    current: Iterable[dict[str, Any]],
    previous: Iterable[dict[str, Any]],
    *,
    tenors: tuple[int, ...] = TENORS,
) -> dict[str, Any]:
    """Build a conservative constant-tenor research feature."""
    current_rows = list(current)
    previous_rows = list(previous)
    current_curve = constant_tenor_curve(
        ({"dte": row.get("dte"), "value": row.get("atm_iv")} for row in current_rows),
        tenors=tenors,
    )
    previous_curve = constant_tenor_curve(
        ({"dte": row.get("dte"), "value": row.get("atm_iv")} for row in previous_rows),
        tenors=tenors,
    )
    complete = all(current_curve[tenor] is not None and previous_curve[tenor] is not None for tenor in tenors)
    blockers: list[str] = []
    if not complete:
        blockers.append("constant_tenor_brackets_incomplete")
    w1 = wasserstein_1_equal_mass(
        (current_curve[tenor] for tenor in tenors),
        (previous_curve[tenor] for tenor in tenors),
    ) if complete else None
    current_skew_by_side = _curves_by_option_type(
        current_rows, field="delta_25_iv", tenors=tenors,
    )
    previous_skew_by_side = _curves_by_option_type(
        previous_rows, field="delta_25_iv", tenors=tenors,
    )
    current_skew = _put_call_risk_reversal(current_skew_by_side, tenors)
    previous_skew = _put_call_risk_reversal(previous_skew_by_side, tenors)
    current_term = constant_tenor_curve(
        ({"dte": row.get("dte"), "value": row.get("term_slope")} for row in current_rows),
        tenors=tenors, positive_only=False,
    )
    previous_term = constant_tenor_curve(
        ({"dte": row.get("dte"), "value": row.get("term_slope")} for row in previous_rows),
        tenors=tenors, positive_only=False,
    )
    skew_shift = _matched_curve_change(current_skew, previous_skew, tenors)
    term_shift = _matched_curve_change(current_term, previous_term, tenors)
    blockers.append("risk_neutral_tail_density_not_materialized")
    return {
        "evidence_state": "ready" if complete else "insufficient_surface_evidence",
        "tenors": list(tenors),
        "current_curve": {str(key): value for key, value in current_curve.items()},
        "previous_curve": {str(key): value for key, value in previous_curve.items()},
        "current_skew_curve": {str(key): value for key, value in current_skew.items()},
        "previous_skew_curve": {str(key): value for key, value in previous_skew.items()},
        "current_25d_iv_by_option_type": _stringify_side_curves(current_skew_by_side),
        "previous_25d_iv_by_option_type": _stringify_side_curves(previous_skew_by_side),
        "current_term_curve": {str(key): value for key, value in current_term.items()},
        "previous_term_curve": {str(key): value for key, value in previous_term.items()},
        "w1_shift": w1,
        "skew_shift": skew_shift,
        "term_shift": term_shift,
        "tail_mass_change": None,
        "blockers": blockers,
        "method": "equal_mass_w1_over_constant_tenor_atm_iv",
        "skew_method": "put_minus_call_25d_skew_risk_reversal",
        "explanation_only": True,
        "strategy_effect": False,
        "w2_offline_only": True,
    }


def materialize_surface_shift(
    runtime: DatabaseRuntime, *, symbol: str, as_of: datetime, snapshot_id: int | None = None,
    capture_generation_id: int | None = None,
    current_analysis_run_id: Any | None = None, model_revision: str = "history-v3",
    mode: str = "historical_evidence",
    feature_version: str = FEATURE_VERSION,
) -> dict[str, Any]:
    """Store one shift anchored to one complete point-in-time snapshot."""
    cutoff = _utc(as_of)
    with runtime.read(JOB_PROFILE) as connection:
        if snapshot_id is None:
            snapshots = connection.execute(
                """
                SELECT snapshot.id, snapshot.slot_at, instrument.id AS instrument_id,
                       generation.id AS capture_generation_id,
                       generation.capture_finished_at AS available_at
                FROM raw.option_snapshot snapshot
                JOIN catalog.instrument instrument ON instrument.symbol = snapshot.history_symbol
                JOIN LATERAL (
                    SELECT candidate.id, candidate.capture_finished_at
                    FROM raw.option_capture_generation candidate
                    WHERE candidate.snapshot_id = snapshot.id
                      AND candidate.capture_state = 'complete'
                      AND candidate.capture_finished_at <= %s
                    ORDER BY candidate.capture_finished_at DESC, candidate.generation DESC, candidate.id DESC
                    LIMIT 1
                ) generation ON true
                WHERE snapshot.history_symbol = %s AND snapshot.collection_profile = 'history_full'
                  AND snapshot.slot_at <= %s
                ORDER BY snapshot.slot_at DESC, snapshot.id DESC LIMIT 2
                """,
                [cutoff, symbol.strip().upper(), cutoff],
            ).fetchall()
        else:
            current = connection.execute(
                """
                SELECT snapshot.id, snapshot.slot_at, instrument.id AS instrument_id,
                       generation.id AS capture_generation_id,
                       generation.capture_finished_at AS available_at
                FROM raw.option_snapshot snapshot
                JOIN catalog.instrument instrument ON instrument.symbol = snapshot.history_symbol
                JOIN LATERAL (
                    SELECT candidate.id, candidate.capture_finished_at
                    FROM raw.option_capture_generation candidate
                    WHERE candidate.snapshot_id = snapshot.id
                      AND candidate.capture_state = 'complete'
                      AND candidate.capture_finished_at <= %s
                      AND (%s IS NULL OR candidate.id = %s)
                    ORDER BY candidate.capture_finished_at DESC, candidate.generation DESC, candidate.id DESC
                    LIMIT 1
                ) generation ON true
                WHERE snapshot.id = %s AND snapshot.history_symbol = %s
                  AND snapshot.collection_profile = 'history_full'
                  AND snapshot.slot_at <= %s
                """,
                [cutoff, capture_generation_id, capture_generation_id,
                 snapshot_id, symbol.strip().upper(), cutoff],
            ).fetchone()
            previous = connection.execute(
                """
                SELECT snapshot.id, snapshot.slot_at, instrument.id AS instrument_id,
                       generation.id AS capture_generation_id,
                       generation.capture_finished_at AS available_at
                FROM raw.option_snapshot snapshot
                JOIN catalog.instrument instrument ON instrument.symbol = snapshot.history_symbol
                JOIN LATERAL (
                    SELECT candidate.id, candidate.capture_finished_at
                    FROM raw.option_capture_generation candidate
                    WHERE candidate.snapshot_id = snapshot.id
                      AND candidate.capture_state = 'complete'
                      AND candidate.capture_finished_at <= %s
                    ORDER BY candidate.capture_finished_at DESC, candidate.generation DESC, candidate.id DESC
                    LIMIT 1
                ) generation ON true
                WHERE snapshot.history_symbol = %s AND snapshot.collection_profile = 'history_full'
                  AND snapshot.slot_at < %s
                ORDER BY snapshot.slot_at DESC, snapshot.id DESC LIMIT 1
                """,
                [cutoff, symbol.strip().upper(), current["slot_at"] if current else cutoff],
            ).fetchone()
            snapshots = [row for row in (current, previous) if row is not None]
        if len(snapshots) < 2:
            return _insufficient(symbol, cutoff, "two_complete_snapshots_required")
        rows_by_snapshot: dict[int, list[dict[str, Any]]] = {}
        for snapshot in snapshots:
            selected_run_id = current_analysis_run_id if snapshot is snapshots[0] else None
            rows = connection.execute(
                """
                WITH chosen_run AS (
                    SELECT summary.analysis_run_id, run.finished_at
                    FROM analysis.option_surface_summary summary
                    JOIN analysis.run run ON run.id = summary.analysis_run_id
                    WHERE summary.snapshot_id = %s AND summary.capture_generation_id = %s
                      AND run.status = 'succeeded'
                      AND (
                        (%s::uuid IS NOT NULL AND run.id = %s::uuid)
                        OR (%s::uuid IS NULL
                            AND run.summary->>'model_revision' = %s
                            AND coalesce(run.summary->>'mode', 'historical_evidence') = %s
                            AND run.input_cutoff <= %s)
                      )
                    GROUP BY summary.analysis_run_id, run.finished_at
                    ORDER BY run.finished_at ASC, summary.analysis_run_id ASC LIMIT 1
                ), canonical AS (
                    SELECT summary.dte, summary.option_type, summary.atm_iv,
                           summary.delta_25_iv, summary.skew_25, summary.term_slope,
                           summary.fit_status, summary.analysis_run_id
                    FROM analysis.option_surface_summary summary
                    JOIN chosen_run chosen ON chosen.analysis_run_id = summary.analysis_run_id
                    WHERE summary.snapshot_id = %s AND summary.capture_generation_id = %s
                )
                SELECT dte, option_type, atm_iv, delta_25_iv, skew_25, term_slope,
                       analysis_run_id::text AS analysis_run_id
                FROM canonical
                WHERE fit_status = 'succeeded'
                ORDER BY dte
                """,
                [snapshot["id"], snapshot["capture_generation_id"],
                 selected_run_id, selected_run_id, selected_run_id,
                 model_revision, mode, cutoff,
                 snapshot["id"], snapshot["capture_generation_id"]],
            ).fetchall()
            rows_by_snapshot[int(snapshot["id"])] = [dict(row) for row in rows]
    current, previous = snapshots[0], snapshots[1]
    payload = surface_shift_payload(
        rows_by_snapshot[int(current["id"])], rows_by_snapshot[int(previous["id"])]
    )
    current_run_id = _analysis_run_id(rows_by_snapshot[int(current["id"])])
    previous_run_id = _analysis_run_id(rows_by_snapshot[int(previous["id"])])
    if current_run_id is None or previous_run_id is None:
        return _insufficient(symbol, cutoff, "surface_analysis_run_missing")
    details = {
        key: value for key, value in payload.items()
        if key not in {"w1_shift", "skew_shift", "term_shift", "tail_mass_change", "tenors", "evidence_state"}
    }
    details.update({
        "current_snapshot_id": int(current["id"]),
        "current_capture_generation_id": int(current["capture_generation_id"]),
        "current_analysis_run_id": current_run_id,
        "current_snapshot_slot": current["slot_at"].isoformat(),
        "previous_snapshot_id": int(previous["id"]),
        "previous_capture_generation_id": int(previous["capture_generation_id"]),
        "previous_analysis_run_id": previous_run_id,
        "previous_snapshot_slot": previous["slot_at"].isoformat(),
        "previous_capture_finished_at": previous["available_at"].isoformat(),
    })
    with runtime.transaction(JOB_PROFILE) as connection:
        connection.execute(
            """
            INSERT INTO analysis.option_surface_shift (
                instrument_id, current_capture_generation_id, previous_capture_generation_id,
                current_analysis_run_id, previous_analysis_run_id,
                as_of, previous_as_of, feature_version, tenors,
                w1_shift, tail_mass_change, skew_shift, term_shift, evidence_state, details
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (current_analysis_run_id, previous_analysis_run_id, feature_version)
            DO NOTHING
            """,
            [current["instrument_id"], current["capture_generation_id"],
             previous["capture_generation_id"], current_run_id, previous_run_id,
             cutoff, previous["available_at"], feature_version,
             list(TENORS), payload["w1_shift"], payload["tail_mass_change"], payload["skew_shift"],
             payload["term_shift"], payload["evidence_state"], Jsonb(details)],
        )
    return {"symbol": symbol.strip().upper(), "as_of": cutoff,
            "previous_as_of": previous["available_at"], "feature_version": feature_version, **payload}


def surface_shift_rows(runtime: DatabaseRuntime, *, symbol: str, as_of: datetime) -> dict[str, Any]:
    cutoff = _utc(as_of)
    with runtime.read() as connection:
        row = connection.execute(
            """
            SELECT instrument.symbol, shift.as_of, shift.previous_as_of, shift.feature_version,
                   shift.tenors, shift.w1_shift, shift.tail_mass_change, shift.skew_shift,
                   shift.term_shift, shift.evidence_state, shift.details
            FROM analysis.option_surface_shift shift
            JOIN catalog.instrument instrument ON instrument.id = shift.instrument_id
            JOIN analysis.run current_run ON current_run.id = shift.current_analysis_run_id
            WHERE instrument.symbol = %s AND shift.as_of = %s
            ORDER BY current_run.finished_at ASC, shift.current_analysis_run_id ASC LIMIT 1
            """,
            [symbol.strip().upper(), cutoff],
        ).fetchone()
    if row is None:
        return _insufficient(symbol, cutoff, "stored_surface_shift_missing")
    payload = dict(row)
    payload["details"] = dict(payload.get("details") or {})
    payload["explanation_only"] = True
    payload["strategy_effect"] = False
    return payload


def _matched_curve_change(
    current: dict[int, float | None], previous: dict[int, float | None], tenors: tuple[int, ...],
) -> float | None:
    changes = [
        float(current[tenor]) - float(previous[tenor])
        for tenor in tenors
        if current.get(tenor) is not None and previous.get(tenor) is not None
    ]
    return sum(changes) / len(changes) if changes else None


def _curves_by_option_type(
    rows: list[dict[str, Any]], *, field: str, tenors: tuple[int, ...],
) -> dict[str, dict[int, float | None]]:
    return {
        option_type: constant_tenor_curve(
            (
                {"dte": row.get("dte"), "value": row.get(field)}
                for row in rows
                if str(row.get("option_type") or "").lower() == option_type
            ),
            tenors=tenors,
            positive_only=False,
        )
        for option_type in ("call", "put")
    }


def _put_call_risk_reversal(
    curves: dict[str, dict[int, float | None]], tenors: tuple[int, ...],
) -> dict[int, float | None]:
    calls, puts = curves["call"], curves["put"]
    return {
        tenor: (
            float(puts[tenor]) - float(calls[tenor])
            if puts.get(tenor) is not None and calls.get(tenor) is not None
            else None
        )
        for tenor in tenors
    }


def _stringify_side_curves(
    curves: dict[str, dict[int, float | None]],
) -> dict[str, dict[str, float | None]]:
    return {
        option_type: {str(tenor): value for tenor, value in curve.items()}
        for option_type, curve in curves.items()
    }


def _analysis_run_id(rows: list[dict[str, Any]]) -> str | None:
    values = {str(row["analysis_run_id"]) for row in rows if row.get("analysis_run_id")}
    return next(iter(values)) if len(values) == 1 else None


def _finite(value: Any) -> bool:
    try:
        return isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _finite_positive(value: Any) -> bool:
    return _finite(value) and float(value) > 0


def _insufficient(symbol: str, as_of: datetime, blocker: str) -> dict[str, Any]:
    return {
        "symbol": symbol.strip().upper(), "as_of": as_of, "previous_as_of": None,
        "feature_version": FEATURE_VERSION, "tenors": list(TENORS), "w1_shift": None,
        "tail_mass_change": None, "skew_shift": None, "term_shift": None,
        "evidence_state": "insufficient_surface_evidence",
        "details": {"blockers": [blocker], "explanation_only": True, "strategy_effect": False},
        "explanation_only": True, "strategy_effect": False,
    }


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
