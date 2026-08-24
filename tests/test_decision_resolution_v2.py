from __future__ import annotations

from datetime import UTC, datetime

import pytest

from investment_panel.core.decision import (
    DecisionResolutionV2,
    capital_action_from_resolution,
    build_decision_resolution,
)
from investment_panel.core.risk_policy import AssignmentPolicy, coerce_assignment_policy


NOW = datetime(2026, 8, 23, 15, tzinfo=UTC)


def test_blocked_resolution_has_one_safe_primary_blocker() -> None:
    resolution = build_decision_resolution(
        action="BUY",
        decision_revision="decision-1",
        policy_version="risk-policy.v2:test",
        provenance={"as_of": NOW},
        ticker="QQQ",
        blockers=["target_range", "paper_assignment_permission_required"],
        blocked=True,
    )

    assert resolution.action == "NO_TRADE"
    assert resolution.primary_blocker == "paper_assignment_permission_required"
    assert resolution.blockers == ["paper_assignment_permission_required"]
    assert capital_action_from_resolution(resolution).action == "AVOID"


def test_blocked_and_actionable_invariants_are_fail_closed() -> None:
    with pytest.raises(ValueError, match="cannot contain an order action"):
        DecisionResolutionV2(
            action="BUY",
            eligibility="BLOCKED",
            primary_blocker="missing_fact",
            blockers=["missing_fact"],
            decision_revision="decision-1",
        )

    with pytest.raises(ValueError, match="trade-plan fields"):
        DecisionResolutionV2(
            action="BUY",
            eligibility="ACTIONABLE",
            data_quality="FRESH",
            decision_revision="decision-1",
        )


def test_actionable_resolution_contains_the_complete_point_in_time_plan() -> None:
    resolution = build_decision_resolution(
        action="BUY",
        decision_revision="decision-1",
        policy_version="risk-policy.v2:test",
        provenance={"as_of": NOW, "available_at": NOW},
        ticker="QQQ",
        entry={"limit_price": 1.0},
        size=2,
        invalidation={"statement": "thesis breaks"},
        exit={"profit_price": 2.0},
        ttl=NOW,
        portfolio_context={"status": "complete", "nav": 10000},
        data_quality="FRESH",
    )

    assert resolution.is_actionable
    assert resolution.authorization_mode == "ADVISORY"


def test_assignment_policy_requires_all_paper_csp_gates() -> None:
    legacy_bool = coerce_assignment_policy(True)
    assert "fresh_postgres_account_facts_required" in legacy_bool.blockers()

    policy = AssignmentPolicy(
        paper_assignment_allowed=True,
        thesis_direction="neutral_bullish",
        thesis_as_of=NOW,
        account_as_of=NOW,
        account_source="postgresql",
        cash_balance=25_000,
        buying_power=25_000,
        required_cash=10_000,
        symbol_limit=25_000,
        aggregate_limit=75_000,
        evaluated_at=NOW,
    )
    assert policy.blockers(as_of=NOW) == ()
