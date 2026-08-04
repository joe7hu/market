"""Typed recovery-policy settings kept separate from generic application config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class OptionsDecisionSystemConfig:
    mode: str = "shadow"
    options_paper_actions_enabled: bool = False
    recovery_paper_actions_enabled: bool = False
    options_risk_sleeve_capital: float | None = None
    max_risk_per_trade_pct: float = 0.02
    max_open_risk_pct: float = 0.10
    max_symbol_risk_pct: float = 0.04
    daily_loss_halt_pct: float = 0.04
    max_recovery_open_positions: int = 5
    strategy_auto_promotion_enabled: bool = False
    event_agent_debounce_minutes: int = 30
    event_agent_max_batches_per_symbol_per_day: int = 2
    event_agent_max_tasks_per_batch: int = 12


def options_decision_system_config(
    raw: dict[str, Any],
    mode_parser: Callable[[Any], str],
) -> OptionsDecisionSystemConfig:
    """Normalize recovery settings once for jobs and web-runtime readers."""

    sleeve = raw.get("options_risk_sleeve_capital")
    return OptionsDecisionSystemConfig(
        mode=mode_parser(raw),
        options_paper_actions_enabled=bool(raw.get("options_paper_actions_enabled", False)),
        recovery_paper_actions_enabled=bool(raw.get("recovery_paper_actions_enabled", False)),
        options_risk_sleeve_capital=_float_or_none(sleeve),
        max_risk_per_trade_pct=_float_or_nan(raw.get("max_risk_per_trade_pct", 0.02)),
        max_open_risk_pct=_float_or_nan(raw.get("max_open_risk_pct", 0.10)),
        max_symbol_risk_pct=_float_or_nan(raw.get("max_symbol_risk_pct", 0.04)),
        daily_loss_halt_pct=_float_or_nan(raw.get("daily_loss_halt_pct", 0.04)),
        max_recovery_open_positions=_int_or_zero(raw.get("max_recovery_open_positions", 5)),
        strategy_auto_promotion_enabled=bool(raw.get("strategy_auto_promotion_enabled", False)),
        event_agent_debounce_minutes=int(raw.get("event_agent_debounce_minutes", 30)),
        event_agent_max_batches_per_symbol_per_day=int(raw.get("event_agent_max_batches_per_symbol_per_day", 2)),
        event_agent_max_tasks_per_batch=int(raw.get("event_agent_max_tasks_per_batch", 12)),
    )


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    return _float_or_nan(value)


def _float_or_nan(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
