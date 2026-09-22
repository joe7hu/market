"""Independent source, publication, execution and evidence-collection states."""
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from investment_panel.settings import AppConfig
from investment_panel.infrastructure.postgres.workstation import WORKFLOW_JOBS, WorkstationRepository, configured_workflow_jobs, worker_projection, next_session_open
from investment_panel.api.response_contracts import WorkstationStatus

NOW = datetime(2026, 9, 18, 15, tzinfo=UTC)


@pytest.mark.parametrize("status", ["succeeded", "failed", "partial", "running", "skipped"])
def test_worker_status_is_not_investment_quality(status):
    result = worker_projection({"id": "x", "status": status, "finished_at": NOW,
        "heartbeat_at": NOW, "started_at": NOW - timedelta(seconds=30)},
        job="process_options_paper_orders", interval=15, now=NOW, enabled=True)
    assert result["status"] == status
    assert result["next_expected_at"] == (None if status == "running" else NOW + timedelta(seconds=15))


def test_source_success_with_downstream_failure_stays_partial():
    result = worker_projection({"status": "partial", "started_at": NOW, "finished_at": NOW,
        "source_status": "ok", "downstream_status": "failed"},
        job="update_market_data", interval=3600, now=NOW, enabled=True)
    assert result["status"] == "partial"
    assert result["source_status"] == "ok" and result["downstream_status"] == "failed"


@pytest.mark.parametrize("enabled,interval,expected", [(False, 15, "disabled"), (True, None, "disabled"), (True, 15, "not_started")])
def test_never_run_is_not_success(enabled, interval, expected):
    assert worker_projection(None, job="j", interval=interval, now=NOW, enabled=enabled)["status"] == expected


def test_due_worker_and_weekend_are_explicit():
    result = worker_projection({"status": "running", "heartbeat_at": NOW - timedelta(minutes=10)},
        job="j", interval=15, now=NOW, enabled=True)
    assert result["status"] == "overdue"
    assert next_session_open(datetime(2026, 9, 19, 15, tzinfo=UTC)) == datetime(2026, 9, 21, 13, 30, tzinfo=UTC)


@pytest.mark.parametrize("failed_table", [None, "ops.job_run", "app.paper_order", "analysis.shadow_trade"])
def test_read_failure_is_not_reported_as_zero_or_healthy(failed_table):
    class Connection:
        def transaction(self):
            return nullcontext(self)
        def execute(self, sql, params=None):
            if failed_table and failed_table in sql:
                raise RuntimeError("unavailable test relation")
            return SimpleNamespace(fetchall=lambda: [])
    runtime = SimpleNamespace(snapshot=lambda *args: nullcontext(Connection()))
    status = WorkstationRepository(runtime).status(AppConfig())
    WorkstationStatus.model_validate(status)
    assert status["status"] == ("unavailable" if failed_table else "partial")
    # Successful empty queries do not prove that required producers/publications exist.
    assert status["blockers"]
    assert status["market"]["status"] == "not_published"
    if failed_table == "app.paper_order":
        assert status["paper"]["status"] == "unavailable"
        assert status["paper"]["waiting_status"] == "unavailable"
    if failed_table == "ops.job_run":
        assert all(worker["status"] == "unavailable" for worker in status["workers"])
    if failed_table == "analysis.shadow_trade":
        assert status["observations"]["status"] == "unavailable"


def test_disabled_worker_has_no_expected_dispatch():
    result = worker_projection({"status": "succeeded", "finished_at": NOW},
        job="j", interval=15, now=NOW, enabled=False)
    assert result["status"] == "disabled" and result["next_expected_at"] is None


def test_health_excludes_disabled_generation_but_keeps_claim_settlement():
    config = AppConfig()
    assert {"update_broker_account", "run_continuous_advisor"}.isdisjoint(configured_workflow_jobs(config))
    assert "run_continuous_advisor_replay" in configured_workflow_jobs(config)
    enabled = replace(config, data_sources=replace(config.data_sources, brokers=replace(
        config.data_sources.brokers, ibkr=replace(config.data_sources.brokers.ibkr, enabled=True),
    )), agents=replace(config.agents, thesis_monitor=replace(config.agents.thesis_monitor, continuous_enabled=True)))
    assert configured_workflow_jobs(enabled) == WORKFLOW_JOBS


def market_status(*, unavailable=False, cutoff=NOW, references=1, failures=None):
    from investment_panel.infrastructure.postgres.workstation import BASELINE_MODELS, market_readiness
    counts = dict.fromkeys(BASELINE_MODELS, 1)
    counts["market_valuation_reference_charts"] = references
    drivers = [{"category": name, "current_status": "unavailable" if unavailable else "available",
                "score": None if unavailable else 0, "blockers": ["missing_prices"] if unavailable else []}
               for name in ("Price Trend", "Market Breadth", "Risk Appetite")]
    return market_readiness({"published_at": NOW, "input_cutoff": cutoff}, counts, drivers,
        failures=failures or [], now=NOW, max_age_minutes=1440)


def test_populated_placeholder_models_do_not_establish_market_readiness():
    assert market_status(unavailable=True)["status"] == "partial"
    assert market_status()["status"] == "available"  # a legitimate score of zero remains usable


def test_optional_valuation_does_not_blank_supported_baseline():
    result = market_status(references=0)
    assert result["status"] == "available" and result["valuation_status"] == "not_available"


def test_republishing_an_old_cutoff_does_not_make_it_current():
    result = market_status(cutoff=NOW - timedelta(days=2))
    assert result["status"] == "stale" and result["evidence_session"] < result["expected_session"]


def test_unknown_market_evidence_is_not_counted_as_available():
    assert market_status(failures=["market_drivers"])["status"] == "unavailable"
    assert market_status(cutoff=None)["status"] == "unavailable"


def test_news_macro_and_calendar_are_visible_and_do_not_wait_for_market_open():
    from investment_panel.infrastructure import scheduler
    from investment_panel.infrastructure.postgres.workstation import WORKFLOW_JOBS
    sunday = datetime(2026, 9, 20, 15, tzinfo=UTC)
    for job in ('update_research_sources', 'update_phase2_sources', 'update_event_calendar'):
        assert job in WORKFLOW_JOBS
        assert job not in scheduler.SESSION_JOBS
        assert scheduler._recurring_delay_seconds(job, 3600, reference_time=sunday) == 3600
        projection = worker_projection({'status': 'succeeded', 'finished_at': sunday},
            job=job, interval=3600, now=sunday, enabled=True)
        assert projection['next_expected_at'] == sunday + timedelta(hours=1)


def test_explicitly_disabled_settlement_stays_visible_as_disabled(monkeypatch):
    from investment_panel.core.job_policy import scheduler_intervals
    config = AppConfig()
    monkeypatch.setenv("MARKET_CONTINUOUS_ADVISOR_REPLAY_SECONDS", "0")
    assert "run_continuous_advisor_replay" in configured_workflow_jobs(config)
    assert "run_continuous_advisor_replay" not in scheduler_intervals(config)
    projection = worker_projection(None, job="run_continuous_advisor_replay", interval=None, now=NOW, enabled=True)
    assert projection["status"] == "disabled"
    assert projection["next_expected_at"] is None
