from __future__ import annotations

from datetime import UTC, datetime, timedelta

from investment_panel.core.db import db, init_db
from investment_panel.core import refresh_jobs


def test_refresh_job_can_be_started_and_completed(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"
    monkeypatch.setitem(refresh_jobs.ALLOWLIST, "unit_refresh", lambda _config_path: {"ok": True, "rows": 3})

    job = refresh_jobs.start_refresh_job("unit_refresh", db_path)
    assert job["status"] == "running"

    result = refresh_jobs.execute_refresh_job(job["id"], "unit_refresh", db_path, "config.yaml")
    assert result["status"] == "succeeded"

    rows = refresh_jobs.refresh_job_rows(db_path)
    assert rows[0]["id"] == job["id"]
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["summary"] == {"ok": True, "rows": 3}


def test_refresh_job_records_failure_without_reraising(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"

    def fail(_config_path):
        raise RuntimeError("provider unavailable")

    monkeypatch.setitem(refresh_jobs.ALLOWLIST, "unit_refresh", fail)
    job = refresh_jobs.start_refresh_job("unit_refresh", db_path)

    result = refresh_jobs.execute_refresh_job(job["id"], "unit_refresh", db_path, "config.yaml", raise_on_error=False)
    assert result["status"] == "failed"

    rows = refresh_jobs.refresh_job_rows(db_path)
    assert rows[0]["status"] == "failed"
    assert "provider unavailable" in (rows[0]["error"] or "")
    assert rows[0]["summary"] == {"error": "provider unavailable"}


def test_refresh_job_marks_failed_summary_as_failed(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"
    monkeypatch.setitem(
        refresh_jobs.ALLOWLIST,
        "unit_refresh",
        lambda _config_path: {"ok": False, "status": "failed", "failedStep": "free_sources"},
    )

    job = refresh_jobs.start_refresh_job("unit_refresh", db_path)
    result = refresh_jobs.execute_refresh_job(job["id"], "unit_refresh", db_path, "config.yaml")
    assert result["status"] == "failed"

    rows = refresh_jobs.refresh_job_rows(db_path)
    assert rows[0]["status"] == "failed"
    assert rows[0]["error"] == "Refresh failed at free_sources"
    assert rows[0]["summary"]["ok"] is False


def test_refresh_options_radar_job_is_allowlisted(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"

    monkeypatch.setattr(
        refresh_jobs.refresh_options_radar,
        "run",
        lambda config_path: {"job": "refresh_options_radar", "config_path": config_path},
    )

    result = refresh_jobs.run_refresh_job("refresh_options_radar", db_path, "config.yaml")

    assert result["status"] == "succeeded"
    assert result["summary"] == {"job": "refresh_options_radar", "config_path": "config.yaml"}


def test_start_refresh_job_returns_existing_running_job(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"
    monkeypatch.setitem(refresh_jobs.ALLOWLIST, "unit_refresh", lambda _config_path: {"ok": True})

    first = refresh_jobs.start_refresh_job("unit_refresh", db_path)
    second = refresh_jobs.start_refresh_job("unit_refresh", db_path)

    assert second["id"] == first["id"]
    assert first["created"] is True
    assert second["created"] is False
    rows = refresh_jobs.refresh_job_rows(db_path)
    assert len(rows) == 1


def test_run_refresh_job_does_not_execute_existing_running_job(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"
    calls = []
    monkeypatch.setitem(refresh_jobs.ALLOWLIST, "unit_refresh", lambda _config_path: calls.append("run") or {"ok": True})

    first = refresh_jobs.start_refresh_job("unit_refresh", db_path)
    second = refresh_jobs.run_refresh_job("unit_refresh", db_path, "config.yaml")

    assert second["id"] == first["id"]
    assert second["status"] == "running"
    assert calls == []
    rows = refresh_jobs.refresh_job_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "running"


def test_stale_running_jobs_are_marked_failed(tmp_path) -> None:
    db_path = tmp_path / "jobs.duckdb"
    init_db(db_path)
    stale_started = datetime.now(UTC) - timedelta(hours=4)
    with db(db_path, read_only=False) as con:
        con.execute(
            """
            INSERT INTO refresh_jobs (id, job_name, status, started_at, finished_at, error, summary)
            VALUES ('stale-job', 'full_market_refresh', 'running', ?, NULL, NULL, '{}')
            """,
            [stale_started],
        )

    rows = refresh_jobs.refresh_job_rows(db_path)
    assert rows[0]["id"] == "stale-job"
    assert rows[0]["status"] == "failed"
    assert "did not finish" in (rows[0]["error"] or "")
