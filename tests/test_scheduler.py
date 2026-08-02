from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app import scheduler
from investment_panel.core.refresh_jobs import ALLOWLIST


def test_scheduler_enabled_defaults_on(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_SCHEDULER_ENABLED", raising=False)
    assert scheduler.scheduler_enabled() is True


def test_scheduler_enabled_respects_off_values(monkeypatch) -> None:
    for value in ("0", "false", "off", "no", "OFF"):
        monkeypatch.setenv("MARKET_SCHEDULER_ENABLED", value)
        assert scheduler.scheduler_enabled() is False


def test_job_intervals_default_to_daily_premarket_options(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.delenv("MARKET_SOURCE_REFRESH_SECONDS", raising=False)
    monkeypatch.delenv("MARKET_RADAR_REFRESH_SECONDS", raising=False)
    monkeypatch.delenv("MARKET_OPTIONS_RADAR_HARD_REFRESH_SECONDS", raising=False)
    intervals = scheduler.job_intervals()
    assert "options_radar_hard_refresh" not in intervals


def test_operational_source_refreshes_default_on(monkeypatch) -> None:
    for variable in (
        "MARKET_SOCIAL_REFRESH_SECONDS",
        "MARKET_RESEARCH_REFRESH_SECONDS",
        "MARKET_ARCO_REFRESH_SECONDS",
        "MARKET_MARKET_DATA_REFRESH_SECONDS",
    ):
        monkeypatch.delenv(variable, raising=False)

    intervals = scheduler.job_intervals()

    assert intervals["update_social_sources"] == 1800
    assert intervals["update_research_sources"] == 3600
    assert intervals["update_arco_data"] == 14400
    assert intervals["update_market_data"] == 3600


def test_robinhood_split_source_and_signal_can_be_enabled_explicitly(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_SOURCE_REFRESH_SECONDS", "120")
    monkeypatch.setenv("MARKET_RADAR_REFRESH_SECONDS", "60")
    intervals = scheduler.job_intervals()
    assert "options_radar_hard_refresh" not in intervals
    assert intervals["update_robinhood_options"] == 120
    assert intervals["refresh_options_radar_signal_robinhood"] == 60


def test_ibkr_source_fallback_via_env(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_RADAR_OPTION_SOURCE", "ibkr")
    monkeypatch.setenv("MARKET_IN_PROCESS_HEAVY_REFRESH", "1")
    monkeypatch.setenv("MARKET_SOURCE_REFRESH_SECONDS", "3600")
    monkeypatch.setenv("MARKET_RADAR_REFRESH_SECONDS", "900")
    intervals = scheduler.job_intervals()
    assert "update_ibkr_options" in intervals
    assert "refresh_options_radar_signal_ibkr" in intervals
    assert "update_robinhood_options" not in intervals


def test_retired_free_source_falls_back_to_postgresql_robinhood(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_RADAR_OPTION_SOURCE", "free")
    monkeypatch.setenv("MARKET_IN_PROCESS_HEAVY_REFRESH", "1")
    monkeypatch.setenv("MARKET_SOURCE_REFRESH_SECONDS", "3600")
    monkeypatch.setenv("MARKET_RADAR_REFRESH_SECONDS", "900")
    intervals = scheduler.job_intervals()
    assert "update_robinhood_options" in intervals
    assert "refresh_options_radar_signal_robinhood" in intervals
    assert "update_ibkr_options" not in intervals


def test_job_intervals_ignore_invalid_env(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_IN_PROCESS_HEAVY_REFRESH", "1")
    monkeypatch.setenv("MARKET_SOURCE_REFRESH_SECONDS", "not-a-number")
    monkeypatch.setenv("MARKET_RADAR_REFRESH_SECONDS", "-5")
    intervals = scheduler.job_intervals()
    assert "options_radar_hard_refresh" not in intervals
    assert "update_robinhood_options" not in intervals
    assert "refresh_options_radar_signal_robinhood" not in intervals


def test_source_pull_can_be_disabled(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_SOURCE_REFRESH_SECONDS", "0")
    monkeypatch.setenv("MARKET_RADAR_REFRESH_SECONDS", "900")
    intervals = scheduler.job_intervals()
    assert "options_radar_hard_refresh" not in intervals
    assert "update_robinhood_options" not in intervals
    # The continuous fresh-signal loop must remain regardless.
    assert "refresh_options_radar_signal_robinhood" in intervals


def test_radar_freshness_loop_defaults_to_premarket_only(monkeypatch) -> None:
    for var in (
        "MARKET_RADAR_OPTION_SOURCE",
        "MARKET_IN_PROCESS_HEAVY_REFRESH",
        "MARKET_RADAR_REFRESH_SECONDS",
        "MARKET_SOURCE_REFRESH_SECONDS",
        "MARKET_LEARNING_REFRESH_SECONDS",
        "MARKET_ENVIRONMENT_REFRESH_SECONDS",
        "MARKET_PREOPEN_BRIEF_REFRESH_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    intervals = scheduler.job_intervals()
    assert "options_radar_hard_refresh" not in intervals
    assert "refresh_options_radar_learning_marks" not in intervals
    assert "refresh_options_radar_deterministic" not in intervals
    assert intervals["update_market_environment"] == 3600
    assert "update_preopen_daily_brief_scheduled" not in intervals


def test_heavy_refresh_loops_can_be_enabled_for_app_process(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_IN_PROCESS_HEAVY_REFRESH", "1")
    monkeypatch.setenv("MARKET_RADAR_REFRESH_SECONDS", "900")
    monkeypatch.setenv("MARKET_SOURCE_REFRESH_SECONDS", "3600")
    intervals = scheduler.job_intervals()
    assert "options_radar_hard_refresh" not in intervals
    assert intervals["refresh_options_radar_signal_robinhood"] == 900
    assert intervals["update_robinhood_options"] == 3600
    assert intervals["refresh_options_radar_deterministic"] == 21600


def test_market_environment_refresh_can_be_disabled(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_ENVIRONMENT_REFRESH_SECONDS", "0")
    intervals = scheduler.job_intervals()
    assert "update_market_environment" not in intervals


def test_preopen_brief_refresh_can_be_disabled(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_PREOPEN_BRIEF_REFRESH_SECONDS", "0")
    intervals = scheduler.job_intervals()
    assert "update_preopen_daily_brief_scheduled" not in intervals


def test_postgresql_market_and_preopen_refreshes_are_scheduled(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_ENVIRONMENT_REFRESH_SECONDS", "3600")
    monkeypatch.setenv("MARKET_PREOPEN_BRIEF_REFRESH_SECONDS", "300")
    intervals = scheduler.job_intervals()
    assert intervals["update_market_environment"] == 3600
    assert intervals["update_preopen_daily_brief_scheduled"] == 300


def test_agent_pass_on_by_default_daily(monkeypatch) -> None:
    for var in ("MARKET_RADAR_OPTION_SOURCE", "MARKET_AGENT_REFRESH_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MARKET_IN_PROCESS_HEAVY_REFRESH", "1")
    import investment_panel.core.config as config

    monkeypatch.setattr(
        config,
        "load_config",
        lambda: SimpleNamespace(agents=SimpleNamespace(option_agent=SimpleNamespace(enabled=True, auto_run_seconds=0))),
    )
    intervals = scheduler.job_intervals()
    assert intervals["run_option_agents"] == 86400  # daily by default (Phase 2c)


def test_scheduler_status_reports_actual_intervals(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.delenv("MARKET_AGENT_REFRESH_SECONDS", raising=False)
    monkeypatch.setenv("MARKET_IN_PROCESS_HEAVY_REFRESH", "1")
    status = scheduler.scheduler_status(
        {"agents": {"option_agent": {"enabled": True, "auto_run_seconds": 123}}}
    )

    assert status["agent_refresh_seconds"] == "123"
    assert status["radar_refresh_seconds"] == "0"
    assert status["source_refresh_seconds"] == "0"
    assert status["options_hard_refresh_seconds"] == "0"
    assert status["learning_mark_refresh_seconds"] == "0"
    assert status["learning_refresh_seconds"] == "21600"
    assert status["market_environment_refresh_seconds"] == "3600"
    assert status["preopen_brief_refresh_seconds"] == "0"
    assert status["external_jobs"]["premarket_options_intelligence"]["owner"] == "launchd"
    assert status["external_jobs"]["premarket_options_intelligence"]["market_calendar_gated"] is True
    assert status["jobs"]["run_option_agents"] == 123


def test_source_writers_wait_one_interval_before_first_run() -> None:
    assert scheduler._initial_delay_seconds("options_radar_hard_refresh", 900, 0) == 900
    assert scheduler._initial_delay_seconds("update_robinhood_options", 120, 1) == 120
    assert scheduler._initial_delay_seconds("refresh_options_radar_signal_robinhood", 60, 2) == 2 * scheduler.STAGGER_SECONDS


def test_option_history_starts_on_the_next_quarter_hour() -> None:
    eastern = ZoneInfo("America/New_York")
    assert scheduler._initial_delay_seconds(
        "robinhood_option_history", 900, 0, reference_time=datetime(2026, 7, 20, 9, 30, tzinfo=eastern)
    ) == 0
    assert scheduler._initial_delay_seconds(
        "robinhood_option_history", 900, 0, reference_time=datetime(2026, 7, 20, 9, 30, 5, tzinfo=eastern)
    ) == 895
    assert scheduler._is_slot_boundary(
        "robinhood_option_history", 900, datetime(2026, 7, 20, 9, 30, 0, 500_000, tzinfo=eastern)
    )
    assert scheduler._is_slot_boundary(
        "robinhood_option_history", 900, datetime(2026, 7, 20, 9, 30, 22, tzinfo=eastern)
    )
    assert not scheduler._is_slot_boundary(
        "robinhood_option_history", 900, datetime(2026, 7, 20, 9, 29, 59, 999_000, tzinfo=eastern)
    )


def test_recurring_jobs_wait_the_configured_interval_after_completion_or_skip() -> None:
    assert scheduler._recurring_delay_seconds("update_market_data", 3600) == 3600
    assert scheduler._recurring_delay_seconds("update_social_sources", 1800) == 1800


def test_option_history_recurrence_uses_the_next_quarter_hour_not_the_startup_stagger() -> None:
    eastern = ZoneInfo("America/New_York")
    assert scheduler._recurring_delay_seconds(
        "robinhood_option_history", 900, reference_time=datetime(2026, 7, 20, 9, 30, 5, tzinfo=eastern)
    ) == 895
    assert scheduler._recurring_delay_seconds(
        "robinhood_option_history", 900, reference_time=datetime(2026, 7, 20, 9, 30, tzinfo=eastern)
    ) == 900


def test_agent_pass_can_be_disabled(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_AGENT_REFRESH_SECONDS", "0")
    intervals = scheduler.job_intervals()
    assert "run_option_agents" not in intervals


def test_learning_refresh_can_be_disabled(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_IN_PROCESS_HEAVY_REFRESH", "1")
    monkeypatch.setenv("MARKET_RADAR_REFRESH_SECONDS", "900")
    monkeypatch.setenv("MARKET_LEARNING_REFRESH_SECONDS", "0")
    intervals = scheduler.job_intervals()
    assert "refresh_options_radar_deterministic" not in intervals
    assert "refresh_options_radar_signal_robinhood" in intervals  # fast loop stays


def test_learning_mark_refresh_can_be_enabled_explicitly(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_LEARNING_MARK_REFRESH_SECONDS", "1800")
    intervals = scheduler.job_intervals()
    assert intervals["refresh_options_radar_learning_marks"] == 1800


def test_dispatch_starts_and_executes_refresh_job(monkeypatch) -> None:
    started_calls: list[tuple[str, object]] = []
    execute_calls: list[tuple[str, str, object, str]] = []

    def fake_start_refresh_job(job_name, db_path):
        started_calls.append((job_name, db_path))
        return {"id": "job-1", "job_name": job_name, "created": True}

    async def fake_execute_started_refresh_job(job_name, job_id, db_path, config_path):
        execute_calls.append((job_name, job_id, db_path, config_path))
        return {"status": "succeeded"}

    monkeypatch.setattr(scheduler, "start_refresh_job", fake_start_refresh_job)
    monkeypatch.setattr(scheduler, "_execute_started_refresh_job", fake_execute_started_refresh_job)
    asyncio.run(scheduler._dispatch("refresh_options_radar_signal_robinhood", "db", "config.yaml"))

    assert started_calls == [("refresh_options_radar_signal_robinhood", "db")]
    assert execute_calls == [("refresh_options_radar_signal_robinhood", "job-1", "db", "config.yaml")]


def test_dispatch_skips_existing_running_job(monkeypatch) -> None:
    execute_calls: list[str] = []

    def fake_start_refresh_job(job_name, db_path):
        return {"id": "job-1", "job_name": job_name, "status": "running", "created": False}

    async def fake_execute_started_refresh_job(*_args):
        execute_calls.append("executed")
        return {"status": "succeeded"}

    monkeypatch.setattr(scheduler, "start_refresh_job", fake_start_refresh_job)
    monkeypatch.setattr(scheduler, "_execute_started_refresh_job", fake_execute_started_refresh_job)

    asyncio.run(scheduler._dispatch("refresh_options_radar_signal_robinhood", "db", "config.yaml"))

    assert execute_calls == []


def test_dispatch_swallows_exceptions(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("job blew up")

    monkeypatch.setattr(scheduler, "start_refresh_job", boom)
    # Must not raise: a bad job can never be allowed to kill the scheduler loop.
    asyncio.run(scheduler._dispatch("update_free_sources", "db", "config.yaml"))


def test_dispatch_marks_started_job_failed_when_setup_raises(monkeypatch) -> None:
    failures: list[tuple[str, str, object, str]] = []

    monkeypatch.setattr(
        scheduler,
        "start_refresh_job",
        lambda job_name, _db_path: {"id": "job-1", "job_name": job_name, "created": True},
    )

    async def fail_setup(*_args):
        raise ValueError("invalid config")

    def finish_failed(job_id, job_name, db_path, error):
        failures.append((job_id, job_name, db_path, error))
        return {"id": job_id, "status": "failed"}

    monkeypatch.setattr(scheduler, "_execute_started_refresh_job", fail_setup)
    monkeypatch.setattr(scheduler, "finish_refresh_job_failed", finish_failed)

    asyncio.run(scheduler._dispatch("update_free_sources", "db", "bad-config.yaml"))

    assert failures == [
        (
            "job-1",
            "update_free_sources",
            "db",
            "scheduler failed before refresh execution completed: invalid config",
        )
    ]


def test_execute_started_job_passes_database_url_only_in_process_environment(monkeypatch) -> None:
    captured_specs = []

    async def execute(spec, _fail):
        captured_specs.append(spec)
        return {"status": "succeeded"}

    monkeypatch.setattr(scheduler, "execute_async", execute)

    result = asyncio.run(
        scheduler._execute_started_refresh_job(
            "update_free_sources",
            "job-1",
            "postgresql://user:secret@localhost/market",
            "config.yaml",
        )
    )

    assert result == {"status": "succeeded"}
    assert captured_specs[0].database_url == "postgresql://user:secret@localhost/market"
    assert captured_specs[0].database_reference is None


def test_scheduler_does_not_let_slow_job_starve_market_environment(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_dispatch(job, _db_path, _config_path):
        calls.append(job)
        if job == "slow_job":
            await asyncio.sleep(0.1)

    monkeypatch.setattr(scheduler, "load_config", lambda _path: object())
    monkeypatch.setattr(scheduler, "job_intervals", lambda _config: {"slow_job": 60, "update_market_environment": 60})
    monkeypatch.setattr(scheduler, "_dispatch", fake_dispatch)
    monkeypatch.setattr(scheduler, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(scheduler, "STAGGER_SECONDS", 0)
    monkeypatch.setenv("MARKET_SCHEDULER_WARMUP_SECONDS", "0")

    async def run_briefly() -> None:
        task = asyncio.create_task(scheduler.run_scheduler("db", "config.yaml"))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_briefly())

    assert "slow_job" in calls
    assert "update_market_environment" in calls


def test_deterministic_radar_job_is_allowlisted() -> None:
    # The scheduler's frequent loop depends on an agent-free refresh entry so it
    # never triggers Codex thesis/postmortem workers.
    assert "refresh_options_radar_deterministic" in ALLOWLIST
    assert "update_robinhood_options" in ALLOWLIST
    assert "refresh_options_radar_signal_robinhood" in ALLOWLIST
    assert "premarket_options_intelligence" in ALLOWLIST
    assert "postgres_retention" in ALLOWLIST
    assert "snapshot_database" in ALLOWLIST
