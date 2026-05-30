"""Registry for deterministic Market analysis steps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from investment_panel.analysis.correlation import store_correlation_runs
from investment_panel.analysis.earnings_setup import store_earnings_setups
from investment_panel.analysis.liquidity import store_liquidity_metrics
from investment_panel.analysis.options_payoff import store_options_payoff_scenarios
from investment_panel.analysis.sepa import store_sepa_analyses
from investment_panel.analysis.valuation import store_valuation_models
from investment_panel.core.config import AppConfig


AnalysisRunner = Callable[[Any, list[str], AppConfig], int]


@dataclass(frozen=True)
class AnalysisStep:
    key: str
    run: AnalysisRunner


ANALYSIS_STEPS: tuple[AnalysisStep, ...] = (
    AnalysisStep("sepa_rows", lambda con, symbols, _config: store_sepa_analyses(con, symbols)),
    AnalysisStep("liquidity_rows", lambda con, symbols, _config: store_liquidity_metrics(con, symbols)),
    AnalysisStep(
        "correlation_runs",
        lambda con, symbols, config: store_correlation_runs(
            con,
            symbols,
            lookback_days=config.analysis.correlation_lookback_days,
            max_peers=config.analysis.max_correlation_peers,
        ),
    ),
    AnalysisStep("earnings_setups", lambda con, symbols, _config: store_earnings_setups(con, symbols)),
    AnalysisStep("options_payoff_scenarios", lambda con, symbols, _config: store_options_payoff_scenarios(con, symbols)),
    AnalysisStep("valuation_rows", lambda con, symbols, _config: store_valuation_models(con, symbols)),
)


def run_analysis_steps(con: Any, symbols: list[str], config: AppConfig) -> dict[str, int]:
    return {step.key: step.run(con, symbols, config) for step in ANALYSIS_STEPS}
