"""Market-session / RTH helpers for snapshot timestamps."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from investment_panel.core.decision import (is_market_open)

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
    return bool(parsed and is_market_open(parsed))


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
