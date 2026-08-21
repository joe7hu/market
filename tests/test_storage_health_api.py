from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.routers import storage as storage_router


def test_storage_health_is_read_only_and_reports_capacity(monkeypatch) -> None:
    expected = {
        "local": {"free_bytes": 40 * 1024**3},
        "nas": {"free_bytes": 100 * 1024**3},
        "archive_verification_failures": 0,
        "active_reclamation": [],
        "full_history_collection_allowed": True,
    }
    monkeypatch.setattr(storage_router, "storage_health_owner", lambda _config: expected)

    with TestClient(app) as client:
        response = client.get("/api/health/storage")

    assert response.status_code == 200
    assert response.json() == expected


def test_storage_health_fails_explicitly_when_storage_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(storage_router, "storage_health_owner", lambda _config: (_ for _ in ()).throw(OSError("NAS unavailable")))

    with TestClient(app) as client:
        response = client.get("/api/health/storage")

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert "NAS unavailable" in response.json()["message"]
