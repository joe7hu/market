from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

import investment_panel.core.decision.ticker as ticker_module
from app.data_access.payloads import option_decision_adapter
from investment_panel.core.decision.ticker import (
    CapitalActionType,
    ExpressionDecision,
    ExpressionKind,
    Horizon,
    Stance,
    build_ticker_decision,
)
from investment_panel.core.refresh_jobs import ALLOWLIST
from investment_panel.core.risk_policy import RiskPolicySnapshot, compile_risk_policy_snapshot
import investment_panel.jobs.ticker_decisions as ticker_decision_job
from investment_panel.jobs.ticker_decisions import portfolio_impacts

AS_OF = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)


def _expression_for_impact(kind: ExpressionKind, utility: float) -> ExpressionDecision:
    return ExpressionDecision(
        kind=kind,
        ticker="ACME",
        horizon=Horizon.TACTICAL,
        thesis_revision="revision",
        stance=Stance.BULLISH,
        net_expected_value_per_loss_dollar=utility,
        quantity=1,
        status="eligible",
        rationale="test expression",
    )


def test_post_impact_selector_uses_portfolio_cost_once() -> None:
    expressions = {
        ExpressionKind.STOCK: _expression_for_impact(ExpressionKind.STOCK, 0.8),
        ExpressionKind.CALL: _expression_for_impact(ExpressionKind.CALL, 0.7),
        ExpressionKind.CASH: _expression_for_impact(ExpressionKind.CASH, 0),
    }
    impacts = {
        kind: ticker_module.PortfolioImpact.model_construct(
            availability="available", availability_status=ticker_module.AvailabilityStatus.AVAILABLE,
            blockers=(), expected_transaction_costs=cost,
        )
        for kind, cost in ((ExpressionKind.STOCK, 0.3), (ExpressionKind.CALL, 0.0))
    }
    tactical = SimpleNamespace()
    fundamental = SimpleNamespace()
    selected = ticker_module._post_impact_select(
        expressions=expressions, impacts=impacts, action=CapitalActionType.BUY,
        tactical=tactical, fundamental=fundamental,
    )
    assert selected.kind is ExpressionKind.CALL
    assert expressions[ExpressionKind.CALL].selected
    assert not expressions[ExpressionKind.STOCK].selected


def test_post_impact_selector_includes_diversification_benefit() -> None:
    expressions = {
        ExpressionKind.STOCK: _expression_for_impact(ExpressionKind.STOCK, 0.8),
        ExpressionKind.CALL: _expression_for_impact(ExpressionKind.CALL, 0.85),
        ExpressionKind.CASH: _expression_for_impact(ExpressionKind.CASH, 0),
    }
    impacts = {
        ExpressionKind.STOCK: ticker_module.PortfolioImpact.model_construct(
            availability="available", availability_status=ticker_module.AvailabilityStatus.AVAILABLE,
            blockers=(), expected_transaction_costs=0.0, diversification_benefit=0.2,
        ),
        ExpressionKind.CALL: ticker_module.PortfolioImpact.model_construct(
            availability="available", availability_status=ticker_module.AvailabilityStatus.AVAILABLE,
            blockers=(), expected_transaction_costs=0.0, diversification_benefit=0.0,
        ),
    }
    selected = ticker_module._post_impact_select(
        expressions=expressions, impacts=impacts, action=CapitalActionType.BUY,
        tactical=SimpleNamespace(), fundamental=SimpleNamespace(),
    )
    assert selected.kind is ExpressionKind.STOCK


def test_crypto_impact_does_not_authorize_from_stock_evidence() -> None:
    expression = _expression_for_impact(ExpressionKind.CRYPTO_SPOT, 0.5)
    _, _, _, blockers = ticker_module._crypto_impact_values(
        expression,
        {"portfolio_value": 100_000, "stock_evidence": {"status": "available", "source": "stock"}},
        AS_OF,
    )
    assert "crypto_portfolio_evidence_missing" in blockers


@pytest.mark.parametrize(
    ("kind", "evidence", "expected_blocker"),
    (
        (
            ExpressionKind.CRYPTO_SPOT,
            {
                "status": "available",
                "source_id": "crypto-feed",
                "observed_at": AS_OF,
                "price": 100,
                "risk_budget": {"available": 0, "consumed": 0},
            },
            "crypto_risk_evidence_missing",
        ),
        (
            ExpressionKind.CRYPTO_SPOT,
            {
                "status": "available",
                "source_id": "crypto-feed",
                "observed_at": AS_OF,
                "price": 0,
                "risk_budget": {"available": 100, "consumed": 0},
            },
            "crypto_evidence_price_non_positive",
        ),
        (
            ExpressionKind.CRYPTO_PERPETUAL,
            {
                "status": "available",
                "source_id": "crypto-feed",
                "observed_at": AS_OF,
                "price": 100,
                "bid": 101,
                "ask": 99,
                "risk_budget": {"available": 100, "consumed": 0},
            },
            "crypto_perpetual_quote_inverted",
        ),
    ),
)
def test_crypto_impact_fails_closed_on_invalid_bounded_evidence(
    kind: ExpressionKind,
    evidence: dict[str, object],
    expected_blocker: str,
) -> None:
    expression = _expression_for_impact(kind, 0.5).model_copy(
        update={"planned_loss": 10.0, "max_loss_per_unit": 10.0},
    )
    _, _, _, blockers = ticker_module._crypto_impact_values(
        expression,
        {"portfolio_value": 100_000, "crypto_evidence": evidence},
        AS_OF,
    )
    assert expected_blocker in blockers


def _account_facts(**updates: object) -> dict[str, object]:
    facts: dict[str, object] = {
        "broker_net_liquidation": 100_000,
        "broker_available_capital": 80_000,
        "cash_balance": 70_000,
        "buying_power": 75_000,
        "available_at": AS_OF,
        "account_source": "postgresql",
    }
    facts.update(updates)
    return facts


@pytest.mark.parametrize(
    "field",
    (
        "broker_net_liquidation",
        "broker_available_capital",
        "cash_balance",
        "buying_power",
        "available_at",
        "account_source",
    ),
)
def test_risk_policy_version_changes_for_each_material_account_fact(field: str) -> None:
    baseline = compile_risk_policy_snapshot(
        account_facts=_account_facts(),
        sleeve_capital=100_000,
        conviction_tier="STANDARD",
        policy_kind="ticker",
    )
    changed = {
        "available_at": AS_OF.replace(minute=1),
        "account_source": "other-source",
    }.get(field, 100_001)
    revised = compile_risk_policy_snapshot(
        account_facts=_account_facts(**{field: changed}),
        sleeve_capital=100_000,
        conviction_tier="STANDARD",
        policy_kind="ticker",
    )

    assert baseline.policy_version != revised.policy_version


def test_account_timestamps_are_preserved_as_distinct_fields() -> None:
    snapshot = compile_risk_policy_snapshot(
        account_facts={
            "account_observed_at": AS_OF,
            "available_at": AS_OF.replace(minute=1),
        },
        sleeve_capital=100_000,
        conviction_tier="STANDARD",
        policy_kind="ticker",
    )

    assert snapshot.account_observed_at == AS_OF
    assert "available_at" not in RiskPolicySnapshot.model_fields
    assert snapshot.model_extra["available_at"] == AS_OF.replace(minute=1)
    assert snapshot.model_dump(mode="json")["available_at"] == "2026-08-22T14:01:00Z"


def test_risk_policy_version_is_stable_for_normalized_replay_inputs() -> None:
    facts = _account_facts(available_at="2026-08-22T14:00:00Z")
    first = compile_risk_policy_snapshot(
        account_facts=facts,
        sleeve_capital=100_000,
        conviction_tier="STANDARD",
        policy_kind="ticker",
    )
    replay_facts = {**facts, "account_observed_at": AS_OF}
    replay_facts.pop("available_at")
    replay = compile_risk_policy_snapshot(
        account_facts=replay_facts,
        sleeve_capital=100_000,
        conviction_tier="STANDARD",
        policy_kind="ticker",
    )

    assert first.policy_version == replay.policy_version


def test_account_observation_and_availability_both_contribute_to_policy_identity() -> None:
    baseline = compile_risk_policy_snapshot(
        account_facts=_account_facts(account_observed_at=AS_OF),
        sleeve_capital=100_000,
        conviction_tier="STANDARD",
        policy_kind="ticker",
    )
    revised = compile_risk_policy_snapshot(
        account_facts=_account_facts(
            account_observed_at=AS_OF,
            available_at=AS_OF.replace(minute=1),
        ),
        sleeve_capital=100_000,
        conviction_tier="STANDARD",
        policy_kind="ticker",
    )

    assert baseline.policy_version != revised.policy_version


def test_ticker_compiles_one_policy_snapshot_before_sizing(monkeypatch: pytest.MonkeyPatch) -> None:
    original = ticker_module.compile_risk_policy_snapshot
    calls = 0

    def counted(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(ticker_module, "compile_risk_policy_snapshot", counted)
    decision = build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio_summary": [{"symbol": "ACME", "net_liquidation": 100_000, "available_at": "2026-08-22T13:55:00Z"}],
            "decision_queue": [{
                "symbol": "ACME", "stance": "BULLISH", "action": "BUY",
                "entry_low": 99, "entry_high": 101, "invalidation_price": 90,
                "conviction_tier": "STANDARD", "available_at": "2026-08-22T13:55:00Z",
            }],
        },
        as_of=AS_OF,
    )

    assert calls == 1
    assert decision.risk_policy_snapshot is not None
    assert decision.risk_policy.policy_version == decision.risk_policy_snapshot.policy_version
    assert decision.risk_policy.loss_budget == pytest.approx(
        decision.risk_policy_snapshot.sleeve_capital
        * decision.risk_policy_snapshot.ticker_loss_budget_pct
    )


@pytest.mark.parametrize(
    ("account_rows", "expected_blocker"),
    (
        ([], "fresh_postgres_account_facts_required"),
        ([{"net_liquidation": 100_000, "available_at": "2026-08-22T12:00:00Z"}], "fresh_postgres_account_facts_required"),
        ([{"net_liquidation": 100_000, "available_at": "2026-08-22T15:00:00Z"}], "fresh_postgres_account_facts_required"),
    ),
    ids=("missing", "stale", "future"),
)
def test_ticker_fail_closes_account_authority_without_external_context(
    account_rows: list[dict[str, object]],
    expected_blocker: str,
) -> None:
    decision = build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio_summary": account_rows,
            "decision_queue": [{
                "symbol": "ACME",
                "tactical_stance": "BEARISH",
                "fundamental_stance": "BULLISH",
                "entry_low": 95,
                "entry_high": 98,
                "invalidation_price": 90,
                "available_at": "2026-08-22T13:55:00Z",
            }],
        },
        as_of=AS_OF,
    )

    assert decision.capital_action.action is CapitalActionType.AVOID
    assert decision.resolution is not None
    assert decision.resolution.action.value == "NO_TRADE"
    assert decision.resolution.is_blocked is True
    assert decision.resolution.size is None
    assert decision.resolution.blockers == [expected_blocker]
    assert expected_blocker in decision.context_blockers


def test_conflicting_horizons_preserve_views_but_block_missing_account_authority() -> None:
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

    assert decision.capital_action.action is CapitalActionType.AVOID
    assert decision.resolution is not None
    assert decision.resolution.action.value == "NO_TRADE"
    assert decision.resolution.is_blocked is True
    assert "fresh_postgres_account_facts_required" in decision.resolution.blockers
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
    assert decision.capital_action.action is CapitalActionType.AVOID
    assert decision.resolution is not None
    assert decision.resolution.action.value == "NO_TRADE"
    assert decision.resolution.is_blocked is True
    assert "fresh_postgres_account_facts_required" in decision.resolution.blockers
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


def test_persisted_legacy_portfolio_impacts_infer_the_parent_ticker() -> None:
    source = build_ticker_decision(
        "ACME",
        {"decision_queue": [{"symbol": "ACME", "stance": "BULLISH", "available_at": AS_OF}]},
        as_of=AS_OF,
    )
    persisted = source.model_dump(mode="json")
    for impact in persisted["portfolio_impacts"].values():
        impact.pop("ticker", None)
    persisted.update({
        "ticker_decision_id": "persisted-legacy-impact-id",
        "contract_version": "ticker-decision.v1",
        "available_at": AS_OF,
    })

    replay = build_ticker_decision("ACME", {"ticker_decisions": [persisted]}, as_of=AS_OF)

    assert replay.decision_revision == source.decision_revision
    assert all(impact.ticker == "ACME" for impact in replay.portfolio_impacts.values())


def test_new_portfolio_impact_without_ticker_does_not_use_legacy_inference() -> None:
    source = build_ticker_decision(
        "ACME",
        {"decision_queue": [{"symbol": "ACME", "stance": "BULLISH", "available_at": AS_OF}]},
        as_of=AS_OF,
    )
    impact = source.portfolio_impacts[ExpressionKind.CASH]
    payload = impact.model_dump(mode="python")
    payload.pop("ticker")

    with pytest.raises(ValueError, match="ticker"):
        type(impact).model_validate(payload)


def test_legacy_portfolio_impact_inference_rejects_ambiguous_or_unknown_rows() -> None:
    source = build_ticker_decision(
        "ACME",
        {"decision_queue": [{"symbol": "ACME", "stance": "BULLISH", "available_at": AS_OF}]},
        as_of=AS_OF,
    )
    impact = source.portfolio_impacts[ExpressionKind.STOCK]
    payload = impact.model_dump(mode="python")
    payload.pop("ticker")
    before = dict(payload["portfolio_before"])
    before["symbol"] = "ACME"
    before["instrument_symbol"] = "OTHER"
    payload["portfolio_before"] = before

    with pytest.raises(ValueError, match="conflicting"):
        type(impact).from_legacy(payload, ticker="ACME")

    payload["contract_version"] = "portfolio-impact.v0"
    with pytest.raises(ValueError, match="portfolio-impact.v1"):
        type(impact).from_legacy(payload, ticker="ACME")


def test_persisted_ticker_decision_rechecks_current_account_authority() -> None:
    source = build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio_summary": [{
                "symbol": "ACME",
                "net_liquidation": 100_000,
                "available_at": AS_OF,
                "account_source": "postgresql",
            }],
            "decision_queue": [{
                "symbol": "ACME",
                "stance": "BULLISH",
                "entry_low": 99,
                "entry_high": 101,
                "invalidation_price": 90,
                "available_at": "2026-08-22T13:55:00Z",
            }],
        },
        as_of=AS_OF,
    )
    persisted = source.model_dump(mode="json")
    persisted.update({"ticker_decision_id": "persisted-id", "available_at": AS_OF.isoformat()})

    replay = build_ticker_decision(
        "ACME",
        {
            "ticker_decisions": [persisted],
            "portfolio_summary": [{
                "symbol": "ACME",
                "net_liquidation": 100_000,
                "available_at": "2026-08-22T12:00:00Z",
                "account_source": "postgresql",
            }],
        },
        as_of=AS_OF,
    )

    assert replay.capital_action.action is CapitalActionType.AVOID
    assert replay.resolution is not None
    assert replay.resolution.action.value == "NO_TRADE"
    assert replay.resolution.is_blocked is True
    assert "fresh_postgres_account_facts_required" in replay.context_blockers
    assert replay.selected_expression is not None
    assert replay.selected_expression.kind is ExpressionKind.CASH
    assert replay.market_evidence_assessment is not None
    assert replay.market_evidence_assessment.expression_kind is ExpressionKind.CASH
    assert replay.market_evidence_assessment.status == "not_applicable"
    persisted_stock = replay.expressions[ExpressionKind.STOCK]
    assert persisted_stock.quantity is None
    assert persisted_stock.entry_range is None
    assert persisted_stock.target_range is None
    assert persisted_stock.invalidation is None
    assert replay.resolution.entry is None
    assert replay.resolution.invalidation is None
    assert replay.resolution.exit is None

    supplied = source.risk_policy_snapshot.model_copy(update={"cash_balance": 1.0})
    supplied_replay = build_ticker_decision(
        "ACME",
        {
            "ticker_decisions": [persisted],
            "portfolio_summary": [{
                "symbol": "ACME",
                "net_liquidation": 100_000,
                "available_at": AS_OF,
                "account_source": "postgresql",
            }],
        },
        as_of=AS_OF,
        risk_policy_snapshot=supplied,
    )

    assert supplied_replay.resolution is not None
    assert supplied_replay.resolution.action.value == "NO_TRADE"
    assert supplied_replay.resolution.is_blocked is True
    assert "risk_policy_snapshot_mismatch" in supplied_replay.context_blockers


def test_stale_account_authority_blocks_resolution() -> None:
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

    assert decision.capital_action.action is CapitalActionType.AVOID
    assert decision.resolution is not None
    assert decision.resolution.action.value == "NO_TRADE"
    assert decision.resolution.is_blocked is True
    assert "fresh_postgres_account_facts_required" in decision.resolution.blockers
    assert decision.selected_expression is not None
    assert decision.selected_expression.kind is ExpressionKind.CASH
    assert "portfolio_nav" in {request.field for request in decision.data_requests}
    assert any("update_broker_account" in request.why_it_matters for request in decision.data_requests)


@pytest.mark.parametrize("observation_field", ("observed_at", "updated_at"))
def test_stale_account_observation_does_not_use_fresh_ingestion_time(observation_field: str) -> None:
    decision = build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio_summary": [{
                "symbol": "ACME",
                "net_liquidation": 100_000,
                "available_at": AS_OF,
                observation_field: AS_OF - timedelta(hours=1),
            }],
            "decision_queue": [{
                "symbol": "ACME", "stance": "BULLISH", "action": "BUY",
                "entry_low": 99, "entry_high": 101, "invalidation_price": 90,
                "available_at": "2026-08-22T13:55:00Z",
            }],
        },
        as_of=AS_OF,
    )

    assert decision.resolution is not None
    assert decision.resolution.action.value == "NO_TRADE"
    assert decision.resolution.is_blocked is True
    assert "fresh_postgres_account_facts_required" in decision.resolution.blockers


def test_account_policy_blocker_suppresses_positive_expression_terms() -> None:
    decision = build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio_summary": [{
                "symbol": "ACME",
                "net_liquidation": 100_000,
                "available_at": "2026-08-22T13:55:00Z",
                "account_observed_at": "2026-08-22T15:00:00Z",
                "account_source": "postgresql",
            }],
            "decision_queue": [{
                "symbol": "ACME", "stance": "BULLISH", "action": "BUY",
                "entry_low": 99, "entry_high": 101, "target_low": 110, "target_high": 120,
                "invalidation_price": 90, "conviction_tier": "STANDARD", "available_at": "2026-08-22T13:55:00Z",
            }],
        },
        as_of=AS_OF,
    )

    stock = decision.expressions[ExpressionKind.STOCK]
    assert decision.selected_expression is not None
    assert decision.selected_expression.kind is ExpressionKind.CASH
    assert stock.quantity is None
    assert stock.entry_range is None
    assert stock.target_range is None
    assert stock.invalidation is None
    assert stock.planned_loss is None
    assert decision.resolution is not None
    assert decision.resolution.action.value == "NO_TRADE"
    assert decision.resolution.size is None
    assert decision.resolution.entry is None
    assert decision.resolution.invalidation is None
    assert decision.resolution.exit is None


def test_explicit_unsupported_account_source_blocks_ticker_sizing() -> None:
    decision = build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio_summary": [{
                "net_liquidation": 100_000,
                "available_at": "2026-08-22T13:55:00Z",
                "account_observed_at": "2026-08-22T13:55:00Z",
                "account_source": "unsupported-account-source",
            }],
            "decision_queue": [{
                "symbol": "ACME", "stance": "BULLISH", "action": "BUY",
                "entry_low": 99, "entry_high": 101, "invalidation_price": 90,
                "conviction_tier": "STANDARD", "available_at": "2026-08-22T13:55:00Z",
            }],
        },
        as_of=AS_OF,
    )

    assert decision.capital_action.action is CapitalActionType.AVOID
    assert decision.selected_expression is not None
    assert decision.selected_expression.kind is ExpressionKind.CASH
    assert decision.expressions[ExpressionKind.STOCK].quantity is None
    assert decision.resolution is not None
    assert decision.resolution.size is None
    assert "postgresql_account_facts_required" in decision.context_blockers


def test_policy_blocker_rehashes_sanitized_portfolio_impacts() -> None:
    source = build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio_summary": [{"net_liquidation": 100_000, "available_at": "2026-08-22T13:55:00Z"}],
            "decision_queue": [{
                "symbol": "ACME", "stance": "BULLISH", "action": "BUY",
                "entry_low": 99, "entry_high": 101, "invalidation_price": 90,
                "conviction_tier": "STANDARD", "available_at": "2026-08-22T13:55:00Z",
            }],
        },
        as_of=AS_OF,
    )
    assert source.risk_policy_snapshot is not None
    supplied_policy = source.risk_policy_snapshot.model_copy(update={"cash_balance": 1.0})

    decision = build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio_summary": [{"net_liquidation": 100_000, "available_at": "2026-08-22T13:55:00Z"}],
            "decision_queue": [{
                "symbol": "ACME", "stance": "BULLISH", "action": "BUY",
                "entry_low": 99, "entry_high": 101, "invalidation_price": 90,
                "conviction_tier": "STANDARD", "available_at": "2026-08-22T13:55:00Z",
            }],
        },
        as_of=AS_OF,
        risk_policy_snapshot=supplied_policy,
        portfolio_impacts=source.portfolio_impacts,
    )

    for kind, impact in decision.portfolio_impacts.items():
        assert impact.expression_identity == ticker_module._expression_identity_for(
            decision.expressions[kind], kind, decision.ticker, decision.decision_revision
        )
        assert impact.impact_id == ticker_module._portfolio_impact_id(impact)
        if kind is not ExpressionKind.CASH:
            assert impact.impact_id != source.portfolio_impacts[kind].impact_id


def test_policy_blocked_seed_impacts_survive_publication_bound_rebuild() -> None:
    tables = {
        "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
        "portfolio_summary": [{
            "net_liquidation": 100_000,
            "available_at": "2026-08-22T13:55:00Z",
            "account_observed_at": "2026-08-22T12:00:00Z",
        }],
        "decision_queue": [{
            "symbol": "ACME", "stance": "BULLISH", "action": "BUY",
            "entry_low": 99, "entry_high": 101, "invalidation_price": 90,
            "conviction_tier": "STANDARD", "available_at": "2026-08-22T13:55:00Z",
        }],
    }
    replay: dict[str, object] = {}
    seed = build_ticker_decision("ACME", tables, as_of=AS_OF, portfolio_replay=replay)
    assert seed.risk_policy_snapshot is not None
    assert "fresh_postgres_account_facts_required" in seed.risk_policy_snapshot.blockers
    assert seed.market_state_snapshot is not None

    snapshot = seed.market_state_snapshot.model_copy(update={
        "snapshot_id": "market-state:publication-bound",
        "publication_id": "market-publication:publication-bound",
    })
    impacts = portfolio_impacts(seed, snapshot, snapshot.publication_id, replay)
    decision = build_ticker_decision(
        "ACME",
        tables,
        as_of=AS_OF,
        market_state_snapshot=snapshot,
        portfolio_impacts=impacts,
        risk_policy_snapshot=seed.risk_policy_snapshot,
        portfolio_replay=replay,
    )

    assert decision.market_state_publication_id == snapshot.publication_id
    assert decision.selected_expression is not None
    assert decision.selected_expression.kind is ExpressionKind.CASH
    for kind, impact in decision.portfolio_impacts.items():
        assert impact.expression_identity == ticker_module._expression_identity_for(
            decision.expressions[kind], kind, decision.ticker, decision.decision_revision
        )


def test_publisher_reuses_seed_cutoff_stock_evidence_on_bound_rebuild() -> None:
    replay = {
        "cutoff": AS_OF,
        "positions": [],
        "portfolio_value": 0.0,
        "transaction_count": 0,
        "eligible_position_count": 0,
        "valued_position_count": 0,
        "missing_valuation_count": 0,
        "valuation_complete": True,
        "lineage": [],
        "book_identity": "portfolio-book:publisher-evidence",
    }
    tables = {
        "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
        "portfolio_summary": [{"net_liquidation": 100_000, "available_at": "2026-08-22T13:55:00Z"}],
        "decision_queue": [{
            "symbol": "ACME", "stance": "BULLISH", "action": "BUY",
            "entry_low": 99, "entry_high": 101, "invalidation_price": 90,
            "conviction_tier": "STANDARD", "available_at": "2026-08-22T13:55:00Z",
        }],
        "fundamentals": [{
            "symbol": "ACME", "sector": "Technology", "beta": 1.1,
            "avg_dollar_volume": 1_000_000.0, "correlation_cluster_delta": 0.01,
            "available_at": "2026-08-22T13:55:00Z",
        }],
    }
    seed = build_ticker_decision("ACME", tables, as_of=AS_OF, portfolio_replay=replay)
    seed_evidence = seed.portfolio_impacts[ExpressionKind.STOCK].portfolio_before["stock_evidence"]
    assert seed_evidence["sector"] == "Technology"

    replay_for_publication = ticker_decision_job._replay_with_seed_stock_evidence(seed, replay)
    assert "stock_impact" not in replay_for_publication
    impacts = portfolio_impacts(
        seed,
        seed.market_state_snapshot,
        str(seed.market_state_snapshot.publication_id),
        replay_for_publication,
    )
    rebuilt = build_ticker_decision(
        "ACME",
        tables,
        as_of=AS_OF,
        market_state_snapshot=seed.market_state_snapshot,
        portfolio_impacts=impacts,
        risk_policy_snapshot=seed.risk_policy_snapshot,
        portfolio_replay=replay_for_publication,
    )

    assert rebuilt.portfolio_impacts[ExpressionKind.STOCK].portfolio_before["stock_evidence"] == seed_evidence


def test_policy_blocker_without_resolution_forces_blocked_no_trade() -> None:
    source = build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio_summary": [{"net_liquidation": 100_000, "available_at": "2026-08-22T13:55:00Z"}],
            "decision_queue": [{
                "symbol": "ACME", "stance": "BULLISH", "action": "BUY",
                "entry_low": 99, "entry_high": 101, "invalidation_price": 90,
                "conviction_tier": "STANDARD", "available_at": "2026-08-22T13:55:00Z",
            }],
        },
        as_of=AS_OF,
    )
    payload = source.model_dump(mode="python")
    payload["resolution"] = None
    payload["risk_policy_snapshot"] = source.risk_policy_snapshot.model_copy(
        update={"blockers": ("policy_blocker",)}
    )

    decision = ticker_module.TickerDecision.model_validate(payload)

    assert decision.capital_action.action is CapitalActionType.AVOID
    assert decision.resolution is not None
    assert decision.resolution.action.value == "NO_TRADE"
    assert decision.resolution.is_blocked is True
    assert decision.resolution.blockers == ["policy_blocker"]
    assert decision.resolution.size is None


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


def test_ticker_cli_aliases_share_the_symbols_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_panel.jobs import ticker_decisions

    calls: list[tuple[str | None, list[str] | None, int]] = []

    def fake_publish(config_path: str | None, *, symbols: list[str] | None, limit: int) -> dict[str, str]:
        calls.append((config_path, symbols, limit))
        return {"status": "ok"}

    monkeypatch.setattr(ticker_decisions, "publish", fake_publish)

    for option in ("--ticker", "--tickers"):
        ticker_decisions.main([option, "QQQ"])

    assert calls == [
        ("config.yaml", ["QQQ"], 2_000),
        ("config.yaml", ["QQQ"], 2_000),
    ]
