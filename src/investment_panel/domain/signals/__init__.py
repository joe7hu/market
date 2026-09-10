"""Reusable, advisory signal interpretations over the factor catalog."""

from investment_panel.domain.signals.catalog import (
    MOMENTUM_DIRECTION,
    MOMENTUM_VOLATILITY,
    SIGNAL_CATALOG,
    SignalDefinition,
    SignalResult,
    evaluate_signals,
)

__all__ = [
    "MOMENTUM_DIRECTION", "MOMENTUM_VOLATILITY", "SIGNAL_CATALOG", "SignalDefinition", "SignalResult",
    "evaluate_signals",
]
