from __future__ import annotations

from fastapi.testclient import TestClient

from app import dependencies
from app.main import app


def test_storage_health_is_read_only_and_reports_capacity() -> None:
    expected = {
        "local": {"free_bytes": 40 * 1024**3},
        "nas": {"free_bytes": 100 * 1024**3},
        "archive_verification_failures": 0,
        "active_reclamation": [],
        "full_history_collection_allowed": True,
    }
    class StubStorage:
        def health(self):
            return expected

    app.dependency_overrides[dependencies.get_storage_archive_service] = StubStorage

    try:
        with TestClient(app) as client:
            response = client.get("/api/health/storage")
    finally:
        app.dependency_overrides.pop(dependencies.get_storage_archive_service, None)

    assert response.status_code == 200
    assert response.json() == expected


def test_storage_health_fails_explicitly_when_storage_is_unavailable() -> None:
    class StubStorage:
        def health(self):
            raise OSError("NAS unavailable")

    app.dependency_overrides[dependencies.get_storage_archive_service] = StubStorage
    try:
        with TestClient(app) as client:
            response = client.get("/api/health/storage")
    finally:
        app.dependency_overrides.pop(dependencies.get_storage_archive_service, None)

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert "NAS unavailable" in response.json()["message"]
