"""Canonical timestamp normalization for Event Scout observations."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any


def parse_event_timestamp(value: Any, *, default: datetime | None = None) -> datetime | None:
    """Normalize an event timestamp to UTC, or return the explicit default."""

    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=UTC)
    else:
        text = str(value).strip()
        if not text:
            return default
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = ["parse_event_timestamp"]
