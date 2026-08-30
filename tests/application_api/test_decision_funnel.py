from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app import dependencies
from app.main import app
from app.routers import system as system_router
from investment_panel.database.ticker_decisions import (
    TickerDecisionRepository,
    decision_funnel_payload,
)


NOW = datetime(2026, 8, 29, 14, tzinfo=UTC)


def test_decision_funnel_keeps_backend_policy_and_blocker_ownership() -> None:
    payload = decision_funnel_payload(
        [{
            "ticker": "AAA",
            "opportunity_episode": {"episode_id": "episode:aaa"},
            "market_state_publication_id": "market:aaa",
            "expressions": {"STOCK": {"availability_status": "available"}},
            "portfolio_impacts": {"STOCK": {"availability_status": "available"}},
            "resolution": {"eligibility": "ACTIONABLE", "action": "BUY", "blockers": []},
            "point_in_time_facts_available": True,
            "point_in_time_fact_blockers": [],
            "published_at": NOW,
        }, {
            "ticker": "BBB",
            "opportunity_episode": {"episode_id": "episode:bbb"},
            "market_state_publication_id": "market:bbb",
            "expressions": {"STOCK": {"availability_status": "missing", "blockers": ["quote_missing"]}},
            "portfolio_impacts": {},
            "resolution": {"eligibility": "BLOCKED", "action": "NO_TRADE", "blockers": ["quote_missing", "nav_missing"]},
            "point_in_time_facts_available": True,
            "point_in_time_fact_blockers": [],
            "published_at": NOW,
        }],
        [{"ticker": "AAA", "availability_status": "available"}, {"ticker": "BBB", "availability_status": "not_calibrated", "blockers": ["alpha_oos_evaluation_missing"]}],
        [{"ticker": "AAA", "availability_status": "available", "trade_rank": 1, "ranking_version": "ticker-opportunity-ranking.v1"}, {"ticker": "BBB", "availability_status": "missing", "trade_rank": None, "primary_blocker": "alpha_oos_evaluation_missing"}],
        [{"ticker": "AAA", "availability_status": "available", "eligibility": "ACTIONABLE", "published_at": NOW}, {"ticker": "BBB", "availability_status": "policy_blocked", "eligibility": "BLOCKED", "blockers": ["quote_missing", "nav_missing"]}],
        action_queue_rows=[{
            "ticker": "AAA", "source": "capital_action", "lifecycle_state": "actionable",
            "selected_expression": "STOCK", "trade_plan": {"trade_plan_id": "plan:aaa"},
        }, {
            "ticker": "BBB", "source": "capital_action", "lifecycle_state": "blocked",
            "selected_expression": "CASH", "trade_plan": None, "primary_blocker": "quote_missing",
        }],
        now=NOW,
    )

    assert payload["policy_version"] == "ticker-opportunity-ranking.v1"
    assert payload["actionable"] == 1
    alpha = next(stage for stage in payload["stages"] if stage["stage"] == "qualified_stock_alpha")
    assert alpha["count"] == 1
    assert alpha["top_blockers"][0] == {
        "reason": "alpha_oos_evaluation_missing",
        "count": 1,
        "affected_symbols": ["BBB"],
    }
    assert alpha["owner"] == "strategy-governance"
    assert next(stage for stage in payload["stages"] if stage["stage"] == "action_queue")["count"] == 1


def test_decision_funnel_rejects_noncanonical_facts_and_does_not_alias_trade_plan() -> None:
    payload = decision_funnel_payload(
        [{
            "ticker": "AAA",
            "opportunity_episode": {"episode_id": "nonempty-but-unvalidated"},
            "market_state_publication_id": "nonempty-but-unvalidated",
            "expressions": {"STOCK": {"availability_status": "available"}},
            "portfolio_impacts": {"STOCK": {"availability_status": "available"}},
            "resolution": {"eligibility": "ACTIONABLE", "action": "BUY"},
        }],
        [{"ticker": "AAA", "availability_status": "available"}],
        [{"ticker": "AAA", "availability_status": "available", "trade_rank": 1}],
        [{"ticker": "AAA", "availability_status": "available", "eligibility": "ACTIONABLE"}],
        action_queue_rows=[],
        now=NOW,
    )

    facts = next(stage for stage in payload["stages"] if stage["stage"] == "point_in_time_facts")
    plan = next(stage for stage in payload["stages"] if stage["stage"] == "trade_plan")
    queue = next(stage for stage in payload["stages"] if stage["stage"] == "action_queue")
    assert facts["count"] == 0
    assert plan["count"] == 1
    assert queue["count"] == 0
    assert queue["top_blockers"][0]["reason"] == "action_queue_unavailable"


def test_decision_funnel_api_returns_the_repository_contract(monkeypatch) -> None:
    expected = decision_funnel_payload([], [], [], [], now=NOW)
    monkeypatch.setattr(TickerDecisionRepository, "decision_funnel", lambda self, **_kwargs: expected)
    monkeypatch.setattr(system_router, "today_action_queue", lambda *_args: {"actions": []})
    app.dependency_overrides[dependencies.get_runtime] = lambda: object()
    app.dependency_overrides[dependencies.get_config] = lambda: object()
    app.dependency_overrides[dependencies.get_options_actions] = lambda: object()
    try:
        response = TestClient(app).get("/api/decision-funnel")
    finally:
        app.dependency_overrides.pop(dependencies.get_runtime, None)
        app.dependency_overrides.pop(dependencies.get_config, None)
        app.dependency_overrides.pop(dependencies.get_options_actions, None)

    assert response.status_code == 200
    assert response.json()["policy_version"] == expected["policy_version"]
    assert response.json()["stages"] == expected["stages"]
