from __future__ import annotations

import pytest

from app.response_contracts import TodayTradePlanSummaryResponse
from investment_panel.core.decision import ExpressionKind


@pytest.mark.parametrize("kind", list(ExpressionKind))
def test_today_trade_plan_expression_kind_has_exact_golden_backend_payload(kind: ExpressionKind) -> None:
    payload = TodayTradePlanSummaryResponse(
        contract_version="trade-plan.v1",
        trade_plan_id="plan-1",
        ticker="AAA",
        opportunity_episode_id="episode-1",
        decision_revision="ticker-decision.v1:1",
        policy_version="risk-policy.v2",
        selected_expression_kind=kind,
        selected_expression_identity=f"{kind.value}:AAA:1",
        action="NO_TRADE",
        eligibility="BLOCKED",
        authorization_mode="NONE",
        data_quality="INCOMPLETE",
        next_action="Refresh the decision.",
    )

    assert payload.model_dump(mode="json")["selected_expression_kind"] == kind.value
