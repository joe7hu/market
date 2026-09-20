from fastapi.testclient import TestClient

from investment_panel.api import dependencies
from investment_panel.api.main import app
from investment_panel.api.routers import paper
from conftest import typed_config


def test_research_overview_preserves_misconfigured_status_for_list_evidence(monkeypatch):
    class PaperRepository:
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
