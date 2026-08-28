from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from investment_panel.core.decision import (
    ExpressionKind,
    TradePlan,
    bind_trade_plan,
    build_decision_resolution,
    build_ticker_decision,
    build_trade_plan,
    trade_expression_identity,
)
from investment_panel.jobs.ticker_decisions import portfolio_impacts


AS_OF = datetime(2026, 8, 22, 14, tzinfo=UTC)


def _complete_replay() -> dict[str, object]:
    return {
        "cutoff": AS_OF,
        "positions": [],
        "portfolio_value": 0.0,
        "transaction_count": 0,
        "eligible_position_count": 0,
        "valued_position_count": 0,
        "missing_valuation_count": 0,
        "valuation_complete": True,
        "lineage": [],
        "book_identity": "portfolio-book:acme",
    }


def _tables(symbol: str = "ACME") -> dict[str, list[dict[str, object]]]:
    available_at = "2026-08-22T13:55:00Z"
    return {
        "quotes": [{"symbol": symbol, "price": 100, "available_at": available_at, "confirmed": True}],
        "portfolio_summary": [{"symbol": symbol, "net_liquidation": 100_000, "available_at": available_at}],
        "decision_queue": [{
            "symbol": symbol, "stance": "BULLISH", "action": "BUY",
            "entry_low": 99, "entry_high": 101, "target_low": 110, "target_high": 120,
            "invalidation_price": 90, "conviction_tier": "STANDARD", "available_at": available_at,
            "scenarios": {
                "bear": {"probability": 0.2}, "base": {"probability": 0.5}, "bull": {"probability": 0.3},
            },
        }],
        "valuations": [{"symbol": symbol, "upside_pct": 0.01, "available_at": available_at}],
        "fundamentals": [{"symbol": symbol, "source": "sec_companyfacts", "available_at": available_at}],
        "earnings": [{"symbol": symbol, "available_at": available_at}],
        "ticker_benchmark_snapshot": [{"symbol": symbol, "available_at": available_at}],
        "macro": [{"symbol": symbol, "available_at": available_at}],
        "disclosures": [{"symbol": symbol, "available_at": available_at}],
        "short_interest": [{"symbol": symbol, "available_at": available_at}],
    }


def _decision() -> tuple[object, object, object]:
    seed = build_ticker_decision("ACME", _tables(), as_of=AS_OF)
    snapshot = seed.market_state_snapshot.model_copy(update={
        "snapshot_id": "market:snapshot:acme",
        "publication_id": "market:publication:acme",
        "availability": "available",
        "blockers": (),
    })
    replay = _complete_replay()
    impacts = portfolio_impacts(seed, snapshot, "market:publication:acme", replay)
    decision = build_ticker_decision(
        "ACME", _tables(), as_of=AS_OF, market_state_snapshot=snapshot,
        portfolio_impacts=impacts, risk_policy_snapshot=seed.risk_policy_snapshot,
        portfolio_replay=replay,
    )
    selected = decision.selected_expression
    assert selected is not None
    impact = decision.portfolio_impacts[selected.kind]
    signal = {"signal_id": "alpha:acme"}
    rank = {
        "rank_id": "rank:acme",
        "ticker": "ACME",
        "opportunity_episode_id": decision.opportunity_episode_id,
        "decision_revision": decision.decision_revision,
        "policy_version": decision.policy_version,
        "selected_expression_identity": trade_expression_identity(selected),
        "selected_expression_kind": selected.kind.value,
        "portfolio_impact_id": impact.impact_id,
        "risk_policy_version": decision.policy_version,
        "alpha_signal_id": signal["signal_id"],
        "market_snapshot_id": snapshot.snapshot_id,
        "market_state_publication_id": snapshot.publication_id,
        "trade_rank": 1,
        "trade_utility": 0.4,
        "evaluated_universe_complete": True,
    }
    return decision, rank, signal


def _actionable_plan():
    decision, rank, signal = _decision()
    selected = decision.selected_expression
    assert selected is not None
    impact = decision.portfolio_impacts[selected.kind]
    resolution = build_decision_resolution(
        action="BUY",
        decision_revision=decision.decision_revision,
        policy_version=decision.policy_version,
        provenance={"as_of": AS_OF},
        ticker=decision.ticker,
        entry=selected.entry_range.model_dump(mode="json"),
        size=selected.quantity,
        invalidation=selected.invalidation.model_dump(mode="json"),
        exit=selected.target_range.model_dump(mode="json"),
        ttl=AS_OF.date(),
        portfolio_context=impact.model_dump(mode="json"),
        data_quality="FRESH",
    )
    plan = build_trade_plan(
        decision=decision, rank=rank, alpha_signal=signal, resolution=resolution,
    )
    return decision, rank, signal, plan, resolution


def test_trade_plan_id_is_deterministic_and_excludes_bundle_publication() -> None:
    decision, rank, signal, first, resolution = _actionable_plan()
    second = build_trade_plan(decision=decision, rank=rank, alpha_signal=signal, resolution=resolution)
    with_publication = build_trade_plan(
        decision=decision, rank=rank, alpha_signal=signal,
        resolution=resolution, publication_id="bundle:later",
    )
    with_reader_metadata = TradePlan.model_validate({
        **first.model_dump(mode="json"),
        "stable_key": "ACME:plan:reader",
        "publication_published_at": "2026-08-22T14:00:00Z",
    })

    assert first.trade_plan_id == second.trade_plan_id
    assert first.trade_plan_id == with_publication.trade_plan_id
    assert first.trade_plan_id == with_reader_metadata.trade_plan_id
    assert first.eligibility == "BLOCKED"
    assert first.action == "NO_TRADE"
    assert first.authorization_mode == "NONE"
    assert first.primary_blocker == "portfolio_marginal_risk_unsupported"


def test_trade_plan_id_binds_identity_and_economic_terms() -> None:
    _, _, _, plan, _ = _actionable_plan()
    changed_identity = plan.model_dump(mode="json")
    changed_identity["selected_expression_identity"] = "STOCK:changed"
    with pytest.raises(ValueError, match="expression identity"):
        TradePlan.model_validate(changed_identity)

    changed_terms = plan.model_dump(mode="json")
    changed_terms["portfolio_impact_id"] = "portfolio-impact:stale"
    with pytest.raises(ValueError, match="portfolio impact id"):
        TradePlan.model_validate(changed_terms)


def test_blocked_plan_is_cash_no_trade_with_one_blocker() -> None:
    decision, _, _, _, _ = _actionable_plan()
    plan = build_trade_plan(decision=decision, rank=None)

    assert plan.selected_expression_kind is ExpressionKind.CASH
    assert plan.action == "NO_TRADE"
    assert plan.eligibility == "BLOCKED"
    assert plan.authorization_mode == "NONE"
    assert plan.quantity is None
    assert plan.primary_blocker == "portfolio_impact_unavailable:STOCK"
    assert plan.blockers == (plan.primary_blocker,)


def test_binding_reuses_exact_plan_terms_in_resolution() -> None:
    decision, _, _, plan, _ = _actionable_plan()
    bound = bind_trade_plan(decision, plan)

    assert bound.trade_plan == plan
    assert bound.resolution is not None
    assert bound.resolution.trade_plan_id == plan.trade_plan_id
    assert bound.resolution.entry == plan.entry
    assert bound.resolution.size == plan.quantity
    assert bound.resolution.exit == plan.profit_exit
    assert bound.resolution.ttl == plan.expiry
    assert bound.resolution.portfolio_context == plan.portfolio_impact.model_dump(mode="json")


def test_automatic_staging_forwards_only_the_canonical_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_panel.jobs import ticker_decisions

    decision, rank, _, plan, _ = _actionable_plan()
    bundle_id = "bundle:acme"
    plan = plan.model_copy(update={"publication_id": bundle_id})
    decision = bind_trade_plan(decision, plan)
    rank_row = {**rank, "publication_id": bundle_id}
    calls: dict[str, object] = {}

    class FakeAnalysisRepository:
        def __init__(self, _runtime: object) -> None:
            pass

        def publication_rows(self, _scope: str, model_name: str, *, include_lineage: bool) -> list[dict[str, object]]:
            assert include_lineage is True
            return [rank_row] if model_name == "opportunity_rank" else [plan.model_dump(mode="json")]

    class FakeExecutionRepository:
        def __init__(self, _runtime: object, _config: object) -> None:
            pass

        def stage(self, **kwargs: object) -> dict[str, str]:
            calls.update(kwargs)
            return {"status": "staged"}

    monkeypatch.setattr(ticker_decisions, "AnalysisRepository", FakeAnalysisRepository)
    monkeypatch.setattr(ticker_decisions, "TickerPaperExecutionRepository", FakeExecutionRepository)
    config = SimpleNamespace(analysis=SimpleNamespace(options_decision_system=SimpleNamespace(
        mode="paper", ticker_paper_actions_enabled=True,
    )))

    result = ticker_decisions._stage_eligible(object(), config, [decision])

    assert result["status"] == "ok"
    assert result["staged"] == []
    assert calls == {}


def test_manual_staging_forwards_the_exact_plan_id() -> None:
    from app.actions.tickers import TickerActions

    decision, _, _, plan, _ = _actionable_plan()
    calls: dict[str, object] = {}

    class FakeExecution:
        def stage(self, **kwargs: object) -> dict[str, str]:
            calls.update(kwargs)
            return {"status": "staged"}

    actions = TickerActions.__new__(TickerActions)
    actions.execution = FakeExecution()
    result = actions.stage_paper_entry(
        ticker=decision.ticker,
        decision=decision,
        payload={
            "decision_revision": decision.decision_revision,
            "policy_version": decision.policy_version,
            "expression_kind": plan.selected_expression_kind.value,
            "idempotency_key": "caller-key",
            "quantity": plan.quantity,
            "limit_price": plan.entry_limit,
            "trade_plan_id": plan.trade_plan_id,
        },
    )

    assert result == {"status": "staged"}
    assert calls["trade_plan_id"] == plan.trade_plan_id
    assert calls["quantity"] == plan.quantity
    assert calls["limit_price"] == plan.entry_limit


def test_legacy_staging_rejects_missing_canonical_trade_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    from uuid import uuid4

    from fastapi import HTTPException

    from app.contracts import OptionPaperEntryInput
    from app.routers import options as options_router

    class FakeOptionsActions:
        def signal_detail(self, _decision_id: object) -> dict[str, str]:
            return {"symbol": "ACME", "structure": "long_call"}

    class FakeTickerActions:
        def stage_paper_entry(self, **_kwargs: object) -> None:
            pytest.fail("legacy staging must not bypass a missing canonical trade plan")

    class MissingPlanDecision:
        trade_plan = None

    class FakeTickerDecision:
        @classmethod
        def model_validate(cls, _payload: object) -> MissingPlanDecision:
            return MissingPlanDecision()

    monkeypatch.setattr(options_router.panel_snapshot, "context", lambda **_kwargs: (None, object()))
    monkeypatch.setattr(options_router.payloads, "ticker_payload", lambda *_args: {"ticker_decision": {}})
    monkeypatch.setattr(options_router, "TickerDecision", FakeTickerDecision)

    with pytest.raises(HTTPException) as error:
        options_router.stage_option_radar_paper_entry(
            decision_id=uuid4(),
            payload=OptionPaperEntryInput(idempotency_key="legacy", quantity=1, limit_price=1),
            actions=FakeOptionsActions(),
            ticker_actions=FakeTickerActions(),
            config=object(),
            _request=None,
        )

    assert error.value.status_code == 409
    assert error.value.detail == "Current publication has no canonical trade plan"


def test_today_api_projects_the_bound_plan_terms(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.data_access.types import DataStatus, PanelData
    from app.routers import panel as panel_router

    decision, rank, _, plan, _ = _actionable_plan()
    bundle_id = "bundle:acme"
    plan = plan.model_copy(update={"publication_id": bundle_id})
    decision = bind_trade_plan(decision, plan)
    row = {
        "ticker": decision.ticker,
        "decision_revision": decision.decision_revision,
        "opportunity_episode_id": decision.opportunity_episode_id,
        "policy_version": decision.policy_version,
        "as_of": decision.as_of.isoformat(),
        "input_manifest": {"input_hash": decision.input_manifest.input_hash},
        "selected_expression": decision.selected_expression.model_dump(mode="json"),
        "resolution": decision.resolution.model_dump(mode="json"),
        "capital_action": decision.capital_action.model_dump(mode="json"),
    }
    panel = PanelData(
        status=DataStatus(True, "loaded", "test"),
        tables={
            "ticker_decisions": [row],
            "opportunity_rank": [{**rank, "publication_id": bundle_id}],
            "trade_plan": [plan.model_dump(mode="json")],
        },
    )
    monkeypatch.setattr(panel_router.panel_owner, "context", lambda **_kwargs: (None, panel))

    result = panel_router.today(config=object())

    action = result["actions"][0]
    assert action["action"] == "NO_TRADE"
    assert action["selected_expression"] == "CASH"
    assert action["trade_plan"] is None
