"""Market-session / RTH helpers for snapshot timestamps."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from investment_panel.core.decision import (MARKET_CLOSE, MARKET_OPEN, MARKET_TZ, is_market_open, is_us_market_day)

def _parse_utc(value: Any) -> datetime | None:
    """Parse a snapshot timestamp into a tz-aware UTC datetime (naive == UTC)."""

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def market_session(now: datetime | None = None) -> str:
    """Current US equity-options session: 'rth' (regular trading) or 'closed'."""

    reference = now or datetime.now(timezone.utc)
    return "rth" if is_market_open(reference) else "closed"


def snapshot_is_rth(snapshot_time: Any) -> bool:
    """Whether a snapshot's data was captured during regular trading hours."""

    parsed = _parse_utc(snapshot_time)
    if not parsed:
        return False
    local = parsed.astimezone(MARKET_TZ)
    return is_market_open(parsed) or (is_us_market_day(local.date()) and local.time() == MARKET_CLOSE)


def display_snapshot_time(snapshot_times: list[str], now: datetime | None = None) -> str | None:
    """Snapshot to present. During RTH: the newest. When closed: freeze on the
    newest regular-hours snapshot so the radar shows the last tradeable state
    instead of an off-hours volume=0 capture."""

    times = sorted({str(t) for t in snapshot_times if t})
    if not times:
        return None
    if market_session(now) == "rth":
        return times[-1]
    rth = [t for t in times if snapshot_is_rth(t)]
    return rth[-1] if rth else times[-1]


def newest_snapshot_time(snapshot_times: list[str]) -> str | None:
    """Chronologically newest snapshot, tz-aware (string max is unsafe across
    mixed naive/aware ISO timestamps)."""

    times = [t for t in snapshot_times if t]
    if not times:
        return None
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return max(times, key=lambda value: _parse_utc(value) or floor)


def snapshot_session_label(snapshot_time: Any) -> str:
    """Classify a capture as 'regular', 'premarket', 'after-hours', or 'weekend'.

    Lets the UI show the freshest snapshot we have while flagging that it was
    captured outside regular trading hours (so it never silently looks stale)."""

    parsed = _parse_utc(snapshot_time)
    if not parsed:
        return ""
    if snapshot_is_rth(snapshot_time):
        return "regular"
    local = parsed.astimezone(MARKET_TZ)
    if not is_us_market_day(local.date()):
        return "weekend"
    return "premarket" if local.time() < MARKET_OPEN else "after-hours"
