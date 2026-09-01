from __future__ import annotations

from investment_panel.core.panel.contracts import TICKER_INITIAL_TABLES


def test_ticker_initial_contract_keeps_deep_payoff_rows_out_of_the_initial_load() -> None:
    required = {
        "symbol_decision_snapshot",
        "decision_queue",
        "theses",
        "thesis_monitor",
        "catalysts",
        "earnings",
        "options_ticker_signals",
    }

    assert required.issubset(TICKER_INITIAL_TABLES)
    assert "options_payoff_scenarios" not in TICKER_INITIAL_TABLES
