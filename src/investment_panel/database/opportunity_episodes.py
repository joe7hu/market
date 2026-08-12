"""Stable, lane-local identities for independent option opportunities."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from zoneinfo import ZoneInfo


MARKET_TZ = ZoneInfo("America/New_York")


def option_episode_key(
    *,
    lane: str,
    symbol: str,
    strategy: str,
    contract_ladder_slot: str,
    entry_at: datetime,
    event_id: str | None = None,
) -> str:
    """Return one reproducible key for a lane-local executable entry window.

    An observation can be repeated many times in a capture loop.  The key has
    the event (when one exists), underlying, stable contract/ladder slot,
    strategy, local trading date, and one-hour entry window.  It is not a row
    identifier and must never be used to count repeated observations.
    """

    if entry_at.tzinfo is None:
        raise ValueError("episode entry time must be timezone-aware")
    local = entry_at.astimezone(MARKET_TZ)
    payload = {
        "lane": str(lane).strip().lower(),
        "event_id": str(event_id or ""),
        "symbol": str(symbol).strip().upper(),
        "contract_ladder_slot": str(contract_ladder_slot).strip(),
        "strategy": str(strategy).strip(),
        "trading_date": local.date().isoformat(),
        "entry_window": local.replace(minute=0, second=0, microsecond=0).isoformat(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:24]
    return f"{payload['lane']}:{digest}"


def normalized_entry_at(value: datetime) -> datetime:
    """Return a UTC timestamp for callers that accept external timestamps."""

    if value.tzinfo is None:
        raise ValueError("entry time must be timezone-aware")
    return value.astimezone(UTC)
