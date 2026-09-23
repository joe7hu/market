"""A missing prerequisite is not a favorable economic comparison with cash."""

import pytest
from pydantic import ValidationError

from investment_panel.domain.decision.resolution import (
    AuthorizationMode, DecisionResolutionV2, ResolutionAction, ResolutionEligibility,
    build_decision_resolution, next_action_for,
)


def _build(**overrides):
    return build_decision_resolution(**{
        "action": "BUY", "decision_revision": "communication-test", "policy_version": "test-policy",
        "provenance": {}, "data_quality": "COMPLETE", "entry": {"price": 100}, "size": 1,
        "invalidation": "Price below 95", "exit": {"target": 110}, "ttl": "1 session",
        "portfolio_context": {"nav": 10000}, **overrides,
    })


@pytest.mark.parametrize("blocker, expected", [
    ("alpha_strategy_revision_missing", "No qualified stock signal yet."),
    ("forecast_missing", "No validated return forecast."),
    ("trade_plan_missing", "No executable trade plan."),
    ("current_price", "Confirmed price unavailable."),
])
def test_failed_prerequisite_is_not_presented_as_cash_outperformance(blocker, expected):
    result = _build(blocked=True, blockers=[blocker], rationale=f"Cash is selected because the current trade plan is unavailable: {blocker}.")
    assert result.rationale == expected
    assert result.eligibility is ResolutionEligibility.BLOCKED
    assert result.action is ResolutionAction.NO_TRADE
    assert result.authorization_mode is AuthorizationMode.NONE
    assert result.size is None
    assert result.primary_blocker == blocker


def test_actionable_signal_has_no_phantom_unrecorded_blocker():
    result = _build()
    assert result.is_actionable
    assert result.primary_blocker is None
    assert "unrecorded" not in result.next_action
    assert "entry" in result.next_action
    assert "exit" in result.next_action
    assert "unrecorded" not in next_action_for(None)


def test_pending_incomplete_plan_does_not_instruct_an_order():
    result = _build(entry=None)
    assert result.eligibility is ResolutionEligibility.PENDING
    assert result.next_action == "No order is authorized by this decision."


def test_specific_risk_explanations_and_actual_cash_decisions_are_preserved():
    text = "Maximum loss exceeds the portfolio limit."
    assert _build(blocked=True, blockers=["risk_policy_blocked"], rationale=text).rationale == text
    text = "Cash offers the better after-cost return."
    assert _build(action="NO_TRADE", rationale=text).rationale == text


def test_qualification_message_does_not_call_unrun_controls_failed():
    text = next_action_for("alpha_strategy_revision_missing")
    assert "independent" in text and "repeated-control" in text
    assert "failed strategy gates" not in text


def test_presentation_change_cannot_grant_paper_or_live_authority():
    blocked = _build(blocked=True, blockers=["alpha_strategy_revision_missing"])
    for mode in ("PAPER", "LIVE"):
        with pytest.raises(ValidationError):
            DecisionResolutionV2.model_validate({**blocked.model_dump(), "authorization_mode": mode})
