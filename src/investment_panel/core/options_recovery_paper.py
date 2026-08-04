"""Fail-closed, typed paper-sleeve risk policy for recovery."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from math import floor, isfinite
from typing import Any


@dataclass(frozen=True)
class RecoveryRiskContext:
    open_risk: float = 0.0
    symbol_open_risk: float = 0.0
    open_positions: int = 0
    daily_realized_unrealized_pnl: float = 0.0
    existing_event_family_position: bool = False


@dataclass(frozen=True)
class RecoveryRiskPolicy:
    """Immutable policy snapshot; dollar limits are derived once from config."""

    version: str
    sleeve_capital: float | None
    max_risk_per_trade_pct: float | None
    max_open_risk_pct: float | None
    max_symbol_risk_pct: float | None
    daily_loss_halt_pct: float | None
    max_open_positions: int
    blockers: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.blockers

    @property
    def per_trade_limit(self) -> float:
        return _dollars(self.sleeve_capital, self.max_risk_per_trade_pct)

    @property
    def aggregate_open_risk_limit(self) -> float:
        return _dollars(self.sleeve_capital, self.max_open_risk_pct)

    @property
    def per_symbol_open_risk_limit(self) -> float:
        return _dollars(self.sleeve_capital, self.max_symbol_risk_pct)

    @property
    def daily_loss_halt(self) -> float:
        return _dollars(self.sleeve_capital, self.daily_loss_halt_pct)

    def snapshot(self) -> dict[str, Any]:
        return {
            "policy_version": self.version,
            "valid": self.valid,
            "blockers": list(self.blockers),
            "sleeve_capital": _round(self.sleeve_capital or 0.0),
            "max_risk_per_trade_pct": self.max_risk_per_trade_pct,
            "max_open_risk_pct": self.max_open_risk_pct,
            "max_symbol_risk_pct": self.max_symbol_risk_pct,
            "daily_loss_halt_pct": self.daily_loss_halt_pct,
            "max_open_positions": self.max_open_positions,
            "per_trade_limit": _round(self.per_trade_limit),
            "aggregate_open_risk_limit": _round(self.aggregate_open_risk_limit),
            "per_symbol_open_risk_limit": _round(self.per_symbol_open_risk_limit),
            "daily_loss_halt": _round(self.daily_loss_halt),
        }


@dataclass(frozen=True)
class RecoveryRiskDecision:
    quantity: int
    unit_risk: float
    total_risk: float
    blockers: tuple[str, ...]
    available_risk: float
    policy_version: str


def recovery_risk_policy(config: Any) -> RecoveryRiskPolicy:
    """Compile typed config into a versioned immutable recovery risk policy."""

    raw = {
        "sleeve_capital": getattr(config, "options_risk_sleeve_capital", None),
        "max_risk_per_trade_pct": getattr(config, "max_risk_per_trade_pct", None),
        "max_open_risk_pct": getattr(config, "max_open_risk_pct", None),
        "max_symbol_risk_pct": getattr(config, "max_symbol_risk_pct", None),
        "daily_loss_halt_pct": getattr(config, "daily_loss_halt_pct", None),
        "max_open_positions": getattr(config, "max_recovery_open_positions", None),
    }
    values = {key: _number(value) for key, value in raw.items() if key != "max_open_positions"}
    try:
        positions = int(raw["max_open_positions"])
    except (TypeError, ValueError):
        positions = 0
    blockers: list[str] = []
    sleeve = values["sleeve_capital"]
    if sleeve is None or sleeve <= 0:
        blockers.append("positive_recovery_sleeve_capital_required")
    percentages = (
        "max_risk_per_trade_pct", "max_open_risk_pct", "max_symbol_risk_pct", "daily_loss_halt_pct",
    )
    for key in percentages:
        value = values[key]
        if value is None or not 0.0 <= value <= 1.0:
            blockers.append(f"{key}_must_be_between_zero_and_one")
    trade = values["max_risk_per_trade_pct"]
    aggregate = values["max_open_risk_pct"]
    symbol = values["max_symbol_risk_pct"]
    if trade is not None and aggregate is not None and trade > aggregate:
        blockers.append("per_trade_risk_cannot_exceed_aggregate_open_risk")
    if symbol is not None and aggregate is not None and symbol > aggregate:
        blockers.append("per_symbol_risk_cannot_exceed_aggregate_open_risk")
    if trade is not None and symbol is not None and trade > symbol:
        blockers.append("per_trade_risk_cannot_exceed_per_symbol_risk")
    if trade is not None and aggregate is not None and positions > 0 and trade * positions < aggregate:
        blockers.append("open_position_limit_cannot_cover_aggregate_open_risk")
    if positions <= 0:
        blockers.append("positive_recovery_open_position_limit_required")
    canonical = {
        **{key: values[key] for key in sorted(values)},
        "max_open_positions": positions,
    }
    digest = sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    return RecoveryRiskPolicy(
        version=f"recovery-risk-v1:{digest}", sleeve_capital=sleeve,
        max_risk_per_trade_pct=values["max_risk_per_trade_pct"],
        max_open_risk_pct=values["max_open_risk_pct"],
        max_symbol_risk_pct=values["max_symbol_risk_pct"],
        daily_loss_halt_pct=values["daily_loss_halt_pct"], max_open_positions=positions,
        blockers=tuple(sorted(set(blockers))),
    )


def size_recovery_position(
    unit_risk: float,
    context: RecoveryRiskContext,
    policy: RecoveryRiskPolicy | None,
) -> RecoveryRiskDecision:
    """Size from a supplied immutable policy; invalid config fails closed."""

    effective = policy or missing_recovery_risk_policy()
    blockers = list(effective.blockers)
    if not isfinite(unit_risk) or unit_risk <= 0:
        blockers.append("positive_executable_unit_risk_required")
    if context.open_positions >= effective.max_open_positions:
        blockers.append("maximum_concurrent_positions_reached")
    if context.existing_event_family_position:
        blockers.append("second_ticket_for_symbol_family_event_prohibited")
    if effective.daily_loss_halt > 0 and context.daily_realized_unrealized_pnl <= -effective.daily_loss_halt:
        blockers.append("daily_loss_halt")
    if blockers:
        return RecoveryRiskDecision(0, max(_safe(unit_risk), 0.0), 0.0, tuple(sorted(set(blockers))), 0.0, effective.version)
    available = min(
        effective.per_trade_limit,
        effective.aggregate_open_risk_limit - max(context.open_risk, 0.0),
        effective.per_symbol_open_risk_limit - max(context.symbol_open_risk, 0.0),
    )
    quantity = max(0, floor(max(available, 0.0) / unit_risk))
    if quantity <= 0:
        blockers.append("available_recovery_risk_below_one_contract")
    total = round(unit_risk * quantity, 2)
    return RecoveryRiskDecision(
        quantity, round(unit_risk, 2), total, tuple(sorted(set(blockers))),
        round(max(available, 0.0), 2), effective.version,
    )


def missing_recovery_risk_policy() -> RecoveryRiskPolicy:
    """Represent absent authority as a durable, explicitly blocked policy.

    Recovery has no implicit dollar defaults.  A caller that has not compiled
    the typed configuration cannot size a ticket or stage a paper order.
    """

    return RecoveryRiskPolicy(
        version="recovery-risk-v1:missing",
        sleeve_capital=None,
        max_risk_per_trade_pct=None,
        max_open_risk_pct=None,
        max_symbol_risk_pct=None,
        daily_loss_halt_pct=None,
        max_open_positions=0,
        blockers=("recovery_risk_policy_required",),
    )


def _number(value: Any) -> float | None:
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and isfinite(number) else None


def _safe(value: float) -> float:
    return value if isfinite(value) else 0.0


def _dollars(capital: float | None, percentage: float | None) -> float:
    if capital is None or percentage is None or capital <= 0 or percentage < 0:
        return 0.0
    return capital * percentage


def _round(value: float) -> float:
    return round(value + 1e-9, 2)
