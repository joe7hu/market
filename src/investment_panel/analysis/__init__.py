"""Deterministic market analysis modules with a production-safe lazy facade."""

from __future__ import annotations

from typing import Any

__all__ = ["run_all_analyses"]


def __getattr__(name: str) -> Any:
    if name == "run_all_analyses":
        from investment_panel.analysis.run import run_all_analyses

        return run_all_analyses
    raise AttributeError(name)
