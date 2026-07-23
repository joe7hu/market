from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import app.deps as app_deps
from app.main import app
from investment_panel.database.options_history_policy import (
    MAX_PROVIDER_LEASES,
    OptionHistoryPolicyRepository,
    PolicyConflict,
    apply_publication_cap,
    eligible_policy_slot,
)
from investment_panel.database.runtime import DatabaseRuntime


def test_policy_seeds_qqq_and_nvda_with_locked_retention(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        repository = OptionHistoryPolicyRepository(runtime)
        payload = repository.symbols()
        rows = {row["symbol"]: row for row in payload["rows"]}
        assert rows["QQQ"]["effective_state"] == "active"
        assert rows["QQQ"]["collection_tier"] == "core"
        assert rows["QQQ"]["cadence_minutes"] == 15
        assert rows["QQQ"]["publication_cap"] == "PAPER_READY"
        assert rows["NVDA"]["effective_state"] == "shadow"
        assert rows["NVDA"]["cadence_minutes"] == 60
        assert rows["NVDA"]["publication_cap"] == "WATCH"
        assert rows["NVDA"]["provider_payload_retention_days"] == 90
        assert rows["NVDA"]["normalized_retention_days"] == 730
    finally:
        runtime.close()


def test_watchlist_toggle_is_optimistic_and_preserves_policy_row(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        repository = OptionHistoryPolicyRepository(runtime)
        nvda = repository.policy_for_symbol("nvda")
        assert nvda is not None
        off = repository.set_requested_state("NVDA", requested_state="off", lock_version=nvda["lock_version"])
        assert off["requested_state"] == "off"
        assert off["effective_state"] == "disabled"
        assert off["lock_version"] == nvda["lock_version"] + 1
        with pytest.raises(PolicyConflict):
            repository.set_requested_state("NVDA", requested_state="on", lock_version=nvda["lock_version"])
        on = repository.set_requested_state("NVDA", requested_state="on", lock_version=off["lock_version"])
        assert on["requested_state"] == "on"
        assert on["effective_state"] == "pending_gate"
    finally:
        runtime.close()


def test_policy_due_slots_keep_core_15_and_standard_hourly() -> None:
    now = datetime(2026, 7, 20, 14, 45, tzinfo=UTC)
    assert eligible_policy_slot(now, cadence_minutes=15) == datetime(2026, 7, 20, 14, 45, tzinfo=UTC)
    assert eligible_policy_slot(now, cadence_minutes=60) == datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    close_grace = datetime(2026, 7, 20, 20, 5, tzinfo=UTC)
    assert eligible_policy_slot(close_grace, cadence_minutes=60) == datetime(2026, 7, 20, 20, 0, tzinfo=UTC)
    assert eligible_policy_slot(datetime(2026, 7, 19, 14, 45, tzinfo=UTC), cadence_minutes=15) is None


def test_provider_leases_enforce_two_concurrent_pulls(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        repository = OptionHistoryPolicyRepository(runtime)
        leases = [
            repository.acquire_provider_lease(provider="robinhood", workload=f"workload-{index}", symbol="QQQ")
            for index in range(MAX_PROVIDER_LEASES)
        ]
        assert all(lease is not None for lease in leases)
        assert repository.acquire_provider_lease(provider="robinhood", workload="nvda", symbol="NVDA") is None
        repository.release_provider_lease(leases[0].id)  # type: ignore[union-attr]
        assert repository.acquire_provider_lease(provider="robinhood", workload="nvda", symbol="NVDA") is not None
    finally:
        runtime.close()


def test_provider_leases_prioritize_radar_over_shadow_history(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        repository = OptionHistoryPolicyRepository(runtime)
        radar = repository.acquire_provider_lease(provider="robinhood", workload="options_radar", symbol="RADAR")
        assert radar is not None
        assert repository.acquire_provider_lease(provider="robinhood", workload="option_history", symbol="NVDA") is None
        qqq = repository.acquire_provider_lease(provider="robinhood", workload="option_history", symbol="QQQ")
        assert qqq is not None
    finally:
        runtime.close()


def test_provider_lease_heartbeat_extends_active_work(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        repository = OptionHistoryPolicyRepository(runtime)
        lease = repository.acquire_provider_lease(
            provider="robinhood", workload="options_radar", symbol="RADAR", ttl_seconds=60
        )
        assert lease is not None
        assert repository.heartbeat_provider_lease(lease.id, ttl_seconds=3600)
        with runtime.read() as connection:
            row = connection.execute("SELECT expires_at FROM ops.provider_lease WHERE id = %s", [lease.id]).fetchone()
        assert row["expires_at"] > lease.expires_at
    finally:
        runtime.close()


def test_shadow_publication_cap_downgrades_only_paper_ready() -> None:
    ready = {"paper_state": "PAPER_READY", "blockers": [], "reasons": []}
    capped = apply_publication_cap(ready, {"publication_cap": "WATCH"})
    assert capped["computed_paper_state"] == "PAPER_READY"
    assert capped["paper_state"] == "WATCH"
    assert "symbol_shadow_only" in capped["blockers"]
    assert apply_publication_cap({"paper_state": "WATCH", "blockers": [], "reasons": []}, {"publication_cap": "WATCH"})["paper_state"] == "WATCH"


def test_options_history_policy_api_and_conflict(
    migrated_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_deps, "load_config", lambda _path=None: {"database": {"url": migrated_postgres_dsn}})
    client = TestClient(app)
    symbols = client.get("/api/options/history/symbols")
    assert symbols.status_code == 200
    nvda = next(row for row in symbols.json()["rows"] if row["symbol"] == "NVDA")
    off = client.patch(
        "/api/watchlist/symbols/NVDA/options-history",
        json={"requested_state": "off", "lock_version": nvda["lock_version"]},
    )
    assert off.status_code == 200
    assert off.json()["options_history_policy"]["effective_state"] == "disabled"
    conflict = client.patch(
        "/api/watchlist/symbols/NVDA/options-history",
        json={"requested_state": "on", "lock_version": nvda["lock_version"]},
    )
    assert conflict.status_code == 409
