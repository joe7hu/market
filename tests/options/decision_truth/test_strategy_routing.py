from __future__ import annotations

from datetime import UTC, datetime

import pytest

from investment_panel.analysis.strategy_routing import (
    PROHIBITED_STRUCTURES,
    ROUTE_VERSION,
    route_promotion_gate,
    route_strategy,
)
from investment_panel.core.risk_policy import PortfolioAssignmentPolicy


NOW = datetime(2026, 8, 19, 14, tzinfo=UTC)


def _feature(state: str, *, er: float = 0.5, rs: float = 0.1, vol: str = "normal") -> dict:
    return {
        "trend_state": state,
        "trend_confidence": 0.8,
        "volatility_state": vol,
        "kaufman_er_20d": er,
        "relative_strength_20d": rs,
        "data_quality_status": "complete",
        "reason_codes": [],
        "feature_version": "daily-trend-v1",
    }


def _market(state: str = "trend_up") -> dict:
    return {
        "trend_state": state,
        "quality_status": "complete",
        "feature_version": "daily-trend-v1",
    }


@pytest.mark.parametrize(
    ("trend", "market", "iv", "rv", "percentile", "expected"),
    [
        ("trend_up", "trend_up", 0.20, 0.22, 0.30, "long_call"),
        ("trend_up", "trend_up", 0.45, 0.20, 0.80, "call_debit_spread"),
        ("trend_down", "trend_down", 0.20, 0.22, 0.30, "long_put"),
        ("trend_down", "trend_down", 0.45, 0.20, 0.80, "put_debit_spread"),
        ("range", "trend_up", 0.20, 0.20, 0.50, "NO_TRADE"),
        ("transition", "trend_up", 0.20, 0.20, 0.50, "NO_TRADE"),
        ("trend_up", "trend_down", 0.20, 0.20, 0.50, "NO_TRADE"),
    ],
)
def test_route_matrix(trend: str, market: str, iv: float, rv: float, percentile: float, expected: str) -> None:
    rs = -0.1 if trend == "trend_down" else 0.1
    result = route_strategy(
        _feature(trend, rs=rs), _market(market), option_iv=iv, realized_vol=rv,
        iv_percentile=percentile, as_of=NOW,
    )
    assert result["route_version"] == ROUTE_VERSION
    assert result["selected_structure"] == expected
    assert result["paper_quantity_authorized"] is False


def test_event_vol_is_research_only_and_uses_full_straddle_semantics() -> None:
    result = route_strategy(
        _feature("range", er=0.1),
        _market("trend_up"),
        option_iv=0.25,
        realized_vol=0.25,
        iv_percentile=0.5,
        event_summary={
            "evidence_state": "ready",
            "sample_size": 25,
            "actual_move_median": 0.07,
            "implied_move": 0.05,
            "complete_same_expiry_atm_legs": True,
        },
        as_of=NOW,
    )
    assert result["selected_structure"] == "EVENT_VOL_RESEARCH"
    assert result["paper_quantity_authorized"] is False


def test_event_vol_requires_twenty_samples_and_two_complete_legs() -> None:
    for sample_size, complete in [(19, True), (25, False)]:
        result = route_strategy(
            _feature("range", er=0.1), _market("trend_up"), option_iv=0.25,
            realized_vol=0.25, iv_percentile=0.5,
            event_summary={
                "evidence_state": "ready", "sample_size": sample_size,
                "actual_move_median": 0.07, "implied_move": 0.05,
                "complete_same_expiry_atm_legs": complete,
            },
        )
        assert result["selected_structure"] == "NO_TRADE"


def test_unlimited_risk_structures_are_always_rejected() -> None:
    result = route_strategy(
        _feature("trend_up"), _market(), option_iv=0.2, realized_vol=0.25, iv_percentile=0.2
    )
    rejected = {row["structure"]: row["reason"] for row in result["rejected_structures"]}
    for structure in PROHIBITED_STRUCTURES:
        assert rejected[structure] == "unlimited_or_unbounded_risk_prohibited"


def test_csp_requires_assignment_and_portfolio_consent() -> None:
    without_consent = route_strategy(
        _feature("trend_up"), _market(), option_iv=0.5, realized_vol=0.2,
        iv_percentile=0.8,
        assignment_policy=PortfolioAssignmentPolicy(
            thesis_direction="bullish", thesis_as_of=NOW, evaluated_at=NOW,
        ),
        as_of=NOW,
    )
    with_consent = route_strategy(
        _feature("trend_up"), _market(), option_iv=0.5, realized_vol=0.2,
        iv_percentile=0.8,
        assignment_policy=PortfolioAssignmentPolicy(
            paper_assignment_allowed=True,
            thesis_direction="bullish",
            thesis_as_of=NOW,
            thesis_preferred_structures=("cash_secured_put",),
            account_as_of=NOW,
            account_source="postgresql",
            cash_balance=100_000,
            buying_power=100_000,
            required_cash=15_000,
            symbol_limit=25_000,
            aggregate_limit=75_000,
            evaluated_at=NOW,
        ),
        as_of=NOW,
    )
    assert without_consent["selected_structure"] == "cash_secured_put"
    assert "paper_assignment_permission_required" in without_consent["route_blockers"]
    assert with_consent["selected_structure"] == "cash_secured_put"
    assert with_consent["route_blockers"] == []


def test_route_promotion_requires_exact_cohort_calibration_forward_and_human_approval() -> None:
    blocked = route_promotion_gate({})
    assert blocked["eligible"] is False
    assert blocked["automatic_promotion"] is False
    passed = route_promotion_gate({
        "mature_exact_structure_regime_outcomes": 30,
        "cost_adjusted_expectancy_lower_bound": 0.01,
        "brier_score": 0.25,
        "forward_session_passed": True,
        "human_approved": True,
    })
    assert passed == {
        "eligible": True,
        "blockers": [],
        "automatic_promotion": False,
        "route_version": ROUTE_VERSION,
    }
