from __future__ import annotations

from datetime import UTC, datetime, timedelta

from investment_panel.core.db import db, init_db
from investment_panel.core import refresh_jobs
from investment_panel.core.retention import prune_operational_tables


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


def test_refresh_job_rows_falls_back_to_read_only_when_writer_config_conflicts(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"
    monkeypatch.setitem(refresh_jobs.ALLOWLIST, "unit_refresh", lambda _config_path: {"ok": True})
    job = refresh_jobs.run_refresh_job("unit_refresh", db_path)

    def fail_init(_db_path):
        raise RuntimeError("Connection Error: different configuration than existing connections")

    monkeypatch.setattr(refresh_jobs, "init_db", fail_init)

    rows = refresh_jobs.refresh_job_rows(db_path)

    assert rows[0]["id"] == job["id"]
    assert rows[0]["status"] == "succeeded"


def test_retention_prunes_old_operational_rows_without_dropping_latest(tmp_path) -> None:
    db_path = tmp_path / "jobs.duckdb"
    init_db(db_path)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    old = now - timedelta(days=45)
    recent = now - timedelta(hours=1)
    with db(db_path, read_only=False) as con:
        for index in range(3):
            timestamp = recent if index == 0 else old - timedelta(hours=index)
            con.execute(
                "INSERT INTO provider_runs VALUES (?, 'test', 'capability', ?, ?, 'ok', '', '{}')",
                [f"provider-{index}", timestamp, timestamp],
            )
            con.execute(
                "INSERT INTO source_runs VALUES ('test_source', ?, 'capability', ?, ?, 'ok', 0, 0, '', '{}')",
                [f"source-{index}", timestamp, timestamp],
            )
            con.execute(
                "INSERT INTO refresh_jobs VALUES (?, 'unit_refresh', 'succeeded', ?, ?, NULL, '{}')",
                [f"job-{index}", timestamp, timestamp],
            )

    counts = prune_operational_tables(db_path, now=now, keep_recent=1, refresh_job_days=14, provider_run_days=30, source_run_days=30)

    assert counts == {"provider_runs": 2, "refresh_jobs": 2, "source_runs": 2}
    with db(db_path, read_only=True) as con:
        provider_ids = [row[0] for row in con.execute("SELECT id FROM provider_runs ORDER BY id").fetchall()]
        source_ids = [row[0] for row in con.execute("SELECT run_id FROM source_runs ORDER BY run_id").fetchall()]
        job_ids = [row[0] for row in con.execute("SELECT id FROM refresh_jobs ORDER BY id").fetchall()]
    assert provider_ids == ["provider-0"]
    assert source_ids == ["source-0"]
    assert job_ids == ["job-0"]


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


def test_refresh_job_failure_message_includes_source_errors(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"
    monkeypatch.setitem(
        refresh_jobs.ALLOWLIST,
        "unit_refresh",
        lambda _config_path: {
            "ok": False,
            "status": "failed",
            "source_errors": [
                {"name": "store_munger_market_metrics", "error": "500"},
                {"name": "store_equity_risk_premium_metric", "error": "504"},
            ],
        },
    )

    job = refresh_jobs.start_refresh_job("unit_refresh", db_path)
    result = refresh_jobs.execute_refresh_job(job["id"], "unit_refresh", db_path, "config.yaml")

    assert result["status"] == "failed"
    rows = refresh_jobs.refresh_job_rows(db_path)
    assert rows[0]["error"] == "Refresh failed for sources: store_munger_market_metrics, store_equity_risk_premium_metric"


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


def test_refresh_options_radar_learning_marks_job_is_allowlisted(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"

    monkeypatch.setattr(
        refresh_jobs.refresh_options_radar,
        "run_learning_marks",
        lambda config_path: {"job": "refresh_options_radar_learning_marks", "config_path": config_path},
    )

    result = refresh_jobs.run_refresh_job("refresh_options_radar_learning_marks", db_path, "config.yaml")

    assert result["status"] == "succeeded"
    assert result["summary"] == {"job": "refresh_options_radar_learning_marks", "config_path": "config.yaml"}


def test_update_market_environment_job_is_allowlisted(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"

    monkeypatch.setattr(
        refresh_jobs.update_market_environment,
        "run",
        lambda config_path: {"job": "update_market_environment", "config_path": config_path},
    )

    result = refresh_jobs.run_refresh_job("update_market_environment", db_path, "config.yaml")

    assert result["status"] == "succeeded"
    assert result["summary"] == {"job": "update_market_environment", "config_path": "config.yaml"}


def test_hourly_options_radar_job_is_allowlisted(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"

    monkeypatch.setattr(
        refresh_jobs.hourly_options_radar,
        "run",
        lambda config_path: {"job": "hourly_options_radar", "config_path": config_path, "agent_workers": "daily_premarket_only"},
    )

    result = refresh_jobs.run_refresh_job("hourly_options_radar", db_path, "config.yaml")

    assert result["status"] == "succeeded"
    assert result["summary"] == {
        "job": "hourly_options_radar",
        "config_path": "config.yaml",
        "agent_workers": "daily_premarket_only",
    }


def test_run_option_agents_job_is_allowlisted(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"

    monkeypatch.setattr(
        refresh_jobs.run_option_agents,
        "run",
        lambda config_path: {"job": "run_option_agents", "config_path": config_path},
    )

    result = refresh_jobs.run_refresh_job("run_option_agents", db_path, "config.yaml")

    assert result["status"] == "succeeded"
    assert result["summary"] == {"job": "run_option_agents", "config_path": "config.yaml"}


def test_premarket_options_intelligence_job_is_allowlisted(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"

    monkeypatch.setattr(
        refresh_jobs.premarket_options_intelligence,
        "run",
        lambda config_path: {"job": "premarket_options_intelligence", "config_path": config_path, "agent_workers": "enabled_once_per_day"},
    )

    result = refresh_jobs.run_refresh_job("premarket_options_intelligence", db_path, "config.yaml")

    assert result["status"] == "succeeded"
    assert result["summary"] == {
        "job": "premarket_options_intelligence",
        "config_path": "config.yaml",
        "agent_workers": "enabled_once_per_day",
    }


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
