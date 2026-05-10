from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_api_routes_return_json() -> None:
    client = TestClient(app)
    for path in [
        "/api/status",
        "/api/dashboard",
        "/api/signals",
        "/api/candidates",
        "/api/portfolio",
        "/api/theses",
        "/api/trader-twins",
        "/api/catalysts",
        "/api/fundamentals",
        "/api/disclosures",
        "/api/source-health",
        "/api/settings",
        "/api/tickers/TSLA",
    ]:
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
