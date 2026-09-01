from __future__ import annotations

from datetime import UTC, datetime
import inspect
from types import SimpleNamespace

import pytest

from investment_panel.core.decision import (
    AvailabilityStatus,
    ExpressionKind,
    OpportunityRank,
    apply_opportunity_rank_safety,
    TradePlan,
    bind_trade_plan,
    build_decision_resolution,
    build_ticker_decision,
    build_trade_plan,
    trade_expression_identity,
)
from investment_panel.jobs.ticker_decisions import portfolio_impacts


AS_OF = datetime(2026, 8, 22, 14, tzinfo=UTC)


def _complete_replay(symbol: str = "ACME") -> dict[str, object]:
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
        "book_identity": f"portfolio-book:{symbol.lower()}",
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


def _decision(symbol: str = "ACME") -> tuple[object, object, object]:
    identity = symbol.lower()
    seed = build_ticker_decision(symbol, _tables(symbol), as_of=AS_OF)
    snapshot = seed.market_state_snapshot.model_copy(update={
        "snapshot_id": f"market:snapshot:{identity}",
        "publication_id": f"market:publication:{identity}",
        "availability": "available",
        "blockers": (),
    })
    replay = _complete_replay(symbol)
    impacts = portfolio_impacts(seed, snapshot, snapshot.publication_id, replay)
    decision = build_ticker_decision(
        symbol, _tables(symbol), as_of=AS_OF, market_state_snapshot=snapshot,
        portfolio_impacts=impacts, risk_policy_snapshot=seed.risk_policy_snapshot,
        portfolio_replay=replay,
    )
    selected = decision.selected_expression
    assert selected is not None
    impact = decision.portfolio_impacts[selected.kind]
    signal = {"signal_id": f"alpha:{identity}"}
    rank = {
        "rank_id": f"rank:{identity}",
        "ticker": symbol,
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


def _actionable_plan(symbol: str = "ACME"):
    decision, rank, signal = _decision(symbol)
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
    assert first.primary_blocker == "stock_nav_missing"


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


def test_blocked_resolution_preserves_diagnostic_blockers() -> None:
    resolution = build_decision_resolution(
        action="NO_TRADE",
        decision_revision="revision:diagnostics",
        policy_version="risk-policy.test",
        ticker="ACME",
        provenance={},
        blockers=["alternate_expression_unavailable", "market_state_unavailable"],
        blocked=True,
    )

    assert resolution.primary_blocker == "alternate_expression_unavailable"
    assert resolution.blockers == ["alternate_expression_unavailable", "market_state_unavailable"]


@pytest.mark.parametrize(
    ("blocker", "expected"),
    (
        ("selected_expression_unsupported", AvailabilityStatus.UNSUPPORTED),
        ("market_state_missing", AvailabilityStatus.MISSING),
        ("market_state_stale", AvailabilityStatus.STALE),
        ("alpha_oos_evaluation_missing", AvailabilityStatus.NOT_CALIBRATED),
        ("risk_policy_blocked", AvailabilityStatus.POLICY_BLOCKED),
        ("publication_lineage_mismatch", AvailabilityStatus.ERROR),
        ("cash_selected", AvailabilityStatus.NOT_APPLICABLE),
    ),
)
def test_blocked_trade_plan_projects_typed_primary_without_losing_details(
    blocker: str, expected: AvailabilityStatus,
) -> None:
    decision, rank, signal = _decision()
    resolution = build_decision_resolution(
        action="NO_TRADE",
        decision_revision=decision.decision_revision,
        policy_version=decision.policy_version,
        provenance={"as_of": AS_OF},
        ticker=decision.ticker,
        blockers=[blocker, "zz_secondary_diagnostic"],
        blocked=True,
    )

    plan = build_trade_plan(
        decision=decision, rank=rank, alpha_signal=signal, resolution=resolution,
    )

    assert plan.availability_status is expected
    assert plan.primary_blocker == blocker
    assert plan.blockers == (blocker, "zz_secondary_diagnostic")


def test_rank_safety_reprojects_primary_status_and_preserves_all_blockers() -> None:
    from investment_panel.jobs import ticker_decisions

    decision, rank_payload, _signal = _decision()
    rank = OpportunityRank.model_construct(
        **rank_payload,
        cutoff=AS_OF,
        input_cutoff=AS_OF,
        blockers=("existing_rank_diagnostic",),
        availability_status=AvailabilityStatus.AVAILABLE,
    )
    safe = apply_opportunity_rank_safety(
        decision, {"trade_rank_unavailable_reason": "alpha_oos_evaluation_missing"},
    )

    projected = ticker_decisions._rank_after_safety(rank, safe)

    assert projected.availability_status is AvailabilityStatus.NOT_CALIBRATED
    assert projected.primary_blocker == "alpha_oos_evaluation_missing"
    assert projected.blockers == (
        "existing_rank_diagnostic",
        "alpha_oos_evaluation_missing",
    )


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

    assert result["actions"] == []
    assert result["missing_plan_count"] == 1
    action = result["book_actions"][0]
    assert action["action"] == "NO_TRADE"
    assert action["selected_expression"] == "CASH"
    assert action["trade_plan"] is None


def test_today_queue_input_bound_is_independent_from_snapshot_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.data_access import loaders as loaders_owner, payloads as payloads_owner
    from app.routers import panel as panel_router
    from investment_panel.database.panel_models import QUERY_POLICIES, today_authority_pages

    tables: dict[str, list[dict[str, object]]] = {
        "ticker_decisions": [],
        "opportunity_rank": [],
        "trade_plan": [],
        "portfolio": [{"symbol": "HELD"}],
    }
    for index in range(5):
        symbol = f"T{index}"
        decision, rank, _, plan, _ = _actionable_plan(symbol)
        bundle_id = f"bundle:{symbol.lower()}"
        plan = plan.model_copy(update={"publication_id": bundle_id})
        decision = bind_trade_plan(decision, plan)
        rank_payload = {**rank, "ranking_publication_id": bundle_id}
        plan_payload = plan.model_dump(mode="json")
        tables["ticker_decisions"].append({
            "ticker": decision.ticker,
            "ticker_decision_id": f"decision:{symbol.lower()}",
            "decision_revision": decision.decision_revision,
            "opportunity_episode_id": decision.opportunity_episode_id,
            "policy_version": decision.policy_version,
            "as_of": decision.as_of.isoformat(),
            "selected_expression": decision.selected_expression.model_dump(mode="json"),
            "resolution": decision.resolution.model_dump(mode="json"),
            "capital_action": decision.capital_action.model_dump(mode="json"),
            "opportunity_rank": rank_payload,
            "trade_plan": plan_payload,
        })
        tables["opportunity_rank"].append(rank_payload)
        tables["trade_plan"].append(plan_payload)

    calls: list[dict[str, int]] = []
    authority_calls: list[dict[str, int]] = []

    def fake_load(_config, table_names, **options):
        limits = dict(options.get("query_row_limits") or {})
        calls.append(limits)
        loaded = {
            name: [
                {**row, "missing_plan_count": 7} if name == "today_ticker_actions" else row
                for row in list(tables.get("ticker_decisions" if name == "today_ticker_actions" else name, []))[: limits.get(name)]
            ]
            if limits.get(name) is not None
            else list(tables.get("ticker_decisions" if name == "today_ticker_actions" else name, []))
            for name in table_names
        }
        return loaded, {
            "database": "postgresql",
            "available_model_count": len(table_names),
            "unavailable_models": [],
        }

    def fake_authority(_config, **limits):
        authority_calls.append(limits)
        return (
            list(tables["ticker_decisions"][:3]),
            list(tables["opportunity_rank"][:3]),
            list(tables["trade_plan"][:3]),
            {"ticker_decisions": 5, "opportunity_rank": 5, "trade_plan": 5},
            7,
        )

    monkeypatch.setattr(loaders_owner, "load_postgres_tables", fake_load)
    monkeypatch.setattr(loaders_owner, "_load_today_authority", fake_authority)
    panel = loaders_owner.load_panel_scope_data(object(), "today")
    panel.tables["opportunity_rank"].reverse()
    panel.tables["trade_plan"].reverse()
    monkeypatch.setattr(panel_router.panel_owner, "context", lambda **_kwargs: (None, panel))

    queue = panel_router.today(
        config=object(),
        option_actions=SimpleNamespace(decision_inbox=lambda **_kwargs: {"items": []}),
    )
    snapshot = payloads_owner.panel_snapshot_payload(panel, "today")

    query = QUERY_POLICIES["today_ticker_actions"].query
    authority_source = inspect.getsource(today_authority_pages)
    assert calls == [{
        "preopen_daily_brief": 1,
        "daily_brief": 12,
        "portfolio_risk_cards": 8,
        "feed_signals": 12,
    }]
    assert authority_calls == [{
        "decision_offset": 0,
        "rank_offset": 0,
        "plan_offset": 0,
        "decision_limit": 3,
        "rank_limit": 3,
        "plan_limit": 3,
    }]
    assert "decision.market_state_snapshot" not in query
    assert "decision.portfolio_impacts" not in query
    assert "decision.tactical" not in query
    assert "decision.fundamental" not in query
    assert "octet_length((decision.input_manifest->'trade_plan')::text) <= 327680" in query
    assert "pg_input_is_valid(opportunity_rank->>'trade_rank', 'integer')" in query
    assert "to_jsonb(positioned_actions)" not in authority_source
    assert "SELECT current_today_actions.*" not in authority_source
    assert 'DIRECT_QUERIES["today_ticker_actions"]' not in authority_source
    assert "'opportunity_rank', opportunity_rank" not in authority_source
    assert "'trade_plan', trade_plan" not in authority_source
    assert "AS trade_plan_present" in authority_source
    assert "JOIN analysis.ticker_decision stored_decision" in authority_source
    assert authority_source.count(
        "THEN stored_decision.input_manifest->'trade_plan'"
    ) == 2
    assert "END AS validation_plan," not in authority_source
    assert "END AS validation_plan_valid" in authority_source
    assert panel.metadata["today_missing_plan_count"] == 7
    assert all("input_manifest" not in row for row in panel.rows("ticker_decisions"))
    assert all("missing_plan_count" not in row for row in panel.rows("ticker_decisions"))
    assert queue["actions"] == []
    assert queue["missing_plan_count"] == 7
    assert [row["ticker"] for row in queue["book_actions"][:-1]] == [f"T{index}" for index in range(3)]
    assert all(
        row["trade_rank_unavailable_reason"] != "opportunity_rank_missing"
        for row in queue["book_actions"][:-1]
    )
    for row in queue["book_actions"]:
        assert "portfolio_context" not in (row.get("resolution") or {})
        plan = row.get("trade_plan") or {}
        assert "input_lineage" not in plan
        assert "portfolio_impact" not in plan
    assert snapshot["tables"]["ticker_decisions"]["count"] == 5
    assert len(snapshot["tables"]["ticker_decisions"]["rows"]) == 3
    assert snapshot["tables"]["portfolio"]["rows"] == [{"symbol": "HELD"}]


def test_today_hides_non_owned_decisions_without_sampled_rank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.data_access.types import DataStatus, PanelData
    from app.routers import panel as panel_router

    panel = PanelData(
        status=DataStatus(True, "loaded", "test"),
        tables={
            "ticker_decisions": [{
                "symbol": "UNRANKED",
                "ticker_decision_id": "decision:unranked",
                "decision_revision": "ticker-decision.v1:unranked",
                "capital_action": {"action": "BUY", "owned": False},
                "as_of": AS_OF.isoformat(),
            }],
            "opportunity_rank": [],
            "trade_plan": [],
        },
    )
    monkeypatch.setattr(panel_router.panel_owner, "context", lambda **_kwargs: (None, panel))

    result = panel_router.today(
        config=object(),
        option_actions=SimpleNamespace(decision_inbox=lambda **_kwargs: {"items": []}),
    )

    assert result["actions"] == []
    assert result["count"] == 0
    assert result["book_actions"][0]["ticker"] == "UNRANKED"
    assert result["book_actions"][0]["action"] == "NO_TRADE"


def test_today_hides_non_owned_decisions_with_malformed_research_rank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.data_access.types import DataStatus, PanelData
    from app.routers import panel as panel_router

    panel = PanelData(
        status=DataStatus(True, "loaded", "test"),
        tables={
            "ticker_decisions": [{
                "symbol": "BADRANK",
                "ticker_decision_id": "decision:bad-rank",
                "decision_revision": "ticker-decision.v1:bad-rank",
                "opportunity_episode_id": "episode:bad-rank",
                "capital_action": {"action": "BUY", "owned": False},
                "as_of": AS_OF.isoformat(),
            }],
            "opportunity_rank": [{
                "ticker": "BADRANK",
                "decision_revision": "ticker-decision.v1:bad-rank",
                "opportunity_episode_id": "episode:bad-rank",
                "research_rank": "0",
            }],
            "trade_plan": [],
        },
    )
    monkeypatch.setattr(panel_router.panel_owner, "context", lambda **_kwargs: (None, panel))

    result = panel_router.today(
        config=object(),
        option_actions=SimpleNamespace(decision_inbox=lambda **_kwargs: {"items": []}),
    )

    assert result["actions"] == []
    assert result["count"] == 0
    assert result["missing_plan_count"] == 1
    assert result["book_actions"][0]["research_rank"] is None
    assert result["book_actions"][0]["action"] == "NO_TRADE"


def test_today_plan_validation_count_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.data_access import loaders as loaders_owner
    from app.routers import panel as panel_router

    def fake_load(_config, table_names, **_options):
        return {
            name: [{
                "ticker": "FAIL",
                "ticker_decision_id": "decision:fail",
                "decision_revision": "revision:fail",
                "opportunity_episode_id": "episode:fail",
                "capital_action": {"owned": False},
                "opportunity_rank": None,
                "trade_plan": {"present": True},
                "missing_plan_count": 0,
            }] if name == "today_ticker_actions" else []
            for name in table_names
        }, {
            "database": "postgresql",
            "available_model_count": len(table_names),
            "unavailable_models": [],
        }

    def fail_authority(_config, **_limits):
        raise RuntimeError("statement timeout")

    monkeypatch.setattr(loaders_owner, "load_postgres_tables", fake_load)
    monkeypatch.setattr(loaders_owner, "_load_today_authority", fail_authority)

    panel = loaders_owner.load_panel_scope_data(object(), "today")
    monkeypatch.setattr(panel_router.panel_owner, "context", lambda **_kwargs: (None, panel))
    response = panel_router.today(
        config=object(),
        option_actions=SimpleNamespace(decision_inbox=lambda **_kwargs: {"items": []}),
    )

    assert panel.status.ready is False
    assert panel.status.source == "postgresql-error"
    assert "Today authority unavailable" in panel.status.message
    assert all(panel.rows(name) == [] for name in panel.tables)
    assert set(panel.metadata["table_counts"].values()) == {0}
    assert response["status"]["ready"] is False
    assert response["actions"] == []
    assert [row["action"] for row in response["book_actions"]] == ["CASH"]
