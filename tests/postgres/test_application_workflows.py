from dataclasses import replace
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg
import pytest

from investment_panel.api import dependencies
from investment_panel.api.routers.sources import router
from conftest import typed_config
from investment_panel.infrastructure.postgres.jobs import JobRepository
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.jobs import update_phase2_sources


def test_application_login_ingests_provider_payload_and_serves_source_status(
    application_postgres_dsn: str, tmp_path, monkeypatch,
) -> None:
    config = typed_config(application_postgres_dsn)
    config = replace(config, nas=replace(config.nas, market_dir=tmp_path))
    monkeypatch.setattr(update_phase2_sources, "load_config", lambda _: config)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    try:
        with runtime.read() as connection:
            identity = connection.execute(
                "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
            assert identity == {"current_user": "market_app", "rolsuper": False, "rolbypassrls": False}
        jobs = JobRepository(runtime)
        job = jobs.start("update_phase2_sources")
        result = update_phase2_sources.run(
            runtime=runtime, source_ids=["treasury", "fred"], payloads={"treasury": {
                "xml": "<feed><entry><properties><NEW_DATE>2026-09-02</NEW_DATE>"
                       "<BC_10YEAR>4.0</BC_10YEAR></properties></entry></feed>",
                "retrieved_at": datetime.now(UTC).isoformat(),
            }},
        )
        assert result["sources"]["treasury"]["stored"] == 1
        assert result["sources"]["fred"]["stored"] == 0
        assert result["sources"]["fred"]["status"] == "MISSING_SOURCE"
        assert jobs.finish(job["id"], "succeeded", summary=result)["status"] == "succeeded"
        with runtime.read() as connection:
            fact = connection.execute(
                "SELECT value, ingest_run_id::text AS run_id, payload_id "
                "FROM raw.market_observation WHERE source_id = 'treasury'"
            ).fetchone()
            assert float(fact["value"]) == 4.0
            assert fact["run_id"] == result["sources"]["treasury"]["run_id"]
            assert fact["payload_id"] is not None
        with runtime.transaction() as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection.transaction():
                    connection.execute("SET ROLE market_migrator")
    finally:
        runtime.close()

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[dependencies.get_config] = lambda: config
    with TestClient(application) as client:
        response = client.get("/api/source-catalog")
    assert response.status_code == 200
    rows = {row["source_id"]: row for row in response.json()["rows"]}
    assert rows["treasury"]["run_status"] == "succeeded"
    assert rows["fred"]["run_status"] == "skipped"
