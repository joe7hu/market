"""Fail-closed free-space guard for write-heavy option collectors.

The option chain history collector can add gigabytes in one full trading day.
It must stop expanding full-chain history before the local data volume is full.
This is intentionally a filesystem guard, rather than a PostgreSQL size guess:
the database, WAL, temporary files, and archive staging all compete for the
same volume.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import shutil


GIB = 1024**3
DEFAULT_MIN_FREE_GIB = 30
DEFAULT_STORAGE_PATH = "/Users/joehu/proj/market"
DEFAULT_OPTION_HISTORY_GROWTH_GIB_PER_TRADING_DAY = 0.7
HOT_OPTION_RETENTION_DAYS = 7


@dataclass(frozen=True)
class StorageCapacity:
    path: str
    available_bytes: int | None
    minimum_free_bytes: int
    history_collection_allowed: bool
    reason: str | None
    option_history_growth_gib_per_trading_day: float
    projected_free_bytes_after_30_trading_days: int | None
    projected_reserve_breach_within_30_trading_days: bool
    hot_option_retention_days: int
    steady_state_hot_storage_bytes: int | None
    projected_free_bytes_after_hot_retention: int | None

    def payload(self) -> dict[str, object]:
        return asdict(self)


def storage_capacity(*, path: str | Path | None = None, minimum_free_gib: int | None = None) -> StorageCapacity:
    """Return the bounded capacity state without changing files or storage.

    Missing or unreadable storage is treated as unavailable.  Event/ticket
    strips are small and safety-critical; callers use the
    ``history_collection_allowed`` result only to block full-history expansion.
    """

    target = Path(path or os.environ.get("MARKET_STORAGE_GUARD_PATH") or DEFAULT_STORAGE_PATH)
    raw_minimum = minimum_free_gib
    if raw_minimum is None:
        raw_minimum = _positive_int(os.environ.get("MARKET_MIN_FREE_STORAGE_GIB"), DEFAULT_MIN_FREE_GIB)
    minimum = int(raw_minimum) * GIB
    growth_gib = _positive_float(
        os.environ.get("MARKET_OPTION_HISTORY_GROWTH_GIB_PER_TRADING_DAY"),
        DEFAULT_OPTION_HISTORY_GROWTH_GIB_PER_TRADING_DAY,
    )
    try:
        available = int(shutil.disk_usage(target).free)
    except OSError as exc:
        return StorageCapacity(
            path=str(target),
            available_bytes=None,
            minimum_free_bytes=minimum,
            history_collection_allowed=False,
            reason=f"storage_unavailable:{type(exc).__name__}",
            option_history_growth_gib_per_trading_day=growth_gib,
            projected_free_bytes_after_30_trading_days=None,
            projected_reserve_breach_within_30_trading_days=True,
            hot_option_retention_days=HOT_OPTION_RETENTION_DAYS,
            steady_state_hot_storage_bytes=None,
            projected_free_bytes_after_hot_retention=None,
        )
    allowed = available > minimum
    projected = max(0, available - int(growth_gib * 30 * GIB))
    steady_state = int(growth_gib * HOT_OPTION_RETENTION_DAYS * GIB)
    return StorageCapacity(
        path=str(target),
        available_bytes=available,
        minimum_free_bytes=minimum,
        history_collection_allowed=allowed,
        reason=None if allowed else "storage_below_minimum_free_space",
        option_history_growth_gib_per_trading_day=growth_gib,
        projected_free_bytes_after_30_trading_days=projected,
        projected_reserve_breach_within_30_trading_days=projected <= minimum,
        hot_option_retention_days=HOT_OPTION_RETENTION_DAYS,
        steady_state_hot_storage_bytes=steady_state,
        projected_free_bytes_after_hot_retention=max(0, available - steady_state),
    )


def _positive_int(value: str | None, fallback: int) -> int:
    try:
        parsed = int(value or fallback)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _positive_float(value: str | None, fallback: float) -> float:
    try:
        parsed = float(value or fallback)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback
