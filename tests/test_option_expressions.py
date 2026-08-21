from __future__ import annotations

from datetime import date

import pytest

from investment_panel.analysis.option_expressions import (
    DebitSpreadInputs,
    LongOptionInputs,
    evaluate_call_debit_spread,
    evaluate_long_option,
    evaluate_put_debit_spread,
)
from investment_panel.database.options_expressions import (
    contiguous_confirmed_closes, history_bar_limits, horizon_returns,
    compatible_contract_terms,
)
from investment_panel.database.options_history_v3_candidates import trading_session_horizon


def test_empirical_history_rejects_a_missing_trading_session() -> None:
    rows = [
        {"trading_date": date(2026, 8, 17), "close": 100},
        {"trading_date": date(2026, 8, 19), "close": 102},
    ]
    assert contiguous_confirmed_closes(rows) == []


def test_vertical_spread_requires_matching_contract_terms() -> None:
    standard = {
        "multiplier": 100, "style": "american", "settlement": "physical",
        "deliverable_key": "qqq-standard", "standard_contract_verified": True,
    }
    assert compatible_contract_terms(standard, dict(standard))
    assert not compatible_contract_terms(standard, {**standard, "multiplier": 10})
    assert not compatible_contract_terms(standard, {**standard, "settlement": "cash"})
    assert not compatible_contract_terms(standard, {**standard, "deliverable_key": None})


def test_calendar_dte_is_converted_to_trading_sessions_without_a_sixty_day_cap() -> None:
    assert trading_session_horizon(40) == 28
    assert trading_session_horizon(90) == 62
    prices = [float(value) for value in range(1, 80)]
    assert len(horizon_returns(prices, 90)) == len(prices) - 62
    assert history_bar_limits([{"instrument_id": 1, "dte": 180}]) == {1: 144}


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
    assert result.conservative_expected_value <= result.optimistic_expected_value
    assert result.lower_95_expected_value <= result.expected_value


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
    assert result.required_2x_price == 106
    assert result.required_5x_price is None
    assert result.target_reasons["5x"] == "target_not_attainable"


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


def test_put_debit_spread_has_bounded_risk_and_bearish_payoff() -> None:
    result = evaluate_put_debit_spread(
        DebitSpreadInputs(
            spot=100,
            long_strike=100,
            short_strike=95,
            long_ask=4,
            short_bid=2,
            historical_horizon_returns=(-0.10, -0.05, 0.05),
            option_type="put",
        )
    )
    assert result is not None
    assert result.entry_cost == 200
    assert result.max_loss == 200
    assert result.max_profit == 300
    assert result.break_even == 98


def test_lower_bound_uses_effective_sample_size_for_overlapping_returns() -> None:
    independent = evaluate_long_option(LongOptionInputs(
        option_type="call", spot=100, strike=100, ask=5, bid=4.5,
        historical_horizon_returns=(-0.2, -0.1, 0.0, 0.1, 0.2, 0.3),
        return_stride=1,
    ))
    overlapping = evaluate_long_option(LongOptionInputs(
        option_type="call", spot=100, strike=100, ask=5, bid=4.5,
        historical_horizon_returns=(-0.2, -0.1, 0.0, 0.1, 0.2, 0.3),
        return_stride=3,
    ))
    assert independent is not None and overlapping is not None
    assert overlapping.lower_95_expected_value < independent.lower_95_expected_value
