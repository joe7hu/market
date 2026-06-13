"""Unit tests for the deterministic EV scoring engine (Phase 1f)."""

from __future__ import annotations

import math

import pytest

from investment_panel.analysis import option_ev
from investment_panel.analysis.option_ev import (
    EVInputs,
    compute_ev,
    conviction_from_ev,
    ev_inverse_buy_under,
    ev_score,
    horizons_for,
    scenario_value,
)


def _leap_call(premium: float = 5.0, iv: float = 0.45) -> EVInputs:
    return EVInputs(option_type="call", spot=100.0, strike=130.0, dte=540, premium=premium, iv=iv, rv_60d=0.40)


def test_deep_itm_scenario_value_approx_intrinsic():
    # A deeply in-the-money call near expiry is essentially intrinsic value.
    inputs = EVInputs(option_type="call", spot=100.0, strike=10.0, dte=10, premium=90.0, iv=0.4)
    # Repriced at a flat scenario with ~0 remaining time -> intrinsic = spot - strike.
    value = scenario_value(inputs, underlying_multiple=1.0, horizon_days=10)
    assert value == pytest.approx(90.0, abs=0.5)


def test_ev_multiple_strictly_decreasing_in_premium():
    cheap = compute_ev(_leap_call(premium=4.0))
    rich = compute_ev(_leap_call(premium=8.0))
    assert cheap is not None and rich is not None
    assert cheap.ev_multiple > rich.ev_multiple
    # ev_value (payoff in $) is premium-independent, so doubling premium ~ halves multiple.
    assert cheap.ev_multiple == pytest.approx(rich.ev_multiple * 2, rel=0.02)


def test_theta_cost_positive_for_otm_call():
    # Price the option at its Black-Scholes fair value, then a no-move scenario after a
    # horizon must be worth less than premium -> positive theta -> finite ev_per_theta.
    from investment_panel.analysis.options_payoff import black_scholes

    spot, strike, iv, dte = 100.0, 130.0, 0.45, 540
    fair = black_scholes("call", spot, strike, dte / 365.0, option_ev.DEFAULT_RISK_FREE_RATE, iv)
    inputs = EVInputs(option_type="call", spot=spot, strike=strike, dte=dte, premium=fair, iv=iv, rv_60d=0.40)
    flat_later = scenario_value(inputs, 1.0, horizons_for(dte)[0])
    assert flat_later < fair  # decay with no move
    result = compute_ev(inputs)
    assert result is not None
    assert result.ev_per_theta is not None


def test_probabilities_ordered_and_bounded():
    result = compute_ev(_leap_call())
    assert result is not None
    assert 0.0 <= result.p_10x <= result.p_5x <= result.p_2x <= 1.0


def test_higher_tail_multiplier_raises_tail_probabilities():
    base = compute_ev(_leap_call())
    fat = compute_ev(EVInputs(option_type="call", spot=100.0, strike=130.0, dte=540, premium=5.0, iv=0.45, rv_60d=0.40, tail_multiplier=1.6))
    assert base is not None and fat is not None
    assert fat.p_10x >= base.p_10x


def test_buy_under_is_real_limit_price():
    inputs = _leap_call(premium=5.0)
    buy_under = ev_inverse_buy_under(inputs, target_ev=2.0)
    assert buy_under is not None and buy_under > 0
    # Paying the buy-under price gives EV multiple ~= the 2.0 floor.
    at_limit = compute_ev(EVInputs(**{**inputs.__dict__, "premium": buy_under}))
    assert at_limit is not None
    assert at_limit.ev_multiple == pytest.approx(2.0, rel=0.02)


def test_invalid_inputs_return_none():
    assert compute_ev(EVInputs(option_type="call", spot=0.0, strike=130.0, dte=540, premium=5.0, iv=0.45)) is None
    assert compute_ev(EVInputs(option_type="call", spot=100.0, strike=130.0, dte=540, premium=0.0, iv=0.45)) is None
    assert compute_ev(EVInputs(option_type="call", spot=100.0, strike=130.0, dte=0, premium=5.0, iv=0.45)) is None


def test_ev_score_haircut_and_bounds():
    assert ev_score(None, None) == 0.0
    high = ev_score(3.0, spread_pct=0.0)
    haircut = ev_score(3.0, spread_pct=0.25)
    assert 0.0 < haircut < high <= 100.0


def test_conviction_scales_with_probability_and_ev():
    low = conviction_from_ev(p_2x=0.1, ev_multiple=2.0)
    high = conviction_from_ev(p_2x=0.4, ev_multiple=2.0)
    assert high > low
    # EV below the floor discounts conviction.
    assert conviction_from_ev(p_2x=0.4, ev_multiple=1.0) < high
    assert 0.0 <= high <= 100.0


def test_horizons_within_contract_life():
    assert all(0 < h <= 200 for h in horizons_for(200))
    assert horizons_for(540) == sorted(set(horizons_for(540)))
