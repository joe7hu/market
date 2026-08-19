import pytest

from investment_panel.database.options_distribution_shift import (
    constant_tenor_curve,
    surface_shift_payload,
    wasserstein_1_equal_mass,
)


def test_constant_tenor_curve_interpolates_but_never_extrapolates() -> None:
    curve = constant_tenor_curve([{"dte": 10, "value": 0.20}, {"dte": 20, "value": 0.30}], tenors=(7, 14, 20, 30))
    assert curve == {7: None, 14: 0.24, 20: 0.30, 30: None}


def test_w1_is_zero_for_identical_and_symmetric() -> None:
    assert wasserstein_1_equal_mass([0.2, 0.3], [0.2, 0.3]) == 0
    assert wasserstein_1_equal_mass([0.1, 0.4], [0.2, 0.3]) == wasserstein_1_equal_mass([0.2, 0.3], [0.1, 0.4])


def test_surface_shift_is_explanation_only_and_does_not_invent_tail_mass() -> None:
    current = [
        {"dte": dte, "option_type": option_type, "atm_iv": 0.20 + dte / 1000,
         "delta_25_iv": 0.22 if option_type == "call" else 0.28,
         "skew_25": -0.02 if option_type == "call" else 0.04, "term_slope": 0.01}
        for dte in (7, 14, 30, 60, 90) for option_type in ("call", "put")
    ]
    previous = [
        {"dte": dte, "option_type": option_type, "atm_iv": 0.19 + dte / 1000,
         "delta_25_iv": 0.20 if option_type == "call" else 0.23,
         "skew_25": -0.01 if option_type == "call" else 0.02, "term_slope": 0.00}
        for dte in (7, 14, 30, 60, 90) for option_type in ("call", "put")
    ]
    result = surface_shift_payload(current, previous)
    assert result["evidence_state"] == "ready"
    assert result["w1_shift"] == pytest.approx(0.01)
    assert result["skew_shift"] == pytest.approx(0.03)
    assert result["skew_method"] == "put_minus_call_25d_skew_risk_reversal"
    assert result["term_shift"] == pytest.approx(0.01)
    assert result["tail_mass_change"] is None
    assert result["explanation_only"] is True
    assert result["strategy_effect"] is False
    assert "risk_neutral_tail_density_not_materialized" in result["blockers"]


def test_surface_shift_fails_closed_when_tenors_are_not_bracketed() -> None:
    result = surface_shift_payload([{"dte": 30, "atm_iv": 0.2}], [{"dte": 30, "atm_iv": 0.2}])
    assert result["evidence_state"] == "insufficient_surface_evidence"
    assert result["w1_shift"] is None


def test_skew_shift_compares_only_matched_constant_tenors() -> None:
    current = [
        {"dte": 7, "option_type": "call", "atm_iv": 0.20, "delta_25_iv": 0.22, "skew_25": -0.02},
        {"dte": 7, "option_type": "put", "atm_iv": 0.20, "delta_25_iv": 0.28, "skew_25": 0.04},
        {"dte": 30, "option_type": "call", "atm_iv": 0.25, "delta_25_iv": 0.26, "skew_25": -0.04},
        {"dte": 30, "option_type": "put", "atm_iv": 0.25, "delta_25_iv": 0.35, "skew_25": 0.05},
    ]
    previous = [
        {"dte": 7, "option_type": "call", "atm_iv": 0.19, "delta_25_iv": 0.20, "skew_25": -0.01},
        {"dte": 7, "option_type": "put", "atm_iv": 0.19, "delta_25_iv": 0.23, "skew_25": 0.02},
        {"dte": 60, "option_type": "call", "atm_iv": 0.26, "delta_25_iv": 0.30, "skew_25": 0.20},
        {"dte": 60, "option_type": "put", "atm_iv": 0.26, "delta_25_iv": 0.40, "skew_25": 0.30},
    ]
    result = surface_shift_payload(current, previous, tenors=(7,))
    assert result["skew_shift"] == pytest.approx(0.03)
