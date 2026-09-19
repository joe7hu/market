"""Independent source, publication, execution and evidence-collection states."""
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from investment_panel.settings import AppConfig
from investment_panel.infrastructure.postgres.workstation import WorkstationRepository, worker_projection, next_session_open
from investment_panel.api.response_contracts import WorkstationStatus

NOW = datetime(2026, 9, 18, 15, tzinfo=UTC)


@pytest.mark.parametrize("status", ["succeeded", "failed", "partial", "running", "skipped"])
def test_worker_status_is_not_investment_quality(status):
    result = worker_projection({"id": "x", "status": status, "finished_at": NOW,
        "heartbeat_at": NOW, "started_at": NOW - timedelta(seconds=30)},
        job="process_options_paper_orders", interval=15, now=NOW, enabled=True)
    assert result["status"] == status
    assert result["next_expected_at"] == NOW + timedelta(seconds=15)


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
    assert (status["status"] == "partial") is bool(failed_table)
    assert status["market"]["status"] == "not_published"
    if failed_table == "app.paper_order":
        assert status["paper"]["status"] == "unavailable"
        assert status["paper"]["waiting_status"] == "unavailable"
    if failed_table == "ops.job_run":
        assert all(worker["status"] == "unavailable" for worker in status["workers"])
    if failed_table == "analysis.shadow_trade":
        assert status["observations"]["status"] == "unavailable"
