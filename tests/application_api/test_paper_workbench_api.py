from fastapi.testclient import TestClient

from investment_panel.api import dependencies
from investment_panel.api.main import app
from conftest import typed_config


def test_paper_and_research_workbench_routes_are_read_only_and_truthful_on_empty_db(
    migrated_postgres_dsn, monkeypatch
):
    monkeypatch.setitem(
        app.dependency_overrides,
        dependencies.get_config,
        lambda: typed_config(migrated_postgres_dsn),
    )
    client = TestClient(app)

    performance = client.get("/api/paper/performance")
    trades = client.get("/api/paper/trades")
    export_csv = client.get("/api/paper/trades/export")
    export_json = client.get("/api/paper/trades/export?format=json")
    research = client.get("/api/research/overview")

    assert performance.status_code == 200
    assert performance.json()["net_pnl"] == 0
    assert performance.json()["nav"] is None
    assert trades.status_code == 200
    assert trades.json()["rows"] == []
    assert export_csv.status_code == 200
    assert "paper_order_id" in export_csv.text
    assert export_json.status_code == 200
    assert export_json.json()["rows"] == []
    assert research.status_code == 200
    assert research.json()["paper_only"] is True
    assert research.json()["strategy_lane"]["status"] == "no_paper_fills"
