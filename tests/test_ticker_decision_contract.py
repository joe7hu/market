from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest

from investment_panel.core.decision.ticker import (
    CapitalActionType,
    ExpressionKind,
    build_ticker_decision,
)


AS_OF = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)


def test_conflicting_horizons_choose_an_owned_hold_action_and_share_one_thesis() -> None:
    decision = build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio": [{"symbol": "ACME", "quantity": 10, "nav": 100_000}],
            "decision_queue": [{
                "symbol": "ACME",
                "tactical_stance": "BEARISH",
                "fundamental_stance": "BULLISH",
                "entry_low": 95,
                "entry_high": 102,
                "invalidation_price": 90,
                "catalyst": "earnings",
            }],
        },
        as_of=AS_OF,
    )

    assert decision.capital_action.action is CapitalActionType.HOLD
    assert decision.capital_action.owned is True
    assert decision.tactical.invalidation == decision.fundamental.invalidation
    assert decision.tactical.horizon == "TACTICAL"
    assert decision.fundamental.horizon == "FUNDAMENTAL"
    assert sum(scenario.probability for scenario in decision.tactical.scenarios) == pytest.approx(1.0)
    assert decision.selected_expression is not None
    declarations = {item.name: item for item in decision.input_manifest.signal_declarations}
    assert declarations["company_financials"].source.startswith("SEC")
    assert declarations["participant_option_flow"].evidence_state == "HYPOTHESIS"


def test_missing_inputs_keep_directional_views_but_do_not_invent_quantity() -> None:
    decision = build_ticker_decision(
        "ACME",
        {"decision_queue": [{"symbol": "ACME", "stance": "BULLISH", "action": "BUY"}]},
        as_of=AS_OF,
    )

    assert decision.tactical.stance == "BULLISH"
    assert decision.capital_action.action is CapitalActionType.BUY
    assert decision.selected_expression is not None
    assert decision.selected_expression.quantity is None
    assert {request.field for request in decision.data_requests} >= {"current_price", "invalidation", "portfolio_nav"}
    assert any("update_broker_account" in request.collect_now for request in decision.data_requests)


def test_expression_sizing_uses_loss_budget_and_option_max_loss() -> None:
    decision = build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio_summary": [{"net_liquidation": 100_000, "available_at": "2026-08-22T13:55:00Z"}],
            "decision_queue": [{
                "symbol": "ACME",
                "stance": "BULLISH",
                "action": "BUY",
                "entry_low": 99,
                "entry_high": 101,
                "invalidation_price": 90,
                "conviction_tier": "STANDARD",
            }],
            "options_payoff_scenarios": [{
                "symbol": "ACME",
                "structure": "long_call",
                "max_loss": 250,
                "lower_confidence_expectancy": 0.20,
                "liquidity_score": 0.9,
                "spread_pct": 0.02,
                "fill_probability": 0.8,
                "expiration": "2026-10-16",
            }],
        },
        as_of=AS_OF,
    )

    assert decision.risk_policy.loss_budget == pytest.approx(1_000)
    assert decision.expressions[ExpressionKind.STOCK].quantity == 100
    assert decision.expressions[ExpressionKind.CALL].quantity == 4
    assert decision.expressions[ExpressionKind.STOCK].horizon == decision.expressions[ExpressionKind.CALL].horizon
    assert decision.expressions[ExpressionKind.STOCK].invalidation == decision.expressions[ExpressionKind.CALL].invalidation


def test_input_hash_changes_when_a_dependency_changes_and_future_rows_are_ignored() -> None:
    base = {
        "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
        "decision_queue": [{"symbol": "ACME", "stance": "NEUTRAL"}],
        "future_revision": [{"symbol": "ACME", "stance": "BEARISH", "available_at": "2026-08-23T13:55:00Z"}],
    }
    first = build_ticker_decision("ACME", base, as_of=AS_OF)
    second = build_ticker_decision("ACME", {**base, "decision_queue": [{"symbol": "ACME", "stance": "BULLISH"}]}, as_of=AS_OF)

    assert first.input_manifest.input_hash != second.input_manifest.input_hash
    assert "future_revision" not in first.input_manifest.inputs


def test_wait_for_price_keeps_the_directional_expression_and_stale_nav_is_actionable() -> None:
    decision = build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio_summary": [{"symbol": "ACME", "net_liquidation": 100_000, "available_at": "2026-08-22T12:00:00Z"}],
            "decision_queue": [{
                "symbol": "ACME", "tactical_stance": "BEARISH", "fundamental_stance": "BULLISH",
                "entry_low": 95, "entry_high": 98, "invalidation_price": 90,
            }],
        },
        as_of=AS_OF,
    )

    assert decision.capital_action.action is CapitalActionType.WAIT_FOR_PRICE
    assert decision.selected_expression is not None
    assert decision.selected_expression.kind is ExpressionKind.STOCK
    assert "portfolio_nav" in {request.field for request in decision.data_requests}
    assert any("update_broker_account" in request.why_it_matters for request in decision.data_requests)


def test_typed_decision_contract_rejects_soft_final_actions_and_blocker_text() -> None:
    decision = build_ticker_decision(
        "ACME",
        {"decision_queue": [{"symbol": "ACME", "stance": "NEUTRAL"}]},
        as_of=AS_OF,
    )
    rendered = json.dumps(decision.model_dump(mode="json")).lower()
    for phrase in ("review the risk", "resolve blockers", "needs stronger evidence"):
        assert phrase not in rendered
    assert '"action": "watch"' not in rendered
