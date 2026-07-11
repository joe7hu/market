"""Decision constants with lazy access to legacy DuckDB read models."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from investment_panel.core.decision.constants import (
    ARCO_STALE_DAYS, DAILY_ANALYSIS_SOURCES, DAILY_STALE_DAYS,
    FILING_STALE_DAYS, FRESHNESS_ORDER, INTRADAY_STALE_HOURS,
    MARKET_CLOSE, MARKET_OPEN, MARKET_TZ, PRIMARY_EVIDENCE_SOURCES,
    STATIC_SOURCES, SYMBOL_RE,
)

_MODULES = (
    "coerce", "calendar", "freshness", "grading", "readiness", "brief_coerce",
    "brief_options", "brief", "watchlist", "portfolio", "quotes", "builders",
    "persistence", "read_models", "service",
)


def __getattr__(name: str) -> Any:
    for module_name in _MODULES:
        module = import_module(f"investment_panel.core.decision.{module_name}")
        if hasattr(module, name):
            value = getattr(module, name)
            globals()[name] = value
            return value
    raise AttributeError(name)


__all__ = [
    "ARCO_STALE_DAYS", "DAILY_ANALYSIS_SOURCES", "DAILY_STALE_DAYS",
    "FILING_STALE_DAYS", "FRESHNESS_ORDER", "INTRADAY_STALE_HOURS",
    "MARKET_CLOSE", "MARKET_OPEN", "MARKET_TZ", "PRIMARY_EVIDENCE_SOURCES",
    "STATIC_SOURCES", "SYMBOL_RE",
]
