from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest

from app.data_access.payloads import option_decision_adapter
from investment_panel.core.decision.ticker import (
    CapitalActionType,
    ExpressionKind,
    build_ticker_decision,
)
from investment_panel.core.refresh_jobs import ALLOWLIST


AS_OF = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)


def test_conflicting_horizons_choose_an_owned_hold_action_and_share_one_thesis() -> None:
    decision = build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio": [{"symbol": "ACME", "quantity": 10, "nav": 100_000, "available_at": "2026-08-22T13:55:00Z"}],
            "decision_queue": [{
                "symbol": "ACME",
                "tactical_stance": "BEARISH",
                "fundamental_stance": "BULLISH",
                "entry_low": 95,
                "entry_high": 102,
                "invalidation_price": 90,
                "catalyst": "earnings",
                "available_at": "2026-08-22T13:55:00Z",
            }],
        },
        as_of=AS_OF,
    )

    assert decision.capital_action.action is CapitalActionType.HOLD
    assert decision.capital_action.owned is True
    assert decision.tactical.invalidation == decision.fundamental.invalidation
    assert decision.tactical.horizon == "TACTICAL"
    assert decision.fundamental.horizon == "FUNDAMENTAL"
    assert all(scenario.probability is None for scenario in decision.tactical.scenarios)
    assert decision.selected_expression is not None
    declarations = {item.name: item for item in decision.input_manifest.signal_declarations}
    assert declarations["company_financials"].source.startswith("SEC")
    assert declarations["participant_option_flow"].evidence_state == "HYPOTHESIS"


def test_missing_inputs_keep_directional_views_but_do_not_invent_quantity() -> None:
    decision = build_ticker_decision(
        "ACME",
        {"decision_queue": [{"symbol": "ACME", "stance": "BULLISH", "action": "BUY", "available_at": "2026-08-22T13:55:00Z"}]},
        as_of=AS_OF,
    )

    assert decision.tactical.stance == "BULLISH"
    assert decision.capital_action.action is CapitalActionType.BUY
    assert decision.selected_expression is not None
    assert decision.selected_expression.quantity is None
    assert {request.field for request in decision.data_requests} >= {"current_price", "invalidation", "portfolio_nav"}
    assert any("update_broker_account" in request.collect_now for request in decision.data_requests)


def test_stale_sec_financials_create_a_refresh_request() -> None:
    decision = build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "decision_queue": [{"symbol": "ACME", "stance": "BULLISH", "available_at": "2026-08-22T13:55:00Z"}],
            "fundamentals": [{
                "symbol": "ACME",
                "source": "sec_companyfacts",
                "available_at": "2026-08-20T13:55:00Z",
                "values": {"metrics": {"revenue": 100}},
            }],
        },
        as_of=AS_OF,
    )

    assert "company_financials" in {request.field for request in decision.data_requests}


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
                "available_at": "2026-08-22T13:55:00Z",
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
                "available_at": "2026-08-22T13:55:00Z",
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
        "decision_queue": [{"symbol": "ACME", "stance": "NEUTRAL", "available_at": "2026-08-22T13:55:00Z"}],
        "future_revision": [{"symbol": "ACME", "stance": "BEARISH", "available_at": "2026-08-23T13:55:00Z"}],
    }
    first = build_ticker_decision("ACME", base, as_of=AS_OF)
    second = build_ticker_decision("ACME", {**base, "decision_queue": [{"symbol": "ACME", "stance": "BULLISH", "available_at": "2026-08-22T13:55:00Z"}]}, as_of=AS_OF)

    assert first.input_manifest.input_hash != second.input_manifest.input_hash
    assert "future_revision" not in first.input_manifest.inputs


def test_persisted_ticker_decision_is_not_dropped_by_point_in_time_filtering() -> None:
    source = build_ticker_decision(
        "ACME",
        {"decision_queue": [{"symbol": "ACME", "stance": "BULLISH", "available_at": AS_OF}]},
        as_of=AS_OF,
    )
    persisted = source.model_dump(mode="json")
    persisted.update({
        "ticker_decision_id": "persisted-id",
        "contract_version": "ticker-decision.v1",
        "available_at": AS_OF,
    })

    replay = build_ticker_decision("ACME", {"ticker_decisions": [persisted]}, as_of=AS_OF)

    assert replay.decision_revision == source.decision_revision


def test_wait_for_price_keeps_the_directional_expression_and_stale_nav_is_actionable() -> None:
    decision = build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio_summary": [{"symbol": "ACME", "net_liquidation": 100_000, "available_at": "2026-08-22T12:00:00Z"}],
            "decision_queue": [{
                "symbol": "ACME", "tactical_stance": "BEARISH", "fundamental_stance": "BULLISH",
                "entry_low": 95, "entry_high": 98, "invalidation_price": 90,
                "available_at": "2026-08-22T13:55:00Z",
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
        {"decision_queue": [{"symbol": "ACME", "stance": "NEUTRAL", "available_at": "2026-08-22T13:55:00Z"}]},
        as_of=AS_OF,
    )
    rendered = json.dumps(decision.model_dump(mode="json")).lower()
    for phrase in ("review the risk", "resolve blockers", "needs stronger evidence"):
        assert phrase not in rendered
    assert '"action": "watch"' not in rendered


def test_every_data_request_collect_operation_is_a_runnable_refresh_job() -> None:
    decision = build_ticker_decision(
        "ACME",
        {"decision_queue": [{"symbol": "ACME", "stance": "BULLISH", "available_at": "2026-08-22T13:55:00Z"}]},
        as_of=AS_OF,
    )
    assert {request.collect_now for request in decision.data_requests} <= set(ALLOWLIST)


def test_options_compatibility_adapter_cannot_reintroduce_a_legacy_thesis() -> None:
    ticker_decision = {
        "ticker": "ACME",
        "as_of": AS_OF,
        "decision_revision": "ticker-revision-1",
        "decision_contract_version": "ticker-decision.v1",
        "capital_action": {"action": "BUY", "rationale": "Buy the shared ticker expression."},
        "tactical": {"stance": "BULLISH", "invalidation": {"statement": "Below 90"}},
        "fundamental": {"stance": "BULLISH", "invalidation": {"statement": "Below 90"}},
        "data_requests": [],
        "selected_expression": {"kind": "CALL"},
        "expressions": {
            "CALL": {
                "kind": "CALL",
                "status": "eligible",
                "quantity": 2,
                "max_loss_per_unit": 100,
                "lower_confidence_expectancy": 0.05,
                "net_expected_value_per_loss_dollar": 0.08,
                "expiration": "2026-10-16",
                "scenarios": [{"name": "bull", "probability": 0.7}],
                "legs": [{
                    "contract_id": 1, "option_type": "call", "side": "long",
                    "strike": 105, "bid": 2.0, "ask": 2.2, "bid_size": 10, "ask_size": 10,
                    "expiration": "2026-10-16",
                }],
            }
        },
    }
    legacy = {
        "symbol": "ACME",
        "state": "READY",
        "strongest_candidate": {
            "thesis": {"direction": "BEARISH", "summary": "legacy opinion"},
            "forecast": {"scenarios": [{"name": "bear", "probability": 1.0}]},
            "legs": [{"contract_id": 999, "option_type": "put", "side": "long", "strike": 80}],
            "ticket": {"legs": [{"contract_id": 1}], "blockers": []},
        },
    }

    result = option_decision_adapter(ticker_decision, legacy)
    assert "strongest_candidate" not in result
    assert result["state"] == "WATCH"
    assert result["decision_truth"]["readiness_state"] == "WATCH"
    assert "opportunity_lineage_invalid" in result["decision_truth"]["blockers"]
    assert result["decision_truth"]["route_version"] == "ticker-decision.v1"


def test_options_compatibility_adapter_uses_the_selected_expression() -> None:
    ticker_decision = {
        "ticker": "ACME",
        "as_of": AS_OF,
        "decision_revision": "ticker-revision-2",
        "decision_contract_version": "ticker-decision.v1",
        "capital_action": {"action": "BUY", "rationale": "Use the selected put."},
        "tactical": {"stance": "BEARISH"},
        "fundamental": {"stance": "BEARISH"},
        "data_requests": [],
        "selected_expression": {"kind": "PUT"},
        "expressions": {
            "CALL": {"kind": "CALL", "status": "eligible", "legs": [{"contract_id": 1, "strike": 105, "expiration": "2026-10-16"}]},
            "PUT": {"kind": "PUT", "status": "eligible", "quantity": 1, "max_loss_per_unit": 100, "legs": [{"contract_id": 2, "strike": 95, "expiration": "2026-10-16"}]},
        },
    }

    result = option_decision_adapter(ticker_decision, {})

    assert "strongest_candidate" not in result
    assert result["state"] == "WATCH"
    assert "opportunity_lineage_invalid" in result["decision_truth"]["blockers"]
    assert result["summary"]["ticker_selected_expression"] == "PUT"
