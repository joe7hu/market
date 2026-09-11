"""Concrete pure strategy implementations.

The strategy catalog owns names, bindings, and persisted definitions.  This
module owns the calculations those bindings point at.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investment_panel.domain.factors import EvaluationContext, InputSnapshot, evaluate_factors
from investment_panel.domain.research.stock_alpha import content_hash
from investment_panel.domain.signals import MOMENTUM_VOLATILITY, evaluate_signals


DAILY_ACTIONABILITY = Literal["daily_research", "shadow_only", "research_only", "registration_only"]


class StrategySignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    strategy_key: str = Field(min_length=1)
    status: Literal["available", "unavailable", "blocked"]
    value: float | None = None
    direction: str | None = None
    horizon: str = "daily"
    actionability: DAILY_ACTIONABILITY = "daily_research"
    regime: str | None = None
    blockers: tuple[str, ...] = ()
    evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def reject_boolean_values(cls, values: Any) -> Any:
        if isinstance(values, Mapping) and isinstance(values.get("value"), bool):
            raise ValueError("strategy signal value cannot be boolean")
        return values

    @model_validator(mode="after")
    def validate_status_payload(self) -> "StrategySignal":
        if self.status != "available" and self.value is not None:
            raise ValueError("non-available strategy signal cannot contain a value")
        return self


class StrategyParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TrendParameters(StrategyParameters):
    lookback_days: int = Field(default=20, ge=1, le=252)


class EmptyParameters(StrategyParameters):
    pass


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _daily_rows(inputs: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    cutoff = _parse_clock(inputs.get("input_cutoff"))
    if cutoff is None:
        return []
    rows: list[Mapping[str, Any]] = []
    for raw_row in inputs.get("daily_bars", ()):
        if not isinstance(raw_row, Mapping):
            continue
        observed = _parse_clock(raw_row.get("observed_at"))
        available = _parse_clock(raw_row.get("available_at"))
        session = raw_row.get("trading_date") or raw_row.get("date")
        session_date = _parse_session(session)
        if session_date is not None and session_date > cutoff.date():
            continue
        if observed is None and available is None and not str(session or "").strip():
            continue
        row = dict(raw_row)
        authoritative = (
            row.get("status") == "confirmed"
            and row.get("confirmed") is True
            and row.get("disabled") is False
            and observed is not None
            and available is not None
            and observed <= cutoff
            and available <= cutoff
        )
        if not authoritative:
            row["close"] = None
            row["open"] = None
        rows.append(row)
    rows = sorted(rows, key=lambda row: (str(row.get("trading_date") or row.get("date") or ""), str(row.get("id") or "")))
    required = _required_sessions(inputs)
    if required:
        by_date = {_parse_session(row.get("trading_date") or row.get("date")): row for row in rows}
        rows = [
            by_date.get(session, {"trading_date": session.isoformat(), "close": None, "open": None, "status": "missing"})
            for session in required
        ]
    return rows


def _parse_session(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _required_sessions(inputs: Mapping[str, Any]) -> tuple[date, ...]:
    raw = inputs.get("required_trading_dates")
    if not isinstance(raw, (list, tuple)):
        return ()
    parsed = tuple(session for session in (_parse_session(item) for item in raw) if session is not None)
    return tuple(sorted(parsed))


def _daily_input_blockers(inputs: Mapping[str, Any]) -> tuple[str, ...]:
    dates = [
        _parse_session(row.get("trading_date") or row.get("date"))
        for row in inputs.get("daily_bars", ())
        if isinstance(row, Mapping)
    ]
    blockers: list[str] = []
    if any(session is None for session in dates):
        blockers.append("daily_session_date_invalid")
    present = [session for session in dates if session is not None]
    if len(set(present)) != len(present):
        blockers.append("daily_session_dates_duplicate")
    required_raw = inputs.get("required_trading_dates")
    if isinstance(required_raw, (list, tuple)):
        required = [_parse_session(item) for item in required_raw]
        if any(item is None for item in required):
            blockers.append("required_trading_dates_invalid")
        if len(set(item for item in required if item is not None)) != len([item for item in required if item is not None]):
            blockers.append("required_trading_dates_duplicate")
    return tuple(dict.fromkeys(blockers))


def _parse_clock(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def factor_snapshot_for_inputs(inputs: Mapping[str, Any]) -> InputSnapshot | None:
    cutoff = _parse_clock(inputs.get("input_cutoff"))
    if cutoff is None:
        return None
    rows = _daily_rows(inputs)
    values: dict[str, Any] = {
        "daily_closes": [_number(row.get("close")) for row in rows],
        "daily_close_dates": [row.get("trading_date") or row.get("date") for row in rows],
        "daily_opens": [_number(row.get("open")) for row in rows],
        "daily_open_dates": [row.get("trading_date") or row.get("date") for row in rows],
    }
    if "benchmark_closes" in inputs:
        values["benchmark_closes"] = inputs["benchmark_closes"]
    if "benchmark_close_dates" in inputs:
        values["benchmark_close_dates"] = inputs["benchmark_close_dates"]
    for key in ("event", "full_chain_state", "oi_volume_state", "dividend_state", "quote_quality"):
        if key in inputs:
            values[key] = inputs[key]
    evidence_refs = tuple(str(item) for item in inputs.get("evidence_refs", ()) if str(item).strip())
    return InputSnapshot(
        str(inputs.get("input_snapshot_identity") or content_hash({"cutoff": inputs.get("input_cutoff"), "bars": rows})),
        cutoff,
        values,
        evidence_refs=evidence_refs,
        source_versions=inputs.get("source_versions") or {},
    )


def daily_trend_underreaction(
    inputs: Mapping[str, Any], *, strategy_key: str = "daily_trend_underreaction_v2",
    params: TrendParameters | None = None, factor_context: EvaluationContext | None = None,
) -> StrategySignal:
    rows = _daily_rows(inputs)
    if blockers := _daily_input_blockers(inputs):
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=blockers)
    closes = [_number(row.get("close")) for row in rows]
    if len(closes) < 2:
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=("daily_close_history_incomplete",))
    validated = params or TrendParameters()
    snapshot = factor_snapshot_for_inputs(inputs)
    if snapshot is None:
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=("input_cutoff_missing",))
    factor = evaluate_factors(
        snapshot,
        {"price.momentum": validated},
        context=factor_context,
    )["price.momentum"]
    if not factor.available or factor.value is None:
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=factor.blockers)
    lookback = validated.lookback_days
    return StrategySignal(
        strategy_key=strategy_key, status="available", value=factor.value,
        direction="long" if factor.value > 0 else "short" if factor.value < 0 else "flat",
        evidence={"lookback_days": lookback, "inputs": "confirmed_daily_bars", "underreaction_test": "future_oos_required"},
    )


def daily_gap_regime(
    inputs: Mapping[str, Any], *, strategy_key: str = "daily_gap_regime_v1",
    params: EmptyParameters | None = None, factor_context: EvaluationContext | None = None,
) -> StrategySignal:
    rows = _daily_rows(inputs)
    if blockers := _daily_input_blockers(inputs):
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=blockers)
    if len(rows) < 2:
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=("daily_gap_history_incomplete",))
    current, previous = rows[-1], rows[-2]
    opening, previous_close = _number(current.get("open")), _number(previous.get("close"))
    if opening is None or previous_close is None or previous_close <= 0:
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=("gap_open_or_previous_close_missing",))
    gap = opening / previous_close - 1
    regime = "gap_up" if gap > 0 else "gap_down" if gap < 0 else "no_gap"
    return StrategySignal(
        strategy_key=strategy_key, status="available", value=gap,
        direction="continuation" if gap else "flat", regime=regime,
        evidence={"gap_pct": gap, "branches": ("continuation", "reversal"), "decision": "continuation_vs_reversal_requires_oos_regime_evidence"},
    )


def event_propagation(
    inputs: Mapping[str, Any], *, strategy_key: str = "daily_event_propagation_v1",
    params: EmptyParameters | None = None, factor_context: EvaluationContext | None = None,
) -> StrategySignal:
    event = inputs.get("event")
    cutoff = _parse_clock(inputs.get("input_cutoff"))
    if not isinstance(event, Mapping) or cutoff is None or event.get("status") != "confirmed" or event.get("confirmed") is not True or event.get("disabled") is not False or not event.get("release_at"):
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=("event_release_clock_missing",))
    release_at = _parse_clock(event.get("release_at"))
    observed_at = _parse_clock(event.get("observed_at"))
    available_at = _parse_clock(event.get("available_at"))
    if release_at is None or observed_at is None or available_at is None or release_at > cutoff or observed_at > cutoff or available_at > cutoff:
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=("event_clock_invalid_or_future",))
    actual, consensus = _number(event.get("actual")), _number(event.get("consensus"))
    if actual is None or consensus is None:
        return StrategySignal(strategy_key=strategy_key, status="unavailable", blockers=("event_actual_or_consensus_missing",))
    surprise = actual - consensus
    fill_ready = inputs.get("fill_model_proven") is True
    return StrategySignal(
        strategy_key=strategy_key, status="available", value=surprise,
        direction="long" if surprise > 0 else "short" if surprise < 0 else "flat",
        actionability="daily_research" if fill_ready else "shadow_only",
        blockers=() if fill_ready else ("event_time_fill_model_unproven",),
        evidence={"release_at": release_at.isoformat(), "surprise": surprise, "daily_only": True},
    )


def options_recovery_v2(
    inputs: Mapping[str, Any], *, strategy_key: str = "options_recovery_v2",
    params: EmptyParameters | None = None, factor_context: EvaluationContext | None = None,
) -> StrategySignal:
    cutoff = _parse_clock(inputs.get("input_cutoff"))
    required = ("full_chain_state", "oi_volume_state", "dividend_state")
    blockers = tuple(
        f"{key}_invalid" for key in required
        if cutoff is None or not _authoritative_state(inputs.get(key), cutoff)
    )
    quote_quality = inputs.get("quote_quality")
    if not isinstance(quote_quality, (int, float)) or isinstance(quote_quality, bool) or not isfinite(float(quote_quality)) or float(quote_quality) < 0:
        blockers += ("quote_quality_invalid",)
    if inputs.get("fill_model_proven") is not True:
        blockers += ("fill_model_unproven",)
    if blockers:
        return StrategySignal(strategy_key=strategy_key, status="unavailable", actionability="shadow_only", blockers=blockers)
    return StrategySignal(strategy_key=strategy_key, status="available", actionability="shadow_only", evidence={"controls": [*required, "quote_quality", "fill_model_proven"], "paper_only": True})


def _authoritative_state(value: Any, cutoff: datetime) -> bool:
    if not isinstance(value, Mapping) or not value or value.get("status") != "confirmed" or value.get("confirmed") is not True or value.get("disabled") is not False:
        return False
    available_at = _parse_clock(value.get("available_at"))
    observed_at = _parse_clock(value.get("observed_at"))
    return available_at is not None and observed_at is not None and available_at <= cutoff and observed_at <= cutoff


def crypto_funding_basis(
    inputs: Mapping[str, Any] | None = None, *, strategy_key: str = "crypto_funding_basis_v1",
    params: EmptyParameters | None = None, factor_context: EvaluationContext | None = None,
) -> StrategySignal:
    return StrategySignal(
        strategy_key=strategy_key, status="blocked", actionability="registration_only",
        blockers=("venue_identity_required", "executable_depth_required", "liquidation_data_required", "failure_scenarios_required"),
    )


def volatility_aware_momentum(
    inputs: Mapping[str, Any], *, strategy_key: str = "volatility_aware_momentum_v1",
    params: EmptyParameters | None = None, factor_context: EvaluationContext | None = None,
) -> StrategySignal:
    """A research-only strategy built from the reusable signal catalog."""

    cutoff = _parse_clock(inputs.get("input_cutoff"))
    if cutoff is None:
        return StrategySignal(strategy_key=strategy_key, status="unavailable", actionability="research_only", blockers=("input_cutoff_missing",))
    snapshot = factor_snapshot_for_inputs(inputs)
    if snapshot is None:
        return StrategySignal(strategy_key=strategy_key, status="unavailable", actionability="research_only", blockers=("input_cutoff_missing",))
    signal = evaluate_signals(snapshot, MOMENTUM_VOLATILITY.key, context=factor_context)[MOMENTUM_VOLATILITY.key]
    if not signal.available or signal.score is None:
        return StrategySignal(
            strategy_key=strategy_key, status="unavailable", actionability="research_only",
            blockers=signal.blockers, evidence={"signal_key": signal.key, "factor_request_ids": signal.factor_request_ids},
        )
    return StrategySignal(
        strategy_key=strategy_key, status="available", value=signal.score,
        direction=signal.direction, actionability="research_only",
        evidence={"signal_key": signal.key, "factor_request_ids": signal.factor_request_ids, **signal.evidence},
    )


__all__ = [
    "DAILY_ACTIONABILITY", "EmptyParameters", "StrategyParameters", "StrategySignal", "TrendParameters",
    "crypto_funding_basis", "daily_gap_regime", "daily_trend_underreaction", "event_propagation",
    "factor_snapshot_for_inputs", "options_recovery_v2", "volatility_aware_momentum",
]
