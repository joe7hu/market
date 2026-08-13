from __future__ import annotations

from investment_panel.core.panel.contracts import TICKER_INITIAL_TABLES


def test_ticker_initial_contract_includes_the_canonical_decision_thesis_options_and_catalyst_rows() -> None:
    required = {
        "symbol_decision_snapshot",
        "decision_queue",
        "theses",
        "thesis_monitor",
        "catalysts",
        "earnings",
        "options_ticker_signals",
        "options_payoff_scenarios",
    }

    assert required.issubset(TICKER_INITIAL_TABLES)
