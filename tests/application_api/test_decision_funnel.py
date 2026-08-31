from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from app import dependencies
from app.main import app
from investment_panel.core.decision import trade_expression_identity
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import API_PROFILE
from investment_panel.database.ticker_decisions import (
    TickerDecisionRepository,
    decision_funnel_payload,
)
import investment_panel.database.ticker_decisions as ticker_decisions


NOW = datetime(2026, 8, 29, 14, tzinfo=UTC)


def _valid_compact_row(ticker: str = "AAA") -> dict[str, Any]:
    expression = {
        "kind": "STOCK", "ticker": ticker, "horizon": "FUNDAMENTAL",
        "thesis_revision": f"thesis:{ticker.lower()}", "stance": "BULLISH",
        "status": "eligible", "availability_status": "available", "blockers": [],
        "selected": False, "rationale": "Validated compact fixture.",
    }
    decision_revision = f"decision:{ticker.lower()}"
    policy_version = "risk-policy.v2:test"
    opportunity_episode_id = f"episode:{ticker.lower()}"
    input_lineage = [{
        "field": "price", "source_id": "compact-fixture",
        "source_version": "v1", "available_at": NOW,
        "opportunity_episode_id": opportunity_episode_id,
        "decision_revision": decision_revision,
        "policy_version": policy_version, "cutoff": NOW,
    }]
    return {
        "ticker": ticker, "as_of": NOW, "published_at": NOW,
        "decision_revision": decision_revision,
        "policy_version": policy_version,
        "opportunity_episode_id": opportunity_episode_id,
        "opportunity_episode": {
            "episode_id": opportunity_episode_id,
            "ticker": ticker,
            "decision_revision": decision_revision,
            "policy_version": policy_version,
            "cutoff": NOW,
            "input_lineage": [dict(input_lineage[0])],
            "expressions": {"STOCK": dict(expression)},
            "selected_expression": None,
            "thesis_identity": f"ticker:{ticker}",
            "first_seen_at": NOW,
            "last_updated_at": NOW,
            "status": "DISCOVERED",
            "horizon": "FUNDAMENTAL",
            "current_revision": decision_revision,
        },
        "opportunity_cutoff_match": True,
        "opportunity_expressions_match": True,
        "opportunity_selected_expression_match": True,
        "selected_expression": None,
        "stock_expression": expression,
        "stock_portfolio_impact": {
            "impact_id": f"impact:{ticker.lower()}", "ticker": ticker,
            "opportunity_episode_id": f"episode:{ticker.lower()}",
            "expression_kind": "STOCK", "expression_identity": trade_expression_identity(expression),
            "decision_revision": f"decision:{ticker.lower()}",
            "risk_policy_version": "risk-policy.v2:test",
            "market_snapshot_id": f"market:{ticker.lower()}",
            "market_state_publication_id": None, "cutoff": NOW,
            "input_lineage": [dict(input_lineage[0])],
            "availability": "unavailable", "availability_status": "missing",
            "blockers": ["portfolio_context_missing"],
        },
        "resolution": {
            "eligibility": "BLOCKED", "authorization_mode": "NONE",
            "data_quality": "INCOMPLETE", "action": "NO_TRADE",
            "primary_blocker": "portfolio_context_missing",
            "blockers": ["portfolio_context_missing"],
            "decision_revision": f"decision:{ticker.lower()}",
            "policy_version": "risk-policy.v2:test", "ticker": ticker,
        },
        "market_state_publication_id": None,
    }


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


def test_decision_funnel_derives_action_queue_from_compact_authority(monkeypatch) -> None:
    repository = TickerDecisionRepository(object())
    monkeypatch.setattr(repository, "_current_funnel_publication_rows", lambda: ([], [], []))
    monkeypatch.setattr(repository, "_current_funnel_rows", lambda **_kwargs: [_valid_compact_row()])
    monkeypatch.setattr(
        ticker_decisions,
        "_funnel_action_queue_row",
        lambda **_kwargs: {
            "ticker": "AAA", "source": "capital_action",
            "lifecycle_state": "actionable", "selected_expression": "STOCK",
            "trade_plan": {"trade_plan_id": "plan:aaa"},
        },
    )

    payload = repository.decision_funnel(now=NOW)

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
    app.dependency_overrides[dependencies.get_runtime] = lambda: object()
    try:
        response = TestClient(app).get("/api/decision-funnel")
    finally:
        app.dependency_overrides.pop(dependencies.get_runtime, None)

    assert response.status_code == 200
    assert response.json()["policy_version"] == expected["policy_version"]
    assert response.json()["stages"] == expected["stages"]


def test_decision_funnel_uses_compact_current_rows(monkeypatch) -> None:
    repository = TickerDecisionRepository(object())

    def full_publication_rows_must_not_run(*_args, **_kwargs):
        raise AssertionError("decision funnel must not hydrate full publication rows")

    monkeypatch.setattr(AnalysisRepository, "publication_rows", full_publication_rows_must_not_run)
    monkeypatch.setattr(
        repository,
        "_current_funnel_publication_rows",
        lambda: (
            [{"ticker": "AAA", "availability_status": "available"}],
            [{
                "ticker": "AAA", "availability_status": "available",
                "trade_rank": 1, "ranking_version": "ranking:test",
            }],
            [{
                "ticker": "AAA", "availability_status": "available",
                "eligibility": "ACTIONABLE",
            }],
        ),
    )

    def broad_read_must_not_run(**_kwargs):
        raise AssertionError("decision funnel must not hydrate full ticker decisions")

    monkeypatch.setattr(repository, "_current_decision_rows", broad_read_must_not_run)
    monkeypatch.setattr(
        repository,
        "_current_funnel_rows",
        lambda **_kwargs: [_valid_compact_row()],
        raising=False,
    )

    payload = repository.decision_funnel(now=NOW)

    assert payload["total"] == 1
    facts = next(stage for stage in payload["stages"] if stage["stage"] == "point_in_time_facts")
    expression = next(stage for stage in payload["stages"] if stage["stage"] == "stock_expression")
    assert facts["count"] == 1
    assert expression["count"] == 1
    assert payload["policy_version"] == "ranking:test"
    assert all(
        next(stage for stage in payload["stages"] if stage["stage"] == name)["count"] == 1
        for name in ("qualified_stock_alpha", "trade_rank", "trade_plan")
    )


def test_decision_funnel_fails_closed_for_malformed_compact_artifacts(monkeypatch) -> None:
    repository = TickerDecisionRepository(object())
    monkeypatch.setattr(repository, "_current_funnel_publication_rows", lambda: ([], [], []))
    rows = [_valid_compact_row(ticker) for ticker in ("EXPR", "IMPACT", "RESOLUTION")]
    rows[0]["stock_expression"] = {"availability_status": "available"}
    rows[1]["stock_portfolio_impact"] = {"availability_status": "available"}
    rows[2]["resolution"] = {"eligibility": "ACTIONABLE", "action": "BUY"}
    monkeypatch.setattr(repository, "_current_funnel_rows", lambda **_kwargs: rows)

    payload = repository.decision_funnel(now=NOW)

    facts = next(stage for stage in payload["stages"] if stage["stage"] == "point_in_time_facts")
    expression = next(stage for stage in payload["stages"] if stage["stage"] == "stock_expression")
    impact = next(stage for stage in payload["stages"] if stage["stage"] == "portfolio_impact")
    resolution = next(stage for stage in payload["stages"] if stage["stage"] == "decision_resolution")
    assert facts["count"] == 0
    assert facts["top_blockers"][0]["reason"] == "ticker_decision_contract_invalid"
    assert expression["count"] == 2
    assert expression["top_blockers"][0]["reason"] == "stock_expression_invalid"
    assert impact["count"] == 0
    assert any(item["reason"] == "stock_portfolio_impact_invalid" for item in impact["top_blockers"])
    assert resolution["count"] == 0
    assert any(item["reason"] == "decision_resolution_invalid" for item in resolution["top_blockers"])


def test_decision_funnel_fails_closed_for_corrupt_opportunity_episodes(monkeypatch) -> None:
    repository = TickerDecisionRepository(object())
    monkeypatch.setattr(repository, "_current_funnel_publication_rows", lambda: ([], [], []))
    rows = [
        _valid_compact_row(ticker)
        for ticker in (
            "VALID", "NOEXP", "SELECTED", "HORIZON", "SELECTEDTERMS",
            "LIFECYCLE", "DRIFT",
        )
    ]
    rows[1]["opportunity_episode"].pop("expressions")
    rows[2]["selected_expression"] = {"kind": "STOCK", "horizon": "FUNDAMENTAL"}
    for index in (3, 4):
        episode_expression = rows[index]["opportunity_episode"]["expressions"]["STOCK"]
        episode_expression["selected"] = True
        rows[index]["stock_expression"]["selected"] = True
        rows[index]["opportunity_episode"]["selected_expression"] = dict(episode_expression)
        rows[index]["selected_expression"] = {"kind": "STOCK", "horizon": "FUNDAMENTAL"}
    rows[3]["selected_expression"]["horizon"] = "TACTICAL"
    rows[4]["opportunity_selected_expression_match"] = False
    rows[5]["opportunity_episode"].update({"status": "CLOSED", "closed_reason": None})
    rows[6]["opportunity_episode"]["expressions"]["STOCK"]["rationale"] = (
        "Corrupt expression drift."
    )
    rows[6]["opportunity_expressions_match"] = False
    monkeypatch.setattr(repository, "_current_funnel_rows", lambda **_kwargs: rows)

    payload = repository.decision_funnel(now=NOW)

    facts = next(stage for stage in payload["stages"] if stage["stage"] == "point_in_time_facts")
    assert facts["count"] == 1
    assert facts["top_blockers"][0] == {
        "reason": "ticker_decision_contract_invalid",
        "count": 6,
        "affected_symbols": [
            "DRIFT", "HORIZON", "LIFECYCLE", "NOEXP", "SELECTED", "SELECTEDTERMS",
        ],
    }


def test_decision_funnel_requires_exact_nonempty_impact_lineage(monkeypatch) -> None:
    repository = TickerDecisionRepository(object())
    monkeypatch.setattr(repository, "_current_funnel_publication_rows", lambda: ([], [], []))
    rows = [_valid_compact_row(ticker) for ticker in ("EMPTY", "MISSING", "DRIFT")]
    rows[0]["opportunity_episode"]["input_lineage"] = []
    rows[0]["stock_portfolio_impact"]["input_lineage"] = []
    rows[1]["stock_portfolio_impact"]["input_lineage"] = []
    rows[2]["stock_portfolio_impact"]["input_lineage"][0]["source_id"] = "wrong-source"
    monkeypatch.setattr(repository, "_current_funnel_rows", lambda **_kwargs: rows)

    payload = repository.decision_funnel(now=NOW)

    facts = next(stage for stage in payload["stages"] if stage["stage"] == "point_in_time_facts")
    impact = next(stage for stage in payload["stages"] if stage["stage"] == "portfolio_impact")
    assert facts["count"] == 0
    assert facts["top_blockers"][0] == {
        "reason": "ticker_decision_contract_invalid",
        "count": 3,
        "affected_symbols": ["DRIFT", "EMPTY", "MISSING"],
    }
    assert impact["count"] == 0
    assert impact["top_blockers"][0] == {
        "reason": "stock_portfolio_impact_invalid",
        "count": 3,
        "affected_symbols": ["DRIFT", "EMPTY", "MISSING"],
    }


def test_decision_funnel_compact_lineage_validation_fails_closed(monkeypatch) -> None:
    repository = TickerDecisionRepository(object())
    monkeypatch.setattr(repository, "_current_funnel_publication_rows", lambda: ([], [], []))
    row = _valid_compact_row()
    row["impact_lineage_match"] = True
    row["opportunity_episode"]["input_lineage"].append(
        dict(row["opportunity_episode"]["input_lineage"][0])
    )
    monkeypatch.setattr(repository, "_current_funnel_rows", lambda **_kwargs: [row])

    payload = repository.decision_funnel(now=NOW)

    facts = next(stage for stage in payload["stages"] if stage["stage"] == "point_in_time_facts")
    assert facts["count"] == 0
    assert facts["top_blockers"][0]["reason"] == "ticker_decision_contract_invalid"


def test_decision_funnel_loads_each_exact_market_publication_once(monkeypatch) -> None:
    repository = TickerDecisionRepository(object())
    monkeypatch.setattr(repository, "_current_funnel_publication_rows", lambda: ([], [], []))
    publication_id = "market-publication:shared"
    market_cutoff = datetime(2026, 8, 29, 13, 58, tzinfo=UTC)
    publication = {
        "publication_id": publication_id,
        "publication_scope": "market",
        "publication_status": "published",
        "input_cutoff": market_cutoff,
        "published_at": datetime(2026, 8, 29, 13, 59, tzinfo=UTC),
        "source_lineage": [],
        "models": {"market_state_snapshot": [{
            "snapshot_id": "market-state:shared",
            "publication_id": publication_id,
            "as_of": market_cutoff,
            "input_cutoff": market_cutoff,
        }]},
    }
    loads: list[tuple[str, str]] = []

    def publication_by_id(_self, scope: str, exact_id: str):
        loads.append((scope, exact_id))
        return publication

    def compact_rows(**_kwargs):
        rows = [_valid_compact_row(ticker) for ticker in ("AAA", "BBB")]
        for row in rows:
            row["market_state_publication_id"] = publication_id
            row["stock_portfolio_impact"]["market_state_publication_id"] = publication_id
            row["stock_portfolio_impact"]["market_snapshot_id"] = "market-state:shared"
        rows[1]["stock_portfolio_impact"]["market_snapshot_id"] = "market-state:wrong"
        return rows

    monkeypatch.setattr(AnalysisRepository, "publication_by_id", publication_by_id)
    monkeypatch.setattr(repository, "_current_funnel_rows", compact_rows)

    payload = repository.decision_funnel(now=NOW)

    assert payload["total"] == 2
    assert loads == [("market", publication_id)]
    facts = next(stage for stage in payload["stages"] if stage["stage"] == "point_in_time_facts")
    impact = next(stage for stage in payload["stages"] if stage["stage"] == "portfolio_impact")
    assert facts["count"] == 1
    assert any(item["reason"] == "stock_portfolio_impact_invalid" for item in impact["top_blockers"])


def test_current_funnel_query_does_not_select_full_market_snapshot() -> None:
    captured: dict[str, str] = {}

    class Result:
        @staticmethod
        def fetchall() -> list[dict[str, object]]:
            return []

    class Connection:
        @staticmethod
        def execute(query: str, _parameters: list[datetime]) -> Result:
            captured["query"] = query
            return Result()

    class Runtime:
        @contextmanager
        def read(self):
            yield Connection()

    rows = TickerDecisionRepository(Runtime())._current_funnel_rows(reference=NOW)

    assert rows == []
    assert "market_state_snapshot" not in captured["query"]
    assert "THEN decision.opportunity_episode END AS opportunity_episode" in captured["query"]
    assert "AS opportunity_cutoff_match" in captured["query"]
    assert "AS opportunity_expressions_match" in captured["query"]
    assert "AS opportunity_selected_expression_match" in captured["query"]
    assert "octet_length(decision.opportunity_episode::text) <= 262144" in captured["query"]
    assert "decision.expressions," not in captured["query"]
    assert "AS has_valid_opportunity_lineage" not in captured["query"]
    assert "jsonb_array_elements" not in captured["query"]


def test_current_funnel_publication_query_projects_only_required_fields() -> None:
    captured: dict[str, object] = {}

    class Result:
        @staticmethod
        def fetchone() -> dict[str, bool]:
            return {"exists": True}

        @staticmethod
        def fetchall() -> list[dict[str, object]]:
            base = {
                "availability_status": "available",
                "blockers": [],
                "trade_rank": None,
                "primary_blocker": None,
                "trade_rank_unavailable_reason": None,
                "eligibility": None,
                "ranking_version": None,
                "publication_id": "publication:compact",
                "published_at": NOW,
            }
            return [
                {**base, "model_name": "alpha_signal", "ticker": "AAA"},
                {
                    **base, "model_name": "opportunity_rank", "ticker": "AAA",
                    "trade_rank": 1, "ranking_version": "ranking:test",
                },
                {
                    **base, "model_name": "trade_plan", "ticker": "AAA",
                    "eligibility": "ACTIONABLE",
                },
            ]

    class Connection:
        @staticmethod
        def execute(query: str, parameters: list[object]) -> Result:
            captured.setdefault("queries", []).append(query)
            captured.setdefault("parameters", []).append(parameters)
            return Result()

    class Runtime:
        @contextmanager
        def snapshot(self, profile):
            captured["profile"] = profile
            yield Connection()

    alpha_rows, rank_rows, plan_rows = (
        TickerDecisionRepository(Runtime())._current_funnel_publication_rows()
    )

    assert captured["profile"] is API_PROFILE
    assert len(captured["queries"]) == 2
    assert "SELECT EXISTS" in str(captured["queries"][0])
    assert "payload.payload->>'availability_status'" in str(captured["queries"][1])
    assert "jsonb_to_record" not in str(captured["queries"][1])
    assert "UNION" not in str(captured["queries"][1])
    assert alpha_rows[0]["ticker"] == rank_rows[0]["ticker"] == plan_rows[0]["ticker"] == "AAA"
    assert rank_rows[0]["trade_rank"] == 1
    assert plan_rows[0]["eligibility"] == "ACTIONABLE"
    assert set(plan_rows[0]) == {
        "ticker", "availability_status", "blockers", "eligibility",
        "publication_id", "publication_published_at",
    }
