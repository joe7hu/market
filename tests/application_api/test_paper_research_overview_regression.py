from fastapi.testclient import TestClient

from investment_panel.api import dependencies
from investment_panel.api.main import app
from investment_panel.api.routers import paper
from conftest import typed_config


def test_research_overview_preserves_misconfigured_status_for_list_evidence(monkeypatch):
    class PaperRepository:
        def observation_progress(self):
            return {"counts": {}, "active": 0, "management_status": "idle"}

        def performance(self):
            return {"as_of": None, "source_watermark": None, "counts": {"filled_orders": 0}, "missing_evidence_reasons": []}

    class ResearchRepository:
        def strategy_revisions(self, *, limit):
            return {"rows": [
                {"authority_group": "options-radar-core", "status": "active", "strategy_revision_id": 1, "evaluations": []},
                {"authority_group": "options-radar-core", "status": "candidate", "strategy_revision_id": 2, "evaluations": [{"evaluation_type": "walk_forward", "verdict": "implementation_version_mismatch", "evidence": [], "metrics": []}]},
            ]}

        def forecast_claims(self, *, limit):
            return {"quality": {}}

        def events(self, *, limit):
            return []

        def diagnostics(self):
            return {}

        def action_items(self, *, limit):
            return []

    monkeypatch.setitem(app.dependency_overrides, dependencies.get_config, typed_config)
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_paper_workbench, PaperRepository)
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_research_workbench, ResearchRepository)
    monkeypatch.setattr(paper.continuous_owner, "overview", lambda config: {})

    response = TestClient(app).get("/api/research/overview")

    assert response.status_code == 200
    assert response.json()["strategy_lane"]["status"] == "misconfigured"
    assert "implementation_version_mismatch" in response.json()["strategy_lane"]["blockers"]


def test_research_collection_is_visible_without_becoming_validation_evidence(monkeypatch):
    from types import SimpleNamespace
    collection = {"active": 18, "completed": 1, "management_status": "collecting"}
    performance = {"as_of": None, "source_watermark": None, "counts": {"filled_orders": 0}, "missing_evidence_reasons": []}
    repository = SimpleNamespace(performance=lambda: performance, observation_progress=lambda: collection)
    research = SimpleNamespace(strategy_revisions=lambda **kwargs: {"rows": [
        {"status": "active", "strategy_key": "core", "authority_group": "options-radar-core", "evaluations": []}]},
        forecast_claims=lambda **kwargs: {"quality": {}}, events=lambda **kwargs: [],
        diagnostics=lambda: {}, action_items=lambda **kwargs: [])
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_config, typed_config)
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_paper_workbench, lambda: repository)
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_research_workbench, lambda: research)
    monkeypatch.setattr(paper.continuous_owner, "overview", lambda config: {})
    client = TestClient(app)
    lane = client.get("/api/research/overview").json()["strategy_lane"]
    assert lane["status"] == "collecting_outcomes"
    assert lane["progress"]["paper_completed"] == 0
    assert lane["evidence_counts"]["filled_orders"] == 0
    collection["management_status"] = "overdue"
    lane = client.get("/api/research/overview").json()["strategy_lane"]
    assert lane["status"] == "collection_stalled"
    assert lane["operational_health"] == "collection_stalled"
