"""Central underwriting rules for QQQ's paper-only option decision system.

Paper-state transition diagram:

    invalid evidence/thesis/EV -> REJECT
    insufficient samples/fit    -> COLLECTING
    valid but gates incomplete  -> WATCH
    exact calibrated gates      -> PAPER_READY -> pending next-cohort entry
"""

from __future__ import annotations

import random
from datetime import datetime
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

    blockers = [*_structure_blockers(legs, structure), *_coherent_legs(legs)]
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

    blockers = [*_structure_blockers(legs, structure), *_coherent_legs(legs)]
    if blockers:
        return None, blockers
    total = 0.0
    for leg in legs:
        side = str(leg.get("side"))
        value = leg.get("bid") if side == "long" else leg.get("ask")
        total += float(value) if side == "long" else -float(value)
    return total, []


def historical_payoff_statistics(
    *,
    spot: float,
    legs: list[dict[str, Any]],
    terminal_returns: tuple[float, ...] | list[float],
    seed: int,
    bootstrap_samples: int = 10_000,
) -> dict[str, Any]:
    """Evaluate conservative one-unit expiry payoffs over point-in-time return paths.

    The function accepts returns that have already been selected by the database
    as-of query.  It therefore owns deterministic payoff/bootstrapping only and
    cannot silently read a current market source during historical replay.
    """

    entry_price, blockers = conservative_entry(legs, "debit_spread" if len(legs) > 1 else "long_option")
    if entry_price is None or blockers or spot <= 0:
        return {
            "scenario_count": 0, "entry_price": entry_price, "max_loss": None,
            "expected_value": None, "lower_95_expected_value": None,
            "probability_profit": None, "seed": seed, "blockers": blockers or ["invalid_entry"],
        }
    premiums = [float(leg["ask"] if leg.get("side") == "long" else leg["bid"]) for leg in legs]
    payoffs: list[float] = []
    for terminal_return in terminal_returns:
        terminal_spot = spot * (1.0 + float(terminal_return))
        payoff = 0.0
        for leg, premium in zip(legs, premiums, strict=True):
            option_type = str(leg.get("option_type") or "").lower()
            strike = float(leg["strike"])
            intrinsic = max(terminal_spot - strike, 0.0) if option_type == "call" else max(strike - terminal_spot, 0.0)
            direction = 1.0 if str(leg.get("side")) == "long" else -1.0
            payoff += direction * (intrinsic - premium) * 100.0
        payoffs.append(payoff)
    if not payoffs:
        return {
            "scenario_count": 0, "entry_price": entry_price, "max_loss": entry_price * 100.0,
            "expected_value": None, "lower_95_expected_value": None,
            "probability_profit": None, "seed": seed, "blockers": ["insufficient_return_paths"],
        }
    expected = sum(payoffs) / len(payoffs)
    rng = random.Random(seed)
    means = sorted(
        sum(payoffs[rng.randrange(len(payoffs))] for _ in range(len(payoffs))) / len(payoffs)
        for _ in range(max(1, bootstrap_samples))
    )
    lower_index = max(0, int(0.05 * (len(means) - 1)))
    return {
        "scenario_count": len(payoffs), "entry_price": entry_price, "max_loss": entry_price * 100.0,
        "expected_value": expected, "lower_95_expected_value": means[lower_index],
        "probability_profit": sum(payoff > 0 for payoff in payoffs) / len(payoffs),
        "seed": seed, "blockers": [],
    }


def _coherent_legs(legs: list[dict[str, Any]]) -> list[str]:
    if not legs:
        return ["missing_leg"]
    timestamps = []
    for leg in legs:
        if str(leg.get("side")) not in {"long", "short"}:
            return ["invalid_leg_side"]
        if leg.get("bid") is None or leg.get("ask") is None or float(leg["bid"]) < 0 or float(leg["ask"]) < float(leg["bid"]):
            return ["crossed_or_missing_leg"]
        if leg.get("size_available") is not True:
            return ["displayed_size_unavailable"]
        observed_at = _as_datetime(leg.get("observed_at"))
        if observed_at is None:
            return ["missing_leg_timestamp"]
        available_at = _as_datetime(leg.get("available_at")) or observed_at
        quote_age = (available_at - observed_at).total_seconds()
        if quote_age < 0 or quote_age > 180:
            return ["quote_age_stale"]
        timestamps.append(observed_at)
    if len(timestamps) >= 2:
        first, last = min(timestamps), max(timestamps)
        if (last - first).total_seconds() > 5:
            return ["interleg_timestamp_skew"]
    return []


def _structure_blockers(legs: list[dict[str, Any]], structure: str) -> list[str]:
    if structure in {"long_call", "long_put", "long_option"}:
        if len(legs) != 1 or str(legs[0].get("side")) != "long":
            return ["invalid_long_option_structure"]
        option_type = str(legs[0].get("option_type") or "").lower()
        if structure == "long_call" and option_type != "call":
            return ["invalid_long_option_structure"]
        if structure == "long_put" and option_type != "put":
            return ["invalid_long_option_structure"]
        return []
    if structure not in {"call_debit_spread", "put_debit_spread", "debit_spread"}:
        return []
    if len(legs) != 2:
        return ["missing_spread_leg"]
    long_legs = [leg for leg in legs if str(leg.get("side")) == "long"]
    short_legs = [leg for leg in legs if str(leg.get("side")) == "short"]
    if len(long_legs) != 1 or len(short_legs) != 1:
        return ["invalid_spread_sides"]
    long_leg, short_leg = long_legs[0], short_legs[0]
    if long_leg.get("option_type") != short_leg.get("option_type"):
        return ["spread_type_mismatch"]
    long_strike, short_strike = float(long_leg["strike"]), float(short_leg["strike"])
    option_type = str(long_leg.get("option_type") or "").lower()
    if structure == "call_debit_spread" and (option_type != "call" or long_strike >= short_strike):
        return ["invalid_call_debit_spread_order"]
    if structure == "put_debit_spread" and (option_type != "put" or long_strike <= short_strike):
        return ["invalid_put_debit_spread_order"]
    return []


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
