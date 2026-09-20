"""Seeded local release smoke: never opens the configured production database."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import pytest

from conftest import typed_config
from investment_panel.api import dependencies
from investment_panel.api.main import app
from investment_panel.application.read_models import loaders as loaders_owner
from investment_panel.application.read_models import panel_snapshot as panel_owner
from investment_panel.application.read_models.types import DataStatus, PanelData
from investment_panel.domain.panel import PANEL_SCOPE_TABLES
from investment_panel.infrastructure.postgres.migrations import HEAD_REVISION
from scripts import verify_workstation as check


class _Response:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        assert len(self.content) <= limit
        return self.content


def test_seeded_local_api_passes_release_smoke_without_production_database(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = "a" * 40
    monkeypatch.setenv("MARKET_BACKEND_COMMIT", expected)
    monkeypatch.setenv("MARKET_FRONTEND_BUILD", expected)
    monkeypatch.setenv("MARKET_SCHEDULER_RELEASE", expected)
    config = typed_config(status_dir=tmp_path / "status")
    tables = {name: [] for name in PANEL_SCOPE_TABLES["today"]}
    tables["ticker_decisions"] = [{
        "ticker": "ACME", "ticker_decision_id": "seed-acme", "decision_revision": "seed.v1",
        "policy_version": "risk-policy.v2", "as_of": "2026-09-20T13:00:00Z",
        "capital_action": {"ticker": "ACME", "action": "BUY", "rationale": "Seeded local decision.",
                           "price_condition": "100-105", "catalyst": "earnings", "owned": False},
    }]
    tables["daily_brief"] = [{"stable_key": "seed-brief", "category": "decide_now", "headline": "Seeded brief"}]
    panel = PanelData(
        status=DataStatus(True, "seeded local PostgreSQL-shaped data", "fixture"),
        tables=tables,
        metadata={"schema_revision": HEAD_REVISION, "expected_schema_revision": HEAD_REVISION},
    )
    panel_owner.invalidate_context_cache()
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_config, lambda: config)
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_options_history,
                        lambda: SimpleNamespace(health=lambda **_kwargs: {"available": True}))
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_options_research,
                        lambda: SimpleNamespace(decision_inbox=lambda **_kwargs: {"items": []}))
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_research_workbench,
                        lambda: SimpleNamespace(action_items=lambda **_kwargs: []))
    monkeypatch.setattr(panel_owner, "load_config", lambda: config)
    monkeypatch.setattr(loaders_owner, "load_panel_data", lambda *_args, **_kwargs: panel)
    monkeypatch.setattr(loaders_owner, "load_panel_scope_data", lambda *_args, **_kwargs: panel)
    client = TestClient(app)

    def local_urlopen(request, *, timeout: float):
        assert timeout == 5
        parsed = urlsplit(request.full_url)
        response = client.get(parsed.path + (f"?{parsed.query}" if parsed.query else ""))
        assert response.status_code == 200
        return _Response(response.content)

    monkeypatch.setattr(check, "urlopen", local_urlopen)
    report = check.verify("http://fixture.local", timeout=5, expected_commit=expected,
                          expected_schema=HEAD_REVISION, release_candidate=True)
    today = client.get("/api/today").json()
    snapshot = client.get("/api/panel-snapshot?scope=today").json()

    assert report["status"] == "pass"
    assert today["actions"] == []
    assert today["book_actions"][0]["action"] == "NO_TRADE"
    assert snapshot["tables"]["ticker_decisions"]["count"] == 1
    assert snapshot["tables"]["daily_brief"]["rows"][0]["stable_key"] == "seed-brief"
    panel_owner.invalidate_context_cache()
