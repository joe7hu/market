from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
import os
import time
from types import SimpleNamespace

from investment_panel.core import refresh_jobs
import psycopg
import pytest
from psycopg.errors import LockNotAvailable

from investment_panel.jobs import refresh_options_radar as radar_refresh_job


def test_material_thesis_monitor_receives_only_changed_symbols(monkeypatch) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        refresh_jobs.run_thesis_monitor,
        "run",
        lambda _path, **kwargs: observed.update(kwargs) or {"status": "ok", "completed": 1, "failed": 0},
    )

    result = refresh_jobs.run_source_with_material_thesis(
        "config.yaml",
        lambda _path: {"status": "ok", "affected_symbols": ["nvda"]},
    )

    assert result["status"] == "ok"
    assert observed == {"symbols": ["NVDA"], "trigger": "material_event"}


def test_material_thesis_monitor_skips_when_source_reports_no_changes(monkeypatch) -> None:
    monkeypatch.setattr(
        refresh_jobs.run_thesis_monitor,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("monitor should not run")),
    )

    result = refresh_jobs.run_source_with_material_thesis(
        "config.yaml",
        lambda _path: {"status": "ok", "affected_symbols": []},
    )

    assert result["material_thesis_monitor"]["reason"] == "no_changed_symbols"


def test_material_thesis_monitor_failure_does_not_hide_source_success(monkeypatch) -> None:
    monkeypatch.setattr(
        refresh_jobs.run_thesis_monitor,
        "run",
        lambda *_args, **_kwargs: {"status": "failed", "error": "monitor unavailable"},
    )

    result = refresh_jobs.run_source_with_material_thesis(
        "config.yaml",
        lambda _path: {"status": "ok", "affected_symbols": ["QQQ"]},
    )

    assert result["status"] == "partial"
    assert result["source_status"] == "ok"
    assert result["downstream_status"] == "failed"
    assert result["source_result"]["status"] == "ok"


def test_outcome_refresh_includes_ticker_learning_without_staging_orders(monkeypatch) -> None:
    runtime = object()
    config = object()

    class FakeSymbolRepository:
        def __init__(self, received_runtime):
            assert received_runtime is runtime

        def refresh(self):
            return {"evaluated": 2, "resolved": 1}

    class FakeTickerRepository:
        def __init__(self, received_runtime):
            assert received_runtime is runtime

        def refresh_outcomes(self):
            return {"evaluated": 3, "updated": 18, "resolved": 2}

    monkeypatch.setattr(refresh_jobs.refresh_symbol_decision_outcomes, "load_config", lambda _path: config)
    monkeypatch.setattr(refresh_jobs.refresh_symbol_decision_outcomes, "runtime_for_config", lambda _config: runtime)
    monkeypatch.setattr(
        refresh_jobs.refresh_symbol_decision_outcomes,
        "SymbolDecisionOutcomeRepository",
        FakeSymbolRepository,
    )
    monkeypatch.setattr(
        refresh_jobs.refresh_symbol_decision_outcomes,
        "TickerDecisionRepository",
        FakeTickerRepository,
    )

    result = refresh_jobs.refresh_symbol_decision_outcomes.run("config.yaml")

    assert result["database"] == "postgresql"
    assert result["paper_orders"] == 0
    assert result["evaluated"] == 2
    assert result["symbol_outcomes"] == {"evaluated": 2, "resolved": 1}
    assert result["ticker_outcomes"] == {"evaluated": 3, "updated": 18, "resolved": 2}


def test_benchmark_refresh_only_freezes_the_equity_denominator(monkeypatch) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        refresh_jobs.ticker_decisions,
        "publish_benchmark",
        lambda path: observed.update({"path": path}) or {
            "status": "ok",
            "published_count": 0,
            "paper_orders": 0,
        },
    )

    result = refresh_jobs.ALLOWLIST["publish_ticker_benchmark"]("config.yaml")

    assert result["published_count"] == 0
    assert result["paper_orders"] == 0
    assert observed == {"path": "config.yaml"}


@pytest.fixture(autouse=True)
def _postgresql_job_authority(migrated_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKET_DATABASE_URL", migrated_postgres_dsn)


def test_refresh_job_can_be_started_and_completed(tmp_path, monkeypatch) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]
    monkeypatch.setitem(refresh_jobs.ALLOWLIST, "unit_refresh", lambda _config_path: {"ok": True, "rows": 3})

    job = refresh_jobs.start_refresh_job("unit_refresh", db_path)
    assert job["status"] == "running"

    result = refresh_jobs.execute_refresh_job(job["id"], "unit_refresh", db_path, "config.yaml")
    assert result["status"] == "succeeded"

    rows = refresh_jobs.refresh_job_rows(db_path)
    assert rows[0]["id"] == job["id"]
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["summary"] == {"ok": True, "rows": 3}


def test_refresh_job_records_due_dispatch_source_and_downstream_status(tmp_path, monkeypatch) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]
    due_at = datetime.now(UTC) - timedelta(seconds=5)
    monkeypatch.setitem(
        refresh_jobs.ALLOWLIST,
        "unit_refresh",
        lambda _config_path: {
            "status": "partial",
            "source_status": "ok",
            "downstream_status": "failed",
            "source_result": {"status": "ok"},
        },
    )

    job = refresh_jobs.start_refresh_job(
        "unit_refresh",
        db_path,
        scheduled_due_at=due_at,
        dispatched_at=datetime.now(UTC),
    )
    result = refresh_jobs.execute_refresh_job(job["id"], "unit_refresh", db_path, "config.yaml")
    row = refresh_jobs.refresh_job_rows(db_path)[0]

    assert result["status"] == "partial"
    assert row["scheduled_due_at"] == due_at
    assert row["dispatched_at"] is not None
    assert row["source_status"] == "ok"
    assert row["downstream_status"] == "failed"


def test_refresh_job_preserves_partial_result_status(tmp_path, monkeypatch) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]
    summary = {"status": "partial", "captures": [{"symbol": "QQQ", "completeness": 0.64}]}
    monkeypatch.setitem(refresh_jobs.ALLOWLIST, "unit_refresh", lambda _config_path: summary)

    job = refresh_jobs.start_refresh_job("unit_refresh", db_path)
    result = refresh_jobs.execute_refresh_job(job["id"], "unit_refresh", db_path, "config.yaml")

    assert result["status"] == "partial"
    assert refresh_jobs.refresh_job_rows(db_path)[0]["status"] == "partial"


def test_refresh_job_rows_reads_completed_postgresql_job(tmp_path, monkeypatch) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]
    monkeypatch.setitem(refresh_jobs.ALLOWLIST, "unit_refresh", lambda _config_path: {"ok": True})
    job = refresh_jobs.run_refresh_job("unit_refresh", db_path)

    rows = refresh_jobs.refresh_job_rows(db_path)

    assert rows[0]["id"] == job["id"]
    assert rows[0]["status"] == "succeeded"


def test_refresh_job_rows_returns_running_postgresql_job(tmp_path, monkeypatch) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]
    monkeypatch.setitem(refresh_jobs.ALLOWLIST, "unit_refresh", lambda _config_path: {"ok": True})
    job = refresh_jobs.start_refresh_job("unit_refresh", db_path)

    rows = refresh_jobs.refresh_job_rows(db_path)

    assert len(rows) == 1
    assert rows[0]["id"] == job["id"]
    assert rows[0]["job_name"] == "unit_refresh"
    assert rows[0]["status"] == "running"
    assert rows[0]["summary"] == {}


def test_refresh_job_records_failure_without_reraising(tmp_path, monkeypatch) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]

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
    db_path = os.environ["MARKET_DATABASE_URL"]
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
    db_path = os.environ["MARKET_DATABASE_URL"]
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
    db_path = os.environ["MARKET_DATABASE_URL"]

    monkeypatch.setattr(
        refresh_jobs.refresh_options_radar,
        "run",
        lambda config_path: {"job": "refresh_options_radar", "config_path": config_path},
    )

    result = refresh_jobs.run_refresh_job("refresh_options_radar", db_path, "config.yaml")

    assert result["status"] == "succeeded"
    assert result["summary"] == {"job": "refresh_options_radar", "config_path": "config.yaml"}


def test_signal_only_radar_skips_a_concurrent_catalog_writer(monkeypatch) -> None:
    config = SimpleNamespace(
        analysis=SimpleNamespace(
            options_decision_system=SimpleNamespace(options_risk_sleeve_capital=500.0)
        )
    )
    monkeypatch.setattr(radar_refresh_job, "load_config", lambda _path: config)
    monkeypatch.setattr(radar_refresh_job, "runtime_for_config", lambda _config: object())
    monkeypatch.setattr(
        radar_refresh_job,
        "refresh_options_radar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(LockNotAvailable("busy")),
    )

    result = radar_refresh_job.run_signal_only("config.yaml", source="robinhood")

    assert result == {
        "database": "postgresql",
        "strategy_version": radar_refresh_job.DEFAULT_STRATEGY_VERSION,
        "mode": "signal_only",
        "source": "robinhood",
        "status": "skipped",
        "reason": "database_lock_busy",
    }


def test_options_radar_hard_refresh_updates_source_then_rebuilds_radar(tmp_path, monkeypatch) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]
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


def test_options_radar_hard_refresh_retries_provider_capacity_before_pulling_source(tmp_path, monkeypatch) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]
    attempts: list[int] = []
    waits: list[float] = []
    released: list[int] = []

    class DeferredThenAvailablePolicy:
        def acquire_provider_lease(self, **_kwargs):
            attempts.append(1)
            return None if len(attempts) < 3 else SimpleNamespace(id=42)

        def release_provider_lease(self, lease_id: int) -> None:
            released.append(lease_id)

    monkeypatch.setattr(refresh_jobs, "OptionHistoryPolicyRepository", lambda _runtime: DeferredThenAvailablePolicy())
    monkeypatch.setattr(refresh_jobs, "RADAR_PROVIDER_LEASE_RETRY_DELAYS_SECONDS", (3.0, 7.0))
    monkeypatch.setattr(refresh_jobs.time, "sleep", waits.append)
    monkeypatch.setattr(
        refresh_jobs.update_robinhood_options,
        "run",
        lambda _config_path: {"status": "ok", "symbols": ["NVDA"]},
    )
    monkeypatch.setattr(
        refresh_jobs.refresh_options_radar,
        "run_signal_only",
        lambda *_args, **_kwargs: {"status": "ok", "option_radar_opportunities": 1},
    )

    result = refresh_jobs.run_refresh_job("options_radar_hard_refresh", db_path, "config.yaml")

    assert result["status"] == "succeeded"
    assert result["summary"]["provider_lease_attempts"] == 3
    assert attempts == [1, 1, 1]
    assert waits == [3.0, 7.0]
    assert released == [42]


def test_options_radar_hard_refresh_fails_after_capacity_retry_budget(tmp_path, monkeypatch) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]
    waits: list[float] = []

    class AlwaysDeferredPolicy:
        def acquire_provider_lease(self, **_kwargs):
            return None

    monkeypatch.setattr(refresh_jobs, "OptionHistoryPolicyRepository", lambda _runtime: AlwaysDeferredPolicy())
    monkeypatch.setattr(refresh_jobs, "RADAR_PROVIDER_LEASE_RETRY_DELAYS_SECONDS", (2.0, 5.0))
    monkeypatch.setattr(refresh_jobs.time, "sleep", waits.append)
    monkeypatch.setattr(
        refresh_jobs.update_robinhood_options,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("source pull should not run")),
    )

    result = refresh_jobs.run_refresh_job("options_radar_hard_refresh", db_path, "config.yaml")

    assert result["status"] == "failed"
    assert result["summary"]["reason"] == "provider_capacity_deferred"
    assert result["summary"]["provider_lease_attempts"] == 3
    assert waits == [2.0, 5.0]
    assert "after 3 provider-capacity attempts" in result["error"]


def test_options_radar_hard_refresh_skips_radar_when_no_incremental_symbols(tmp_path, monkeypatch) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]
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
    db_path = os.environ["MARKET_DATABASE_URL"]
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
    db_path = os.environ["MARKET_DATABASE_URL"]
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


def test_refresh_subprocess_keeps_database_credentials_out_of_arguments(monkeypatch) -> None:
    captured = {}

    class _Repository:
        class runtime:
            dsn = "postgresql://market:super-secret@db.internal/market"

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        captured["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(command, 0, stdout='{"status":"succeeded"}', stderr="")

    monkeypatch.setattr(refresh_jobs, "_job_repository", lambda *_args: _Repository())
    monkeypatch.setattr(refresh_jobs.subprocess, "run", fake_run)

    result = refresh_jobs.execute_refresh_job_subprocess("job-1", "unit_refresh", "ignored", "config.yaml")

    assert result["status"] == "succeeded"
    assert "super-secret" not in " ".join(captured["command"])
    assert captured["env"]["MARKET_DATABASE_URL"].endswith("@db.internal/market")
    assert captured["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(refresh_jobs.SOURCE_ROOT)
    assert captured["cwd"] == refresh_jobs.PROJECT_ROOT


def test_refresh_options_radar_learning_marks_job_is_allowlisted(tmp_path, monkeypatch) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]

    monkeypatch.setattr(
        refresh_jobs.refresh_options_radar,
        "run_learning_marks",
        lambda config_path: {"job": "refresh_options_radar_learning_marks", "config_path": config_path},
    )

    result = refresh_jobs.run_refresh_job("refresh_options_radar_learning_marks", db_path, "config.yaml")

    assert result["status"] == "succeeded"
    assert result["summary"] == {"job": "refresh_options_radar_learning_marks", "config_path": "config.yaml"}


def test_hourly_options_radar_job_is_allowlisted(tmp_path, monkeypatch) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]

    monkeypatch.setattr(
        refresh_jobs.refresh_options_radar,
        "run_signal_only",
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
    db_path = os.environ["MARKET_DATABASE_URL"]

    monkeypatch.setattr(
        refresh_jobs.run_option_agents,
        "run",
        lambda config_path: {"job": "run_option_agents", "config_path": config_path},
    )

    result = refresh_jobs.run_refresh_job("run_option_agents", db_path, "config.yaml")

    assert result["status"] == "succeeded"
    assert result["summary"] == {"job": "run_option_agents", "config_path": "config.yaml"}


def test_run_thesis_monitor_jobs_are_allowlisted(tmp_path, monkeypatch) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]
    calls: list[dict[str, object]] = []

    def fake_run(config_path, **kwargs):
        calls.append({"config_path": config_path, **kwargs})
        return {"job": "run_thesis_monitor", **kwargs}

    monkeypatch.setattr(refresh_jobs.run_thesis_monitor, "run", fake_run)

    force = refresh_jobs.run_refresh_job("run_thesis_monitor_force", db_path, "config.yaml")
    preflight = refresh_jobs.run_refresh_job("run_thesis_monitor_preflight", db_path, "config.yaml")

    assert force["status"] == "succeeded"
    assert preflight["status"] == "succeeded"
    assert calls == [
        {"config_path": "config.yaml", "trigger": "manual", "force": True},
        {"config_path": "config.yaml", "trigger": "manual", "force": True, "dry_run": True},
    ]


def test_premarket_options_intelligence_job_is_allowlisted(tmp_path, monkeypatch) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]

    monkeypatch.setattr(
        refresh_jobs.postgres_refresh,
        "premarket",
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
    db_path = os.environ["MARKET_DATABASE_URL"]
    monkeypatch.setitem(refresh_jobs.ALLOWLIST, "unit_refresh", lambda _config_path: {"ok": True})

    first = refresh_jobs.start_refresh_job("unit_refresh", db_path)
    second = refresh_jobs.start_refresh_job("unit_refresh", db_path)

    assert second["id"] == first["id"]
    assert first["created"] is True
    assert second["created"] is False
    rows = refresh_jobs.refresh_job_rows(db_path)
    assert len(rows) == 1


def test_run_refresh_job_does_not_execute_existing_running_job(tmp_path, monkeypatch) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]
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


def test_job_heartbeat_runs_while_handler_is_active() -> None:
    class Repository:
        def __init__(self) -> None:
            self.pulses = 0

        def heartbeat(self, _job_id: str) -> bool:
            self.pulses += 1
            return True

    repository = Repository()
    with refresh_jobs._heartbeat_while_running(repository, "job-1", interval_seconds=0.01):
        time.sleep(0.035)

    assert repository.pulses >= 3


def test_stale_running_jobs_are_marked_failed(tmp_path, migrated_postgres_dsn: str) -> None:
    db_path = os.environ["MARKET_DATABASE_URL"]
    stale_started = datetime.now(UTC) - timedelta(hours=4)
    job = refresh_jobs.start_refresh_job("full_market_refresh", db_path)
    with psycopg.connect(migrated_postgres_dsn) as con:
        con.execute(
            "UPDATE ops.job_run SET started_at = %s, heartbeat_at = %s WHERE id = %s",
            [stale_started, stale_started, job["id"]],
        )

    rows = refresh_jobs.refresh_job_rows(db_path)
    assert rows[0]["id"] == job["id"]
    assert rows[0]["status"] == "failed"
    assert "did not finish" in (rows[0]["error"] or "")
