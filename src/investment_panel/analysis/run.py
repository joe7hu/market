"""Run deterministic analyses over stored Market data."""

from __future__ import annotations

from datetime import date
from typing import Any

from investment_panel.analysis.registry import run_analysis_steps
from investment_panel.core.config import AppConfig
from investment_panel.core.db import query_rows
from investment_panel.core.portfolio import ensure_portfolio_instruments


def run_all_analyses(con: Any, config: AppConfig) -> dict[str, int | str]:
    if not config.analysis.enabled:
        return {"status": "disabled", "as_of": date.today().isoformat()}
    ensure_portfolio_instruments(con)
    symbols = [row["symbol"] for row in query_rows(con, "SELECT symbol FROM instruments ORDER BY symbol")]
    return {
        "status": "ok",
        "as_of": date.today().isoformat(),
        **run_analysis_steps(con, symbols, config),
    }
