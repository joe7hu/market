from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app import scheduler
from investment_panel.core.refresh_jobs import ALLOWLIST


def test_scheduler_enabled_defaults_on(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_SCHEDULER_ENABLED", raising=False)
    assert scheduler.scheduler_enabled() is True


def test_scheduler_enabled_respects_off_values(monkeypatch) -> None:
    for value in ("0", "false", "off", "no", "OFF"):
        monkeypatch.setenv("MARKET_SCHEDULER_ENABLED", value)
        assert scheduler.scheduler_enabled() is False


def test_job_intervals_default_to_robinhood_source(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_SOURCE_REFRESH_SECONDS", "120")
    monkeypatch.setenv("MARKET_RADAR_REFRESH_SECONDS", "60")
    intervals = scheduler.job_intervals()
    assert intervals["update_robinhood_options"] == 120
    assert intervals["refresh_options_radar_signal_robinhood"] == 60


def test_ibkr_source_fallback_via_env(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_RADAR_OPTION_SOURCE", "ibkr")
    intervals = scheduler.job_intervals()
    assert "update_ibkr_options" in intervals
    assert "refresh_options_radar_signal_ibkr" in intervals
    assert "update_robinhood_options" not in intervals


def test_free_source_fallback_via_env(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_RADAR_OPTION_SOURCE", "free")
    intervals = scheduler.job_intervals()
    assert "update_free_sources_radar" in intervals
    assert "refresh_options_radar_signal" in intervals
    assert "update_ibkr_options" not in intervals


def test_job_intervals_ignore_invalid_env(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_SOURCE_REFRESH_SECONDS", "not-a-number")
    monkeypatch.setenv("MARKET_RADAR_REFRESH_SECONDS", "-5")
    intervals = scheduler.job_intervals()
    assert intervals["update_robinhood_options"] == 3600  # default
    assert intervals["refresh_options_radar_signal_robinhood"] == 900  # default


def test_source_pull_can_be_disabled(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_SOURCE_REFRESH_SECONDS", "0")
    intervals = scheduler.job_intervals()
    assert "update_robinhood_options" not in intervals
    # The continuous fresh-signal loop must remain regardless.
    assert "refresh_options_radar_signal_robinhood" in intervals


def test_signal_loop_always_scheduled(monkeypatch) -> None:
    for var in (
        "MARKET_RADAR_OPTION_SOURCE",
        "MARKET_RADAR_REFRESH_SECONDS",
        "MARKET_SOURCE_REFRESH_SECONDS",
        "MARKET_LEARNING_REFRESH_SECONDS",
        "MARKET_ENVIRONMENT_REFRESH_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    intervals = scheduler.job_intervals()
    assert intervals["refresh_options_radar_signal_robinhood"] == 900
    assert intervals["update_robinhood_options"] == 3600
    assert intervals["refresh_options_radar_deterministic"] == 21600  # heavy learning, slow cadence
    assert intervals["update_market_environment"] == 3600


def test_market_environment_refresh_can_be_disabled(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_ENVIRONMENT_REFRESH_SECONDS", "0")
    intervals = scheduler.job_intervals()
    assert "update_market_environment" not in intervals


def test_agent_pass_on_by_default_daily(monkeypatch) -> None:
    for var in ("MARKET_RADAR_OPTION_SOURCE", "MARKET_AGENT_REFRESH_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    import investment_panel.core.config as config

    monkeypatch.setattr(
        config,
        "load_config",
        lambda: SimpleNamespace(agents=SimpleNamespace(option_agent=SimpleNamespace(enabled=True, auto_run_seconds=0))),
    )
    intervals = scheduler.job_intervals()
    assert intervals["run_option_agents"] == 86400  # daily by default (Phase 2c)


def test_agent_pass_can_be_disabled(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_AGENT_REFRESH_SECONDS", "0")
    intervals = scheduler.job_intervals()
    assert "run_option_agents" not in intervals


def test_learning_refresh_can_be_disabled(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_LEARNING_REFRESH_SECONDS", "0")
    intervals = scheduler.job_intervals()
    assert "refresh_options_radar_deterministic" not in intervals
    assert "refresh_options_radar_signal_robinhood" in intervals  # fast loop stays


def test_dispatch_invokes_run_refresh_job(monkeypatch) -> None:
    calls: list[tuple[str, object, str]] = []

    def fake_run_refresh_job(job_name, db_path, config_path):
        calls.append((job_name, db_path, config_path))
        return {"status": "succeeded"}

    monkeypatch.setattr(scheduler, "run_refresh_job", fake_run_refresh_job)
    asyncio.run(scheduler._dispatch("refresh_options_radar_signal_robinhood", "db", "config.yaml"))

    assert calls == [("refresh_options_radar_signal_robinhood", "db", "config.yaml")]


def test_dispatch_swallows_exceptions(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("job blew up")

    monkeypatch.setattr(scheduler, "run_refresh_job", boom)
    # Must not raise: a bad job can never be allowed to kill the scheduler loop.
    asyncio.run(scheduler._dispatch("update_free_sources", "db", "config.yaml"))


def test_scheduler_does_not_let_slow_job_starve_market_environment(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_dispatch(job, _db_path, _config_path):
        calls.append(job)
        if job == "slow_job":
            await asyncio.sleep(0.1)

    monkeypatch.setattr(scheduler, "job_intervals", lambda: {"slow_job": 60, "update_market_environment": 60})
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
    assert "update_market_environment" in ALLOWLIST
