from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta

from investment_panel.core import refresh_jobs
import psycopg
import pytest


@pytest.fixture(autouse=True)
def _postgresql_job_authority(migrated_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKET_DATABASE_URL", migrated_postgres_dsn)


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


def test_refresh_job_rows_reads_completed_postgresql_job(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"
    monkeypatch.setitem(refresh_jobs.ALLOWLIST, "unit_refresh", lambda _config_path: {"ok": True})
    job = refresh_jobs.run_refresh_job("unit_refresh", db_path)

    rows = refresh_jobs.refresh_job_rows(db_path)

    assert rows[0]["id"] == job["id"]
    assert rows[0]["status"] == "succeeded"


def test_refresh_job_rows_returns_running_postgresql_job(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"
    monkeypatch.setitem(refresh_jobs.ALLOWLIST, "unit_refresh", lambda _config_path: {"ok": True})
    job = refresh_jobs.start_refresh_job("unit_refresh", db_path)

    rows = refresh_jobs.refresh_job_rows(db_path)

    assert len(rows) == 1
    assert rows[0]["id"] == job["id"]
    assert rows[0]["job_name"] == "unit_refresh"
    assert rows[0]["status"] == "running"
    assert rows[0]["summary"] == {}


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


def test_options_radar_hard_refresh_updates_source_then_rebuilds_radar(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"
    calls: list[tuple[str, str | None]] = []

    def fake_update(config_path):
        calls.append(("source", config_path))
        return {"status": "ok", "chain_rows": 12, "symbols": ["NVDA", "TSLA"]}

    def fake_signal(config_path, *, symbols=None, source=None):
        calls.append((f"radar:{source}:{','.join(symbols or [])}", config_path))
        return {"mode": "signal_only", "source": source, "symbols": symbols, "option_radar_opportunities": 5}

    monkeypatch.setattr(refresh_jobs.update_robinhood_options, "run", fake_update)
    monkeypatch.setattr(refresh_jobs.refresh_options_radar, "run_signal_only", fake_signal)

    result = refresh_jobs.run_refresh_job("options_radar_hard_refresh", db_path, "config.yaml")

    assert result["status"] == "succeeded"
    assert result["summary"]["ok"] is True
    assert result["summary"]["options_radar"]["option_radar_opportunities"] == 5
    assert result["summary"]["options_radar"]["symbols"] == ["NVDA", "TSLA"]
    assert calls == [("source", "config.yaml"), ("radar:robinhood:NVDA,TSLA", "config.yaml")]


def test_options_radar_hard_refresh_skips_radar_when_no_incremental_symbols(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"
    calls: list[str] = []
    monkeypatch.setattr(refresh_jobs.update_robinhood_options, "run", lambda _config_path: {"status": "ok", "chain_rows": 0, "symbols": []})
    monkeypatch.setattr(refresh_jobs.refresh_options_radar, "run_signal_only", lambda *_args, **_kwargs: calls.append("radar") or {})

    result = refresh_jobs.run_refresh_job("options_radar_hard_refresh", db_path, "config.yaml")

    assert result["status"] == "succeeded"
    assert result["summary"]["options_radar"] == {"status": "skipped", "reason": "no_incremental_symbols", "source": "robinhood"}
    assert calls == []


def test_options_radar_hard_refresh_timeout_covers_source_and_radar_steps() -> None:
    assert refresh_jobs.JOB_TIMEOUT_SECONDS["options_radar_hard_refresh"] >= 5400


def test_options_radar_hard_refresh_fails_when_source_unusable(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"
    calls: list[str] = []

    monkeypatch.setattr(
        refresh_jobs.update_robinhood_options,
        "run",
        lambda _config_path: {"status": "auth_required", "provider": "robinhood"},
    )
    monkeypatch.setattr(refresh_jobs.refresh_options_radar, "run_signal_only", lambda *_args, **_kwargs: calls.append("radar") or {})

    result = refresh_jobs.run_refresh_job("options_radar_hard_refresh", db_path, "config.yaml")

    assert result["status"] == "failed"
    assert result["error"] == "Robinhood option refresh returned auth_required"
    assert calls == []


def test_refresh_job_subprocess_timeout_marks_job_failed(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "jobs.duckdb"
    monkeypatch.setitem(refresh_jobs.ALLOWLIST, "unit_refresh", lambda _config_path: {"ok": True})
    monkeypatch.setitem(refresh_jobs.JOB_TIMEOUT_SECONDS, "unit_refresh", 1)
    job = refresh_jobs.start_refresh_job("unit_refresh", db_path)

    def timeout_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(refresh_jobs.subprocess, "run", timeout_run)

    result = refresh_jobs.execute_refresh_job_subprocess(job["id"], "unit_refresh", db_path, "config.yaml")

    assert result["status"] == "failed"
    assert "timed out after 1s" in result["error"]
    rows = refresh_jobs.refresh_job_rows(db_path)
    assert rows[0]["id"] == job["id"]
    assert rows[0]["status"] == "failed"
    assert "timed out after 1s" in (rows[0]["error"] or "")


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


def test_stale_running_jobs_are_marked_failed(tmp_path, migrated_postgres_dsn: str) -> None:
    db_path = tmp_path / "jobs.duckdb"
    stale_started = datetime.now(UTC) - timedelta(hours=4)
    job = refresh_jobs.start_refresh_job("full_market_refresh", db_path)
    with psycopg.connect(migrated_postgres_dsn) as con:
        con.execute("UPDATE ops.job_run SET started_at = %s WHERE id = %s", [stale_started, job["id"]])

    rows = refresh_jobs.refresh_job_rows(db_path)
    assert rows[0]["id"] == job["id"]
    assert rows[0]["status"] == "failed"
    assert "did not finish" in (rows[0]["error"] or "")
