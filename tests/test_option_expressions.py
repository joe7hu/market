from __future__ import annotations

import pytest

from investment_panel.analysis.option_expressions import (
    DebitSpreadInputs,
    LongOptionInputs,
    evaluate_call_debit_spread,
    evaluate_long_option,
)


def test_long_call_empirical_expectancy_uses_ask_and_round_trip_cost() -> None:
    result = evaluate_long_option(
        LongOptionInputs(
            option_type="call",
            spot=100,
            strike=105,
            ask=3,
            bid=2.8,
            multiplier=100,
            historical_horizon_returns=(-0.10, 0.0, 0.10, 0.20),
        )
    )

    assert result is not None
    assert result.entry_cost == 300
    assert result.max_loss == 300
    assert result.scenario_count == 4
    assert result.expected_value == 180
    assert result.probability_profit == 0.5
    assert result.risk_adjusted_expectancy == pytest.approx(result.expected_value / result.max_loss)


def test_long_put_targets_are_unattainable_when_stock_floor_blocks_them() -> None:
    result = evaluate_long_option(
        LongOptionInputs(
            option_type="put",
            spot=100,
            strike=20,
            ask=5,
            bid=4.8,
            multiplier=100,
            historical_horizon_returns=(-0.10, 0.0, 0.10),
        )
    )

    assert result is not None
    assert result.required_5x_price is None
    assert result.target_reasons["5x"] == "target_not_attainable"
    assert result.required_10x_price is None


def test_call_debit_spread_payoff_is_bounded() -> None:
    result = evaluate_call_debit_spread(
        DebitSpreadInputs(
            spot=100,
            long_strike=100,
            short_strike=110,
            long_ask=5,
            short_bid=2,
            multiplier=100,
            historical_horizon_returns=(-0.20, 0.0, 0.05, 0.10, 0.20),
        )
    )

    assert result is not None
    assert result.entry_cost == 300
    assert result.max_loss == 300
    assert result.max_profit == 700
    assert result.break_even == 103
    assert result.expected_value == 200
    assert result.probability_profit == 0.6


def test_call_debit_spread_rejects_crossed_or_non_debit_structure() -> None:
    assert evaluate_call_debit_spread(
        DebitSpreadInputs(
            spot=100,
            long_strike=110,
            short_strike=100,
            long_ask=2,
            short_bid=5,
            historical_horizon_returns=(0.1,),
        )
    ) is None
