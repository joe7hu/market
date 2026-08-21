from __future__ import annotations

from types import SimpleNamespace

from investment_panel.database.storage_guard import GIB, storage_capacity


def test_storage_guard_blocks_history_at_or_below_the_reserve(monkeypatch) -> None:
    monkeypatch.setattr(
        "investment_panel.database.storage_guard.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=30 * GIB),
    )

    capacity = storage_capacity(path="/fixture", minimum_free_gib=30)

    assert capacity.history_collection_allowed is False
    assert capacity.reason == "storage_below_minimum_free_space"
    assert capacity.projected_reserve_breach_within_30_trading_days is True


def test_storage_guard_allows_history_only_above_the_reserve(monkeypatch) -> None:
    monkeypatch.setattr(
        "investment_panel.database.storage_guard.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=31 * GIB),
    )

    capacity = storage_capacity(path="/fixture", minimum_free_gib=30)

    assert capacity.history_collection_allowed is True
    assert capacity.reason is None
    assert capacity.projected_free_bytes_after_30_trading_days == 10 * GIB
    assert capacity.projected_reserve_breach_within_30_trading_days is True
