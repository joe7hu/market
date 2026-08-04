"""Shared date and numeric helpers for recovery event persistence."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from statistics import quantiles
from typing import Any

from investment_panel.core.decision import MARKET_TZ, is_us_market_day


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return quantiles(values, n=100, method="inclusive")[94]


def number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def fraction(value: Any) -> float | None:
    numeric = number(value)
    return numeric / 100.0 if numeric is not None else None


def option_quote_session_cutoffs(reference: datetime) -> tuple[datetime, datetime]:
    """Start boundaries for one and five exact US trading-session windows."""

    local = reference.astimezone(MARKET_TZ)
    cursor = local.date()
    while not is_us_market_day(cursor):
        cursor -= timedelta(days=1)
    sessions = [cursor]
    while len(sessions) < 5:
        cursor -= timedelta(days=1)
        if is_us_market_day(cursor):
            sessions.append(cursor)
    one = datetime.combine(sessions[0], time.min, MARKET_TZ).astimezone(UTC)
    five = datetime.combine(sessions[-1], time.min, MARKET_TZ).astimezone(UTC)
    return one, five
