"""Central underwriting rules for QQQ's paper-only option decision system.

Paper-state transition diagram:

    invalid evidence/thesis/EV -> REJECT
    insufficient samples/fit    -> COLLECTING
    valid but gates incomplete  -> WATCH
    exact calibrated gates      -> PAPER_READY -> pending next-cohort entry
"""

from __future__ import annotations

from typing import Any, Literal


PaperState = Literal["COLLECTING", "WATCH", "PAPER_READY", "REJECT"]
STRUCTURES = ("long_call", "long_put", "call_debit_spread", "put_debit_spread")


def thesis_v2_blocker(thesis: dict[str, Any] | None) -> str | None:
    if not thesis or int(thesis.get("schema_version") or 1) != 2:
        return "thesis_upgrade_required"
    direction = thesis.get("direction")
    if direction not in {"bullish", "bearish"}:
        return "thesis_direction_required"
    if not thesis.get("horizon_date") or not str(thesis.get("invalidation") or "").strip():
        return "thesis_v2_incomplete"
    try:
        if float(thesis.get("max_loss")) <= 0:
            return "thesis_max_loss_required"
    except (TypeError, ValueError):
        return "thesis_max_loss_required"
    return None


def permitted_structures(direction: str | None) -> tuple[str, ...]:
    if direction == "bullish":
        return ("long_call", "call_debit_spread")
    if direction == "bearish":
        return ("long_put", "put_debit_spread")
    return ()


def paper_state(
    *,
    structure: str,
    lane: str,
    thesis: dict[str, Any] | None,
    fit_status: str | None,
    blockers: list[str] | tuple[str, ...] = (),
    scenario_count: int = 0,
    expected_value: float | None = None,
    lower_95_expected_value: float | None = None,
    max_loss: float | None = None,
    data_confidence: float | None = None,
    execution_confidence: float | None = None,
    calibration: dict[str, Any] | None = None,
    current_complete_generation: bool = True,
) -> dict[str, Any]:
    """Return the only allowable option-local state plus explicit reasons."""

    reasons: list[str] = []
    terminal = list(blockers)
    if fit_status != "succeeded":
        terminal.append("fit_failed")
    if not current_complete_generation:
        terminal.append("current_complete_generation_required")
    if terminal:
        return {"paper_state": "REJECT", "reasons": [], "blockers": sorted(set(terminal))}
    if lane == "anomaly" and thesis is None:
        return {"paper_state": "WATCH", "reasons": ["research_evidence_only"], "blockers": ["thesis_upgrade_required"]}
    thesis_blocker = thesis_v2_blocker(thesis)
    if thesis_blocker:
        return {"paper_state": "WATCH", "reasons": ["thesis_required_for_underwriting"], "blockers": [thesis_blocker]}
    assert thesis is not None
    if scenario_count < 20:
        return {"paper_state": "COLLECTING", "reasons": ["independent_return_samples_collecting"], "blockers": []}
    if structure not in permitted_structures(str(thesis.get("direction"))):
        return {"paper_state": "REJECT", "reasons": [], "blockers": ["structure_thesis_direction_mismatch"]}
    if max_loss is None or max_loss > float(thesis["max_loss"]):
        return {"paper_state": "REJECT", "reasons": [], "blockers": ["thesis_max_loss_exceeded"]}
    if expected_value is None or lower_95_expected_value is None or expected_value <= 0 or lower_95_expected_value <= 0:
        return {"paper_state": "REJECT", "reasons": [], "blockers": ["nonpositive_expected_value_lower_bound"]}
    if (data_confidence or 0) < 0.80 or (execution_confidence or 0) < 0.70:
        return {"paper_state": "WATCH", "reasons": ["confidence_gate_incomplete"], "blockers": []}
    calibration = calibration or {}
    if int(calibration.get("sample_size") or 0) < 30:
        return {"paper_state": "WATCH", "reasons": ["exact_structure_regime_calibration_collecting"], "blockers": []}
    if float(calibration.get("lower_95_expectancy") or 0) <= 0 or float(calibration.get("brier_score") or 1) > 0.25:
        return {"paper_state": "WATCH", "reasons": ["exact_structure_regime_calibration_not_ready"], "blockers": []}
    if int(calibration.get("other_regime_monitoring_count") or 0) < 5:
        return {"paper_state": "WATCH", "reasons": ["other_regime_monitoring_collecting"], "blockers": []}
    reasons.append("all_paper_only_promotion_gates_passed")
    return {"paper_state": "PAPER_READY", "reasons": reasons, "blockers": []}


def conservative_entry(legs: list[dict[str, Any]], structure: str) -> tuple[float | None, list[str]]:
    """Use ask for long legs and bid for short legs; never invent a midpoint fill."""

    blockers = _coherent_legs(legs)
    if blockers:
        return None, blockers
    total = 0.0
    for leg in legs:
        side = str(leg.get("side"))
        value = leg.get("ask") if side == "long" else leg.get("bid")
        total += float(value) if side == "long" else -float(value)
    if structure.endswith("debit_spread") and total <= 0:
        return None, ["nonpositive_debit"]
    return total, []


def conservative_mark(legs: list[dict[str, Any]], structure: str) -> tuple[float | None, list[str]]:
    """Use bid for long legs and ask for short legs from the same later cohort."""

    blockers = _coherent_legs(legs)
    if blockers:
        return None, blockers
    total = 0.0
    for leg in legs:
        side = str(leg.get("side"))
        value = leg.get("bid") if side == "long" else leg.get("ask")
        total += float(value) if side == "long" else -float(value)
    return total, []


def _coherent_legs(legs: list[dict[str, Any]]) -> list[str]:
    if not legs:
        return ["missing_leg"]
    timestamps = []
    for leg in legs:
        if str(leg.get("side")) not in {"long", "short"}:
            return ["invalid_leg_side"]
        if leg.get("bid") is None or leg.get("ask") is None or float(leg["bid"]) < 0 or float(leg["ask"]) < float(leg["bid"]):
            return ["crossed_or_missing_leg"]
        if leg.get("size_available") is False:
            return ["displayed_size_unavailable"]
        if leg.get("observed_at") is not None:
            timestamps.append(leg["observed_at"])
    if len(timestamps) >= 2:
        first, last = min(timestamps), max(timestamps)
        if (last - first).total_seconds() > 5:
            return ["interleg_timestamp_skew"]
    return []
