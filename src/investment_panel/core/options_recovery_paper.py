"""Fail-closed paper sleeve sizing for the forward recovery program."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import floor, isfinite

from investment_panel.core.options_event_tape import trading_sessions_between


SLEEVE_CAPITAL = 25_000.0
MAX_RISK_PER_TRADE = 500.0
MAX_OPEN_RISK = 2_500.0
MAX_SYMBOL_RISK = 1_000.0
DAILY_LOSS_HALT = 1_000.0
MAX_CONCURRENT_POSITIONS = 5
QUALIFIED_EVENT_SESSIONS = 5


@dataclass(frozen=True)
class RecoveryRiskContext:
    open_risk: float = 0.0
    symbol_open_risk: float = 0.0
    open_positions: int = 0
    daily_realized_unrealized_pnl: float = 0.0
    existing_event_family_position: bool = False


@dataclass(frozen=True)
class RecoveryRiskDecision:
    quantity: int
    unit_risk: float
    total_risk: float
    blockers: tuple[str, ...]
    available_risk: float


def qualified_for_paper(event_started_at: datetime, now: datetime) -> bool:
    return trading_sessions_between(event_started_at, now) >= QUALIFIED_EVENT_SESSIONS


def size_recovery_position(unit_risk: float, context: RecoveryRiskContext) -> RecoveryRiskDecision:
    """Size only from the dedicated $25k sleeve; broker state is irrelevant."""

    blockers: list[str] = []
    if not isfinite(unit_risk) or unit_risk <= 0:
        blockers.append("positive_executable_unit_risk_required")
    if context.open_positions >= MAX_CONCURRENT_POSITIONS:
        blockers.append("maximum_concurrent_positions_reached")
    if context.existing_event_family_position:
        blockers.append("second_ticket_for_symbol_family_event_prohibited")
    if context.daily_realized_unrealized_pnl <= -DAILY_LOSS_HALT:
        blockers.append("daily_loss_halt")
    if blockers:
        return RecoveryRiskDecision(0, max(unit_risk, 0.0), 0.0, tuple(sorted(blockers)), 0.0)
    available = min(
        MAX_RISK_PER_TRADE,
        MAX_OPEN_RISK - max(context.open_risk, 0.0),
        MAX_SYMBOL_RISK - max(context.symbol_open_risk, 0.0),
    )
    quantity = max(0, floor(max(available, 0.0) / unit_risk))
    if quantity <= 0:
        blockers.append("available_recovery_risk_below_one_contract")
    total = round(unit_risk * quantity, 2)
    return RecoveryRiskDecision(quantity, round(unit_risk, 2), total, tuple(sorted(blockers)), round(max(available, 0.0), 2))
