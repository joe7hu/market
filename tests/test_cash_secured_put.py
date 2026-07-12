from __future__ import annotations

import math

from investment_panel.analysis.cash_secured_put import (
    CashSecuredPutInputs,
    evaluate_cash_secured_put,
)


def test_cash_secured_put_payoff_and_assignment_basis() -> None:
    result = evaluate_cash_secured_put(
        CashSecuredPutInputs(
            spot=100,
            strike=90,
            dte=45,
            bid=2.0,
            ask=2.2,
            delta=-0.22,
            multiplier=100,
            fee_per_contract=0.65,
            annualized_volatility=0.35,
        )
    )

    assert result is not None
    assert result.entry_credit == 199.35
    assert result.secured_cash == 8800.65
    assert result.max_profit == 199.35
    assert result.max_loss == 8800.65
    assert result.effective_assignment_price == 88.0065
    assert result.break_even == result.effective_assignment_price
    assert result.probability_assignment == 0.22
    assert result.probability_profit > result.probability_assignment
    assert result.annualized_return_on_collateral > result.return_on_collateral


def test_cash_secured_put_stress_losses_are_bounded_and_monotonic() -> None:
    result = evaluate_cash_secured_put(
        CashSecuredPutInputs(
            spot=100,
            strike=95,
            dte=30,
            bid=2.5,
            ask=2.8,
            delta=-0.25,
            multiplier=100,
            annualized_volatility=0.40,
        )
    )

    assert result is not None
    losses = [result.stress_losses[key] for key in ("down_10", "down_20", "down_30", "zero")]
    assert losses == sorted(losses)
    assert losses[-1] == result.max_loss
    assert 0 <= result.tail_cvar <= result.max_loss


def test_cash_secured_put_rejects_incomplete_or_invalid_market() -> None:
    invalid = [
        CashSecuredPutInputs(spot=100, strike=90, dte=45, bid=0, ask=1, delta=-0.2),
        CashSecuredPutInputs(spot=100, strike=90, dte=45, bid=2, ask=1, delta=-0.2),
        CashSecuredPutInputs(spot=0, strike=90, dte=45, bid=2, ask=2.2, delta=-0.2),
        CashSecuredPutInputs(spot=100, strike=90, dte=0, bid=2, ask=2.2, delta=-0.2),
        CashSecuredPutInputs(spot=100, strike=90, dte=45, bid=2, ask=2.2, delta=math.nan),
    ]

    assert all(evaluate_cash_secured_put(inputs) is None for inputs in invalid)


def test_cash_secured_put_costs_reduce_return_and_raise_assignment_basis() -> None:
    no_fee = evaluate_cash_secured_put(
        CashSecuredPutInputs(spot=100, strike=90, dte=45, bid=2, ask=2.2, delta=-0.2, fee_per_contract=0)
    )
    fee = evaluate_cash_secured_put(
        CashSecuredPutInputs(spot=100, strike=90, dte=45, bid=2, ask=2.2, delta=-0.2, fee_per_contract=1)
    )

    assert no_fee is not None and fee is not None
    assert fee.max_profit < no_fee.max_profit
    assert fee.effective_assignment_price > no_fee.effective_assignment_price
    assert fee.return_on_collateral < no_fee.return_on_collateral
