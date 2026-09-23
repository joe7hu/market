from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from investment_panel.infrastructure import scheduler
from investment_panel.core import job_policy
from investment_panel.core.refresh_jobs import ALLOWLIST
from conftest import typed_config


def test_scheduler_enabled_defaults_on(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_SCHEDULER_ENABLED", raising=False)
    assert scheduler.scheduler_enabled() is True


def test_scheduler_enabled_respects_off_values(monkeypatch) -> None:
    for value in ("0", "false", "off", "no", "OFF"):
        monkeypatch.setenv("MARKET_SCHEDULER_ENABLED", value)
        assert scheduler.scheduler_enabled() is False


def test_job_intervals_refresh_quotes_within_paper_entry_window(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.delenv("MARKET_SOURCE_REFRESH_SECONDS", raising=False)
    monkeypatch.delenv("MARKET_RADAR_REFRESH_SECONDS", raising=False)
    monkeypatch.delenv("MARKET_OPTIONS_RADAR_HARD_REFRESH_SECONDS", raising=False)
    intervals = scheduler.job_intervals()
    assert intervals["options_radar_hard_refresh"] == 900


def test_operational_source_refreshes_default_on(monkeypatch) -> None:
    for variable in (
        "MARKET_SOCIAL_REFRESH_SECONDS",
        "MARKET_RESEARCH_REFRESH_SECONDS",
        "MARKET_ARCO_REFRESH_SECONDS",
        "MARKET_MARKET_DATA_REFRESH_SECONDS",
        "MARKET_MUNGERMODE_REFRESH_SECONDS",
        "MARKET_STOCK_ALPHA_REFRESH_SECONDS",
    ):
        monkeypatch.delenv(variable, raising=False)

    intervals = scheduler.job_intervals()

    assert intervals["update_social_sources"] == 1800
    assert intervals["update_research_sources"] == 3600
    assert intervals["update_arco_data"] == 14400
    assert intervals["update_market_data"] == 3600
    assert intervals["update_phase2_sources"] == 86400
    assert intervals["run_stock_alpha_walk_forward"] == 900
    assert intervals["update_market_valuations"] == 86400


def test_decision_publication_refreshes_after_source_updates(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_DECISION_MODEL_REFRESH_SECONDS", raising=False)
    intervals = scheduler.job_intervals()
    assert intervals["refresh_decision_models"] == 300


def test_decision_publication_refresh_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_DECISION_MODEL_REFRESH_SECONDS", "0")
    assert "refresh_decision_models" not in scheduler.job_intervals()


def test_event_and_disclosure_refreshes_default_to_daily(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_EVENT_CALENDAR_REFRESH_SECONDS", raising=False)
    monkeypatch.delenv("MARKET_DISCLOSURE_REFRESH_SECONDS", raising=False)

    intervals = scheduler.job_intervals()

    assert intervals["update_event_calendar"] == 86400
    assert intervals["update_disclosures"] == 86400


def test_generic_paper_manager_runs_with_entry_switches_off_for_safe_exits(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_OPTIONS_PAPER_EXECUTION_SECONDS", raising=False)
    intervals = scheduler.job_intervals()
    assert intervals["process_options_paper_orders"] == 15


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


def test_radar_quote_and_learning_loops_default_on(monkeypatch) -> None:
    for var in (
        "MARKET_RADAR_OPTION_SOURCE",
        "MARKET_IN_PROCESS_HEAVY_REFRESH",
        "MARKET_RADAR_REFRESH_SECONDS",
        "MARKET_SOURCE_REFRESH_SECONDS",
        "MARKET_LEARNING_REFRESH_SECONDS",
        "MARKET_PREOPEN_BRIEF_REFRESH_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    intervals = scheduler.job_intervals()
    assert intervals["options_radar_hard_refresh"] == 900
    assert intervals["refresh_options_radar_learning_marks"] == 3600
    assert "refresh_options_radar_deterministic" not in intervals
    assert "update_market_environment" not in intervals
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


def test_preopen_brief_refresh_can_be_disabled(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.setenv("MARKET_PREOPEN_BRIEF_REFRESH_SECONDS", "0")
    intervals = scheduler.job_intervals()
    assert "update_preopen_daily_brief_scheduled" not in intervals


def test_postgresql_market_and_preopen_refreshes_are_scheduled(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_PREOPEN_BRIEF_REFRESH_SECONDS", "300")
    intervals = scheduler.job_intervals()
    assert "update_market_environment" not in intervals
    assert intervals["update_preopen_daily_brief_scheduled"] == 300


def test_agent_pass_on_by_default_daily(monkeypatch) -> None:
    for var in ("MARKET_RADAR_OPTION_SOURCE", "MARKET_AGENT_REFRESH_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MARKET_IN_PROCESS_HEAVY_REFRESH", "1")
    monkeypatch.setattr(
        job_policy,
        "load_config",
        lambda: typed_config(raw={"agents": {"option_agent": {"enabled": True, "auto_run_seconds": 0}}}),
    )
    intervals = scheduler.job_intervals()
    assert intervals["run_option_agents"] == 86400  # daily by default (Phase 2c)


def test_scheduler_status_reports_actual_intervals(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_RADAR_OPTION_SOURCE", raising=False)
    monkeypatch.delenv("MARKET_AGENT_REFRESH_SECONDS", raising=False)
    monkeypatch.setenv("MARKET_IN_PROCESS_HEAVY_REFRESH", "1")
    status = scheduler.scheduler_status(
        typed_config(raw={"agents": {"option_agent": {"enabled": True, "auto_run_seconds": 123}}})
    )

    assert status["agent_refresh_seconds"] == "123"
    assert status["radar_refresh_seconds"] == "900"
    assert status["source_refresh_seconds"] == "900"
    assert status["options_hard_refresh_seconds"] == "900"
    assert status["learning_mark_refresh_seconds"] == "3600"
    assert status["learning_refresh_seconds"] == "21600"
    assert status["market_environment_refresh_seconds"] == "3600"
    assert status["preopen_brief_refresh_seconds"] == "0"
    assert status["decision_model_refresh_seconds"] == "300"
    assert status["external_jobs"]["premarket_options_intelligence"]["owner"] == "launchd"
    assert status["external_jobs"]["premarket_options_intelligence"]["market_calendar_gated"] is True
    assert status["jobs"]["run_option_agents"] == 123


def test_continuous_advisor_settings_refresh_without_restart(monkeypatch) -> None:
    intervals = {
        "run_continuous_advisor": 7_200,
        "run_continuous_advisor_replay": 900,
        "run_continuous_advisor_evolution": 86_400,
        "other_job": 60,
    }
    next_due = {job: 1.0 for job in intervals}
    next_due_wall = {job: datetime(2026, 7, 20, tzinfo=ZoneInfo("UTC")) for job in intervals}
    monkeypatch.setattr(
        scheduler,
        "load_config",
        lambda _path: object(),
    )
    monkeypatch.setattr(
        scheduler,
        "job_intervals",
        lambda _config: {"run_continuous_advisor": 300, "run_continuous_advisor_replay": 600, "run_continuous_advisor_evolution": 1_800},
    )

    scheduler._refresh_continuous_advisor_intervals(
        "config.yaml",
        intervals,
        next_due,
        next_due_wall,
        {},
        now=100.0,
        wall_now=datetime(2026, 7, 20, 9, 30, tzinfo=ZoneInfo("America/New_York")),
    )

    assert intervals["run_continuous_advisor"] == 300
    assert intervals["run_continuous_advisor_replay"] == 600
    assert intervals["run_continuous_advisor_evolution"] == 1_800
    assert next_due["run_continuous_advisor"] == 100.0
    assert "other_job" in intervals


def test_source_writers_wait_one_interval_before_first_run() -> None:
    assert scheduler._initial_delay_seconds("options_radar_hard_refresh", 900, 0,
        reference_time=datetime(2026, 7, 20, 9, 30, tzinfo=ZoneInfo("America/New_York"))) == 0
    assert scheduler._initial_delay_seconds("update_robinhood_options", 120, 1) == 120
    assert scheduler._initial_delay_seconds("refresh_options_radar_signal_robinhood", 60, 2) == 2 * scheduler.STAGGER_SECONDS


def test_overdue_source_writers_start_immediately_but_staggered() -> None:
    assert scheduler._startup_delay_seconds(
        "update_event_calendar", 86400, 0, overdue_jobs={"update_event_calendar"}
    ) == 0
    assert scheduler._startup_delay_seconds(
        "update_disclosures", 86400, 1, overdue_jobs={"update_disclosures"}
    ) == scheduler.STAGGER_SECONDS
    assert scheduler._startup_delay_seconds(
        "options_radar_hard_refresh", 900, 0, overdue_jobs=set(),
        reference_time=datetime(2026, 7, 20, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    ) == 0


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


def test_result_retry_delay_can_bring_a_partial_collector_forward() -> None:
    assert scheduler._retry_delay_seconds({"summary": {"retry_after_seconds": 300}}, 3600) == 300
    assert scheduler._retry_delay_seconds({"summary": {"retry_after_seconds": "bad"}}, 3600) == 3600


def test_retryable_partial_collector_blocks_an_already_due_successor() -> None:
    now = 100.0
    next_due = {"update_market_data": now + 300, "refresh_symbol_features": now}
    next_due_wall = {job: datetime(2026, 9, 22, 17, tzinfo=ZoneInfo("America/New_York")) for job in next_due}

    scheduler._schedule_pipeline_successor(
        "update_market_data", {"status": "partial", "summary": {"retry_after_seconds": 300}},
        next_due, next_due_wall, now=now, wall_now=next_due_wall["update_market_data"],
    )

    assert next_due["refresh_symbol_features"] == now
    assert scheduler._pipeline_waiting_on_upstream(
        "refresh_symbol_features", next_due, {}, now=now, retry_fences={"update_market_data"},
    )


def test_terminal_bar_retry_blocks_independent_market_publication() -> None:
    now = 100.0
    next_due = {"update_market_data": now + 300, "refresh_market_publication": now}

    assert scheduler._pipeline_waiting_on_upstream(
        "refresh_market_publication", next_due, {}, now=now, retry_fences={"update_market_data"},
    )


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_persisted_terminal_bar_retry_fence_is_restored_and_cleared(status: str) -> None:
    now = 100.0
    wall_now = datetime(2026, 9, 22, 17, tzinfo=ZoneInfo("America/New_York"))
    next_due = {"update_market_data": now + 3600, "refresh_symbol_features": now}
    next_due_wall = {job: wall_now for job in next_due}
    retry_fences: set[str] = set()
    scheduler._apply_retry_fences(
        [{
            "job_name": "update_market_data", "status": status, "finished_at": wall_now - timedelta(seconds=60),
            "summary": {"retry_after_seconds": 300, "terminal_bar_checked": True},
        }],
        next_due, next_due_wall, retry_fences, now=now, wall_now=wall_now,
    )

    assert retry_fences == {"update_market_data"}
    assert next_due["update_market_data"] == now + 240
    assert scheduler._pipeline_waiting_on_upstream(
        "refresh_symbol_features", next_due, {}, now=now, retry_fences=retry_fences,
    )

    scheduler._apply_retry_fences(
        [{
            "job_name": "update_market_data", "status": "succeeded", "finished_at": wall_now,
            "summary": {"terminal_bar_checked": True},
        }],
        next_due, next_due_wall, retry_fences, now=now, wall_now=wall_now,
    )

    assert retry_fences == set()


@pytest.mark.parametrize(
    "job_name",
    [
        "full_market_refresh", "daily_screen", "update_free_sources", "update_free_sources_radar",
        "refresh_market_publication", "refresh_decision_models", "update_market_valuations", "update_decision_models",
        "market-refresh-decision-models", "market-publish-ticker-decisions", "premarket_options_intelligence",
    ],
)
def test_terminal_bar_retry_alias_reschedules_the_canonical_collector(job_name: str) -> None:
    now = 100.0
    wall_now = datetime(2026, 9, 22, 17, tzinfo=ZoneInfo("America/New_York"))
    next_due = {"update_market_data": now + 3600, "refresh_symbol_features": now}
    next_due_wall = {job: wall_now for job in next_due}
    retry_fences: set[str] = set()

    scheduler._apply_retry_fences(
        [{
            "job_name": job_name, "status": "partial", "finished_at": wall_now,
            "summary": {"retry_after_seconds": 300, "terminal_bar_checked": True},
        }],
        next_due, next_due_wall, retry_fences, now=now, wall_now=wall_now,
    )

    assert retry_fences == {"update_market_data"}
    assert next_due["update_market_data"] == now + 300
    assert scheduler._pipeline_waiting_on_upstream(
        "refresh_symbol_features", next_due, {}, now=now, retry_fences=retry_fences,
    )


def test_retrying_alias_waits_for_the_canonical_collector(monkeypatch) -> None:
    async def scenario() -> None:
        calls: list[str] = []
        collector_started = asyncio.Event()

        async def fake_dispatch(job, _db_path, _config_path, **_kwargs):
            calls.append(job)
            if job == "update_market_valuations":
                return {"status": "partial", "summary": {"retry_after_seconds": 0.05}}
            if job == "update_market_data":
                collector_started.set()
                return {"status": "succeeded", "summary": {"terminal_bar_checked": True}}
            return {"status": "succeeded"}

        monkeypatch.setattr(scheduler, "load_config", lambda _path: object())
        monkeypatch.setattr(
            scheduler,
            "job_intervals",
            lambda _config: {"update_market_valuations": 1, "update_market_data": 1},
        )
        monkeypatch.setattr(
            scheduler,
            "_startup_delay_seconds",
            lambda job, *_args, **_kwargs: 0 if job == "update_market_valuations" else 3_600,
        )
        monkeypatch.setattr(scheduler, "_dispatch", fake_dispatch)
        monkeypatch.setattr(scheduler, "mark_stale_running_jobs", lambda _db_path: 0)
        monkeypatch.setattr(scheduler, "overdue_source_refresh_jobs", lambda _db_path: set())
        monkeypatch.setattr(scheduler, "refresh_job_rows", lambda _db_path, **_kwargs: [])
        monkeypatch.setattr(scheduler, "TICK_SECONDS", 0.01)
        monkeypatch.setenv("MARKET_SCHEDULER_WARMUP_SECONDS", "0")

        task = asyncio.create_task(scheduler.run_scheduler("db", "config.yaml"))
        try:
            await asyncio.wait_for(collector_started.wait(), 1)
            await asyncio.sleep(0.1)
            assert calls == ["update_market_valuations", "update_market_data"]
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


def test_market_data_pipeline_dispatches_source_feature_then_decision(monkeypatch) -> None:
    async def scenario() -> None:
        calls: list[str] = []
        source_started, feature_started = asyncio.Event(), asyncio.Event()
        release_source, release_feature = asyncio.Event(), asyncio.Event()

        async def fake_dispatch(job, _db_path, _config_path, **_kwargs):
            calls.append(job)
            if job == "update_market_data":
                source_started.set()
                await release_source.wait()
            elif job == "refresh_symbol_features":
                feature_started.set()
                await release_feature.wait()
            return {"status": "partial"}

        monkeypatch.setattr(scheduler, "load_config", lambda _path: object())
        monkeypatch.setattr(scheduler, "job_intervals", lambda _config: {
            "refresh_decision_models": 60,
            "refresh_symbol_features": 60,
            "update_market_data": 60,
        })
        monkeypatch.setattr(scheduler, "_dispatch", fake_dispatch)
        monkeypatch.setattr(scheduler, "mark_stale_running_jobs", lambda _db_path: 0)
        monkeypatch.setattr(scheduler, "overdue_source_refresh_jobs", lambda _db_path: set())
        monkeypatch.setattr(scheduler, "refresh_job_rows", lambda _db_path, **_kwargs: [])
        monkeypatch.setattr(scheduler, "TICK_SECONDS", 0.01)
        monkeypatch.setattr(scheduler, "STAGGER_SECONDS", 5)
        monkeypatch.setenv("MARKET_SCHEDULER_WARMUP_SECONDS", "0")

        task = asyncio.create_task(scheduler.run_scheduler("db", "config.yaml"))
        try:
            await asyncio.wait_for(source_started.wait(), 1)
            await asyncio.sleep(0.03)
            assert calls == ["update_market_data"]
            release_source.set()
            await asyncio.wait_for(feature_started.wait(), 1)
            await asyncio.sleep(0.03)
            assert calls == ["update_market_data", "refresh_symbol_features"]
            release_feature.set()
            for _ in range(100):
                if len(calls) == 3:
                    break
                await asyncio.sleep(0.01)
            assert calls == ["update_market_data", "refresh_symbol_features", "refresh_decision_models"]
        finally:
            release_source.set()
            release_feature.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())
    assert "update_market_data" in scheduler.PRIORITY_JOBS


def test_market_data_waits_for_an_in_flight_downstream_stage() -> None:
    assert scheduler._pipeline_waiting_on_descendant(
        "update_market_data", {"refresh_symbol_features": object()},
    )
    assert scheduler._pipeline_waiting_on_descendant(
        "refresh_symbol_features", {"refresh_decision_models": object()},
    )


def test_market_data_does_not_enqueue_a_disabled_feature_stage() -> None:
    now = 100.0
    next_due = {"update_market_data": now + 3600, "refresh_decision_models": now}
    next_due_wall = {
        job: datetime(2026, 9, 21, 16, tzinfo=ZoneInfo("America/New_York"))
        for job in next_due
    }
    scheduler._schedule_pipeline_successor(
        "update_market_data", {"status": "succeeded"}, next_due, next_due_wall,
        now=now, wall_now=next_due_wall["update_market_data"],
    )
    assert "refresh_symbol_features" not in next_due
    assert not scheduler._pipeline_waiting_on_upstream(
        "refresh_decision_models", next_due, {}, now=now,
    )


def test_assessment_quote_refresh_enqueues_decision_publication() -> None:
    now = 100.0
    wall_now = datetime(2026, 9, 21, 16, tzinfo=ZoneInfo("America/New_York"))
    next_due = {
        "refresh_assessment_inputs": now + 300,
        "refresh_decision_models": now + 3600,
    }
    next_due_wall = {job: wall_now for job in next_due}

    scheduler._schedule_pipeline_successor(
        "refresh_assessment_inputs", {"status": "succeeded"}, next_due, next_due_wall,
        now=now, wall_now=wall_now,
    )

    assert next_due["refresh_decision_models"] == now
    assert not scheduler._pipeline_waiting_on_upstream(
        "refresh_decision_models", next_due, {}, now=now,
    )


def test_option_history_recurrence_uses_the_next_quarter_hour_not_the_startup_stagger() -> None:
    eastern = ZoneInfo("America/New_York")
    assert scheduler._recurring_delay_seconds(
        "robinhood_option_history", 900, reference_time=datetime(2026, 7, 20, 9, 30, 5, tzinfo=eastern)
    ) == 895
    assert scheduler._recurring_delay_seconds(
        "robinhood_option_history", 900, reference_time=datetime(2026, 7, 20, 9, 30, tzinfo=eastern)
    ) == 900


def test_continuous_advisor_cadence_continues_outside_equity_hours() -> None:
    cadence = 90 * 60
    market_open = datetime(2026, 7, 20, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    assert scheduler._initial_delay_seconds("run_continuous_advisor", cadence, 0, reference_time=market_open) == 0
    assert scheduler._initial_delay_seconds(
        "run_continuous_advisor", cadence, 0, reference_time=market_open - timedelta(seconds=1)
    ) == 0
    assert scheduler._recurring_delay_seconds(
        "run_continuous_advisor", 720 * 60, reference_time=market_open
    ) == 720 * 60


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


def test_continuous_advisor_subprocess_receives_scheduled_due(monkeypatch) -> None:
    captured_specs = []

    async def execute(spec, _fail):
        captured_specs.append(spec)
        return {"status": "succeeded"}

    monkeypatch.setattr(scheduler, "execute_async", execute)
    due = datetime(2026, 9, 9, 13, 30, tzinfo=ZoneInfo("UTC"))
    result = asyncio.run(
        scheduler._execute_started_refresh_job(
            "run_continuous_advisor", "job-1", "postgresql:///market", "config.yaml", due_at=due
        )
    )

    assert result["status"] == "succeeded"
    assert captured_specs[0].scheduled_due_at == "2026-09-09T13:30:00+00:00"


def test_scheduler_does_not_let_slow_job_starve_market_environment(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_dispatch(job, _db_path, _config_path, **_kwargs):
        calls.append(job)
        if job == "slow_job":
            await asyncio.sleep(0.1)

    monkeypatch.setattr(scheduler, "load_config", lambda _path: object())
    monkeypatch.setattr(scheduler, "job_intervals", lambda _config: {"slow_job": 60, "critical_market_job": 60})
    monkeypatch.setattr(scheduler, "_dispatch", fake_dispatch)
    monkeypatch.setattr(scheduler, "mark_stale_running_jobs", lambda _db_path: 0)
    monkeypatch.setattr(scheduler, "overdue_source_refresh_jobs", lambda _db_path: set())
    monkeypatch.setattr(scheduler, "refresh_job_rows", lambda _db_path, **_kwargs: [])
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
    assert "critical_market_job" in calls


def test_deterministic_radar_job_is_allowlisted() -> None:
    # The scheduler's frequent loop depends on an agent-free refresh entry so it
    # never triggers Codex thesis/postmortem workers.
    assert "refresh_options_radar_deterministic" in ALLOWLIST
    assert "update_robinhood_options" in ALLOWLIST
    assert "refresh_options_radar_signal_robinhood" in ALLOWLIST
    assert "premarket_options_intelligence" in ALLOWLIST
    assert "postgres_retention" in ALLOWLIST
    assert "snapshot_database" in ALLOWLIST


def test_company_financials_refresh_daily_and_can_be_disabled(monkeypatch) -> None:
    monkeypatch.delenv("MARKET_COMPANY_FINANCIALS_REFRESH_SECONDS", raising=False)
    assert scheduler.job_intervals()["update_company_financials"] == 86400
    monkeypatch.setenv("MARKET_COMPANY_FINANCIALS_REFRESH_SECONDS", "0")
    assert "update_company_financials" not in scheduler.job_intervals()


def test_quote_refresh_waits_until_next_regular_session() -> None:
    eastern = ZoneInfo("America/New_York")
    close = datetime(2026, 7, 24, 16, 0, tzinfo=eastern)
    monday = datetime(2026, 7, 27, 9, 30, tzinfo=eastern)
    assert scheduler._initial_delay_seconds("options_radar_hard_refresh", 900, 0,
        reference_time=close) == (monday - close).total_seconds()
    assert scheduler._recurring_delay_seconds("options_radar_hard_refresh", 900,
        reference_time=close) == (monday - close).total_seconds()
