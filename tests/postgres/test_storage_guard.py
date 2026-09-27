from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from investment_panel.infrastructure.postgres.storage_guard import GIB, storage_capacity
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.jobs import robinhood_option_history
from conftest import typed_config


def test_storage_guard_blocks_history_at_or_below_the_reserve(monkeypatch) -> None:
    monkeypatch.setattr(
        "investment_panel.infrastructure.postgres.storage_guard.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=15 * GIB),
    )

    capacity = storage_capacity(path="/fixture")

    assert capacity.history_collection_allowed is False
    assert capacity.reason == "storage_below_minimum_free_space"
    assert capacity.projected_reserve_breach_within_30_trading_days is True


def test_storage_guard_allows_history_only_above_the_reserve(monkeypatch) -> None:
    monkeypatch.setattr(
        "investment_panel.infrastructure.postgres.storage_guard.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=40 * GIB),
    )

    capacity = storage_capacity(path="/fixture")

    assert capacity.history_collection_allowed is True
    assert capacity.reason is None
    assert capacity.projected_free_bytes_after_30_trading_days == 19 * GIB
    assert capacity.projected_reserve_breach_within_30_trading_days is False


def test_storage_guard_blocks_optional_history_on_forecast(monkeypatch) -> None:
    monkeypatch.setattr(
        "investment_panel.infrastructure.postgres.storage_guard.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=40 * GIB),
    )
    capacity = storage_capacity(path="/fixture", forecast_free_bytes=14 * GIB)
    assert capacity.history_collection_allowed is False
    assert capacity.reason == "storage_forecast_below_minimum_free_space"


def test_option_history_job_reports_forecast_blocked_captures(migrated_postgres_dsn, monkeypatch) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    slot = datetime(2026, 9, 25, 14, tzinfo=UTC)
    config = typed_config(migrated_postgres_dsn, raw={
        "data_sources": {"brokers": {"robinhood": {"enabled": True, "history_enabled": True}}},
    })
    monkeypatch.setattr(robinhood_option_history, "load_config", lambda _path: config)
    monkeypatch.setattr(robinhood_option_history, "runtime_for_config", lambda _config: runtime)
    monkeypatch.setattr(robinhood_option_history, "history_slot", lambda _now: slot)
    monkeypatch.setattr(robinhood_option_history.OptionHistoryPolicyRepository, "due_symbols",
                        lambda _self, _now: [{"symbol": "QQQ", "slot_at": slot,
                                              "profile": robinhood_option_history.HISTORY_PROFILE}])
    monkeypatch.setattr(robinhood_option_history.StorageArchiveService, "account",
                        lambda _self, record=False: {"status": "degraded", "path": "/fixture",
                                                     "forecast_30d_free_bytes": 14 * GIB})
    monkeypatch.setattr("investment_panel.infrastructure.postgres.storage_guard.shutil.disk_usage",
                        lambda _path: SimpleNamespace(free=40 * GIB))
    try:
        result = robinhood_option_history.run(now=slot)
        assert result["status"] == "skipped"
        assert result["captures"][0]["reason"] == "storage_forecast_below_minimum_free_space"
    finally:
        runtime.close()
