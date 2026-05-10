"""Run deterministic analyses over stored Market data."""

from __future__ import annotations

from datetime import date
from typing import Any

from investment_panel.analysis.correlation import store_correlation_runs
from investment_panel.analysis.liquidity import store_liquidity_metrics
from investment_panel.analysis.sepa import store_sepa_analyses
from investment_panel.analysis.valuation import store_valuation_models
from investment_panel.core.config import AppConfig
from investment_panel.core.db import query_rows


def run_all_analyses(con: Any, config: AppConfig) -> dict[str, int | str]:
    if not config.analysis.enabled:
        return {"status": "disabled", "as_of": date.today().isoformat()}
    symbols = [row["symbol"] for row in query_rows(con, "SELECT symbol FROM instruments ORDER BY symbol")]
    return {
        "status": "ok",
        "as_of": date.today().isoformat(),
        "sepa_rows": store_sepa_analyses(con, symbols),
        "liquidity_rows": store_liquidity_metrics(con, symbols),
        "correlation_runs": store_correlation_runs(
            con,
            symbols,
            lookback_days=config.analysis.correlation_lookback_days,
            max_peers=config.analysis.max_correlation_peers,
        ),
        "valuation_rows": store_valuation_models(con, symbols),
    }
