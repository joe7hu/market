"""Lazy broker facade for the PostgreSQL runtime and legacy tests."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_MODULES = (
    "constants", "types", "coerce", "ibkr", "moomoo", "policy",
    "recommendation_decisions", "persistence", "read_models",
    "recommendations", "service",
)


def __getattr__(name: str) -> Any:
    for module_name in _MODULES:
        module = import_module(f"investment_panel.core.brokers.{module_name}")
        if hasattr(module, name):
            value = getattr(module, name)
            globals()[name] = value
            return value
    raise AttributeError(name)


__all__: list[str] = []
