from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from investment_panel.api import dependencies
from investment_panel.api.main import app
from investment_panel.infrastructure.postgres.research_summary import evaluation_summary


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_runtime, lambda: object())
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_config, lambda: object())
    return TestClient(app)


def test_research_summary_returns_explicit_unknowns(client, monkeypatch):
    expected = {
        "as_of": datetime(2026, 9, 1, tzinfo=UTC), "paper_only": True,
        "strategy_count": 0, "strategies": [], "ideas": [],
        "review_activity": {"window_days": 30, "total": 3, "acknowledged": 3,
            "completed": 0, "rated": 0, "helpful": 0, "not_helpful": 0, "helpful_rate": None},
    }
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_agent_actions,
        lambda: SimpleNamespace(research_results=lambda: expected))
    response = client.get("/api/research/summary")
    assert response.status_code == 200
    assert response.json()["paper_only"] is True
    assert response.json()["review_activity"]["helpful_rate"] is None
    assert response.json()["review_activity"]["rated"] == 0


def test_research_unavailable_is_not_an_empty_success(client, monkeypatch):
    def unavailable(*_args):
        raise RuntimeError("database detail must not reach the browser")
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_agent_actions,
        lambda: SimpleNamespace(research_results=unavailable))
    response = client.get("/api/research/summary")
    assert response.status_code == 503
    assert "database detail" not in response.text
    assert "could not be loaded" in response.text


def test_research_contract_preserves_options_progress_and_unknown_returns(client, monkeypatch):
    stage = evaluation_summary({
        "evaluation_type": "shadow", "verdict": "collecting_data",
        "evidence": {"options_comparison_version": "options-independent-comparison-v1"},
        "metrics": {"proposed": {"sample_size": 7}, "comparison_denominator": 12,
            "unmatched_episodes": 2, "comparison_window_complete": False, "comparison_lower_95": 0.05},
    })
    expected = {
        "as_of": datetime(2026, 9, 1, tzinfo=UTC), "paper_only": True, "strategy_count": 1,
        "strategies": [{"strategy_revision_id": 2, "strategy_key": "options-candidate", "revision": 1,
            "name": "Options candidate", "status": "testing", "automatic_paper_tuning": True,
            "evaluations": [stage], "next_observation": "Complete the observation window."}],
        "review_activity": {"window_days": 30, "total": 0, "acknowledged": 0, "completed": 0,
            "rated": 0, "helpful": 0, "not_helpful": 0},
    }
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_agent_actions,
        lambda: SimpleNamespace(research_results=lambda: expected))
    response = client.get("/api/research/summary")
    assert response.status_code == 200
    observed = response.json()["strategies"][0]["evaluations"][0]
    assert observed["independent_sample_count"] == 7
    assert observed["net_return_lower_bound"] is None
    assert observed["brier_score"] is None
    assert observed["evidence_basis"] == "independent_options_shadow"
    assert observed["unmatched_episodes"] == 2
    assert observed["comparison_window_complete"] is False


def test_explicit_usefulness_accepts_false_and_keeps_authorized_boundary(client, monkeypatch):
    calls = []
    item_id = str(uuid4())
    def save(identifier, *, useful):
        calls.append((identifier, useful))
        return {"id": identifier, "useful": useful, "usefulness_updated_at": datetime.now(UTC)}
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_options_research,
        lambda: SimpleNamespace(set_decision_inbox_usefulness=save))
    response = client.post(f"/api/decision-inbox/{item_id}/usefulness", json={"useful": False})
    assert response.status_code == 200
    assert response.json()["useful"] is False
    assert calls == [(item_id, False)]
    denied = client.post(f"/api/decision-inbox/{item_id}/usefulness", json={"useful": True}, headers={"host": "untrusted.example"})
    assert denied.status_code == 403
    assert calls == [(item_id, False)]


@pytest.mark.parametrize("payload", [{}, {"useful": None}, {"useful": "false"}, {"useful": 0}])
def test_usefulness_requires_an_explicit_boolean(client, monkeypatch, payload):
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_options_research, lambda: object())
    assert client.post(f"/api/decision-inbox/{uuid4()}/usefulness", json=payload).status_code == 422


def test_usefulness_missing_item_returns_not_found(client, monkeypatch):
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_options_research,
        lambda: SimpleNamespace(set_decision_inbox_usefulness=lambda *_args, **_kwargs: None))
    assert client.post(f"/api/decision-inbox/{uuid4()}/usefulness", json={"useful": True}).status_code == 404
