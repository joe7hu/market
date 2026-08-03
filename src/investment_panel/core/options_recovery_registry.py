"""Typed, replayable strategy registry for forward option-recovery signals.

The registry is intentionally independent of a provider or database row shape.
Live scoring, replay, and agent mutation validation all use these same small
types so an agent cannot smuggle an unimplemented setting into a revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from typing import Any, Literal, Mapping

from investment_panel.core.options_recovery import OBJECTIVE_VERSION


SHOCK_REVERSAL_CALL_V1 = "shock_reversal_call_v1"
SHOCK_CONTINUATION_PUT_V1 = "shock_continuation_put_v1"
RECOVERY_FAMILIES = (SHOCK_REVERSAL_CALL_V1, SHOCK_CONTINUATION_PUT_V1)


@dataclass(frozen=True)
class EventSpot:
    observed_at: datetime
    available_at: datetime
    price: float


@dataclass(frozen=True)
class RecoveryEventState:
    event_id: str
    symbol: str
    reference_price: float
    event_low: float
    started_at: datetime
    spots: tuple[EventSpot, ...]
    selloff_trigger_satisfied: bool = True


@dataclass(frozen=True)
class RecoveryContractQuote:
    contract_id: int
    occ_symbol: str
    option_type: Literal["call", "put"]
    expiration: date
    strike: float
    bid: float | None
    ask: float | None
    bid_size: int | None
    ask_size: int | None
    open_interest: int | None
    delta: float | None
    observed_at: datetime
    available_at: datetime
    stable_identity: bool = True
    volume: int | None = None


@dataclass(frozen=True)
class GateResult:
    eligible: bool
    blockers: tuple[str, ...]
    dte: int | None
    spread_pct: float | None


@dataclass(frozen=True)
class FamilySignal:
    family: str
    active: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RankedRecoveryCandidate:
    family: str
    quote: RecoveryContractQuote
    gate: GateResult
    lower_confidence_expectancy: float
    maximum_loss: float
    selection_score: float


@dataclass(frozen=True)
class RegistryStrategy:
    key: str
    revision: int
    name: str
    parameters: Mapping[str, float | int]


_COMMON_PARAMETERS: dict[str, float | int] = {
    "min_dte": 7,
    "max_dte": 45,
    "delta_min": 0.35,
    "delta_max": 0.65,
    "max_spread_pct": 0.20,
    "min_open_interest": 100,
}

_STRATEGIES: dict[str, RegistryStrategy] = {
    SHOCK_REVERSAL_CALL_V1: RegistryStrategy(
        key=SHOCK_REVERSAL_CALL_V1,
        revision=1,
        name="Shock reversal calls",
        parameters={
            **_COMMON_PARAMETERS,
            "reversal_min_rebound_pct": 0.02,
            "reversal_breakout_slots": 4,
        },
    ),
    SHOCK_CONTINUATION_PUT_V1: RegistryStrategy(
        key=SHOCK_CONTINUATION_PUT_V1,
        revision=1,
        name="Shock continuation puts",
        parameters={
            **_COMMON_PARAMETERS,
            "continuation_reclaim_fraction": 0.50,
            "continuation_failed_reclaim_captures": 2,
        },
    ),
}

_RANGES: dict[str, tuple[float, float]] = {
    "min_dte": (1, 120),
    "max_dte": (1, 120),
    "delta_min": (0.01, 0.99),
    "delta_max": (0.01, 0.99),
    "max_spread_pct": (0.01, 1.0),
    "min_open_interest": (0, 1_000_000),
    "reversal_min_rebound_pct": (0.001, 0.50),
    "reversal_breakout_slots": (1, 24),
    "continuation_reclaim_fraction": (0.01, 0.99),
    "continuation_failed_reclaim_captures": (2, 24),
}


def strategies() -> tuple[RegistryStrategy, ...]:
    """Return the complete, deterministic set of recovery families."""

    return tuple(_STRATEGIES[key] for key in RECOVERY_FAMILIES)


def strategy_for(key: str) -> RegistryStrategy:
    try:
        return _STRATEGIES[str(key)]
    except KeyError as exc:
        raise ValueError(f"unsupported recovery strategy: {key}") from exc


def validate_mutation(key: str, changes: Mapping[str, Any]) -> dict[str, float | int]:
    """Validate a mutation before it can become a persisted strategy revision."""

    strategy = strategy_for(key)
    unknown = sorted(set(changes) - set(strategy.parameters))
    if unknown:
        raise ValueError(f"unsupported recovery strategy parameter(s): {', '.join(unknown)}")
    normalized = dict(strategy.parameters)
    for name, raw in changes.items():
        if isinstance(raw, bool):
            raise ValueError(f"recovery strategy parameter {name} must be numeric")
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"recovery strategy parameter {name} must be numeric") from exc
        minimum, maximum = _RANGES[name]
        if not isfinite(value) or value < minimum or value > maximum:
            raise ValueError(f"recovery strategy parameter {name} is out of range")
        template = strategy.parameters[name]
        normalized[name] = int(value) if isinstance(template, int) else value
    if normalized["min_dte"] > normalized["max_dte"]:
        raise ValueError("min_dte cannot exceed max_dte")
    if normalized["delta_min"] > normalized["delta_max"]:
        raise ValueError("delta_min cannot exceed delta_max")
    return normalized


def signal_for(event: RecoveryEventState, family: str) -> FamilySignal:
    """Evaluate a family solely from event-strip spot observations."""

    strategy = strategy_for(family)
    if not event.selloff_trigger_satisfied or event.reference_price <= event.event_low:
        return FamilySignal(family, False, ("selloff_trigger_not_satisfied",))
    spots = tuple(sorted(event.spots, key=lambda item: (item.available_at, item.observed_at)))
    if family == SHOCK_REVERSAL_CALL_V1:
        return _reversal_signal(event, spots, strategy.parameters)
    return _continuation_signal(event, spots, strategy.parameters)


def contract_gate(
    quote: RecoveryContractQuote,
    *,
    family: str,
    as_of: datetime,
) -> GateResult:
    """Apply common contract gates; every failed gate remains explainable."""

    parameters = strategy_for(family).parameters
    blockers: list[str] = []
    dte = (quote.expiration - as_of.date()).days
    if not (int(parameters["min_dte"]) <= dte <= int(parameters["max_dte"])):
        blockers.append("dte_outside_7_to_45")
    if quote.option_type != ("call" if family == SHOCK_REVERSAL_CALL_V1 else "put"):
        blockers.append("family_option_type_mismatch")
    delta = _finite(quote.delta)
    if delta is None or not (float(parameters["delta_min"]) <= abs(delta) <= float(parameters["delta_max"])):
        blockers.append("absolute_delta_outside_0_35_to_0_65")
    bid, ask = _finite(quote.bid), _finite(quote.ask)
    spread_pct = None
    if bid is None or ask is None or bid <= 0 or ask < bid:
        blockers.append("positive_uncrossed_bid_ask_required")
    else:
        midpoint = (bid + ask) / 2.0
        spread_pct = (ask - bid) / midpoint if midpoint else None
        if spread_pct is None or spread_pct > float(parameters["max_spread_pct"]):
            blockers.append("spread_wider_than_20pct")
    if (quote.bid_size or 0) <= 0 or (quote.ask_size or 0) <= 0:
        blockers.append("displayed_size_required")
    if (quote.open_interest or 0) < int(parameters["min_open_interest"]):
        blockers.append("open_interest_below_100")
    if quote.available_at.tzinfo is None or quote.available_at.date() != as_of.date() or quote.available_at > as_of:
        blockers.append("current_session_quote_required")
    if not quote.stable_identity:
        blockers.append("stable_contract_identity_required")
    return GateResult(not blockers, tuple(sorted(set(blockers))), dte, spread_pct)


def rank_candidate(
    *,
    event: RecoveryEventState,
    family: str,
    quote: RecoveryContractQuote,
    as_of: datetime,
    lower_confidence_expectancy: float | None,
    maximum_loss: float,
) -> RankedRecoveryCandidate:
    """Rank by lower-confidence executable expectancy per maximum-loss dollar."""

    gate = contract_gate(quote, family=family, as_of=as_of)
    lower = max(float(lower_confidence_expectancy or 0.0), 0.0)
    risk = max(float(maximum_loss), 0.01)
    spread = gate.spread_pct if gate.spread_pct is not None else 1.0
    liquidity = min((quote.open_interest or 0) / 1_000.0, 1.0)
    # The primary score is deliberately the conservative expectancy ratio.
    # Liquidity and signal direction only provide deterministic tie-breaking.
    direction = 1.0 if family == SHOCK_REVERSAL_CALL_V1 else 0.9
    score = lower / risk + liquidity * 1e-4 + max(0.0, 0.20 - spread) * 1e-5 + direction * 1e-7
    return RankedRecoveryCandidate(family, quote, gate, lower, risk, score)


def objective_version() -> str:
    return OBJECTIVE_VERSION


def _reversal_signal(
    event: RecoveryEventState,
    spots: tuple[EventSpot, ...],
    parameters: Mapping[str, float | int],
) -> FamilySignal:
    if len(spots) < 5:
        return FamilySignal(SHOCK_REVERSAL_CALL_V1, False, ("insufficient_spot_observations",))
    threshold = event.event_low * (1.0 + float(parameters["reversal_min_rebound_pct"]))
    current, previous = spots[-1], spots[-2]
    window = spots[-(int(parameters["reversal_breakout_slots"]) + 1):-1]
    if current.price < threshold or previous.price < threshold:
        return FamilySignal(SHOCK_REVERSAL_CALL_V1, False, ("two_consecutive_rebound_slots_required",))
    if not window or current.price <= max(item.price for item in window):
        return FamilySignal(SHOCK_REVERSAL_CALL_V1, False, ("four_slot_breakout_required",))
    return FamilySignal(
        SHOCK_REVERSAL_CALL_V1,
        True,
        ("selloff_trigger_satisfied", "two_consecutive_rebound_slots", "four_slot_breakout"),
    )


def _continuation_signal(
    event: RecoveryEventState,
    spots: tuple[EventSpot, ...],
    parameters: Mapping[str, float | int],
) -> FamilySignal:
    required = int(parameters["continuation_failed_reclaim_captures"])
    if len(spots) < required + 1:
        return FamilySignal(SHOCK_CONTINUATION_PUT_V1, False, ("insufficient_spot_observations",))
    reclaim = event.event_low + (event.reference_price - event.event_low) * float(parameters["continuation_reclaim_fraction"])
    latest = spots[-1]
    failed = spots[-required:]
    earlier_lows = [item.price for item in spots[:-1]]
    if any(item.price >= reclaim for item in failed):
        return FamilySignal(SHOCK_CONTINUATION_PUT_V1, False, ("failed_half_gap_reclaim_not_confirmed",))
    if not earlier_lows or latest.price >= min(earlier_lows):
        return FamilySignal(SHOCK_CONTINUATION_PUT_V1, False, ("fresh_event_low_required",))
    return FamilySignal(
        SHOCK_CONTINUATION_PUT_V1,
        True,
        ("selloff_trigger_satisfied", "failed_half_gap_reclaim", "fresh_event_low"),
    )


def _finite(value: float | int | None) -> float | None:
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and isfinite(number) else None
