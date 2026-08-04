"""Canonical confirmed daily-price selection shared by history and recovery.

The module intentionally returns no substitute when one of the exact trading
dates is missing.  A stale, duplicate, or later-available bar is a data-quality
failure, never a zero-return fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable

from investment_panel.core.decision import MARKET_TZ, is_us_market_day, market_session_bounds


@dataclass(frozen=True)
class ConfirmedDailyPrice:
    instrument_id: int
    trading_date: date
    close: float
    source_id: str
    observed_at: datetime
    available_at: datetime


def confirmed_daily_bars(
    connection: Any,
    instrument_ids: Iterable[int],
    *,
    as_of: datetime,
) -> dict[int, list[dict[str, Any]]]:
    """One provider-priority confirmed daily bar per instrument/date."""

    ids = sorted({int(item) for item in instrument_ids})
    if not ids:
        return {}
    reference = _utc(as_of)
    rows = connection.execute(
        """
        SELECT DISTINCT ON (instrument_id, trading_date)
               instrument_id, trading_date, close, source_id, observed_at, available_at
        FROM raw.confirmed_price_bar
        WHERE instrument_id = ANY(%s) AND interval = '1d' AND close > 0
          AND observed_at <= %s AND available_at <= %s
        ORDER BY instrument_id, trading_date,
                 CASE source_id
                   WHEN 'polygon' THEN 1
                   WHEN 'yahoo_chart' THEN 2
                   WHEN 'yfinance' THEN 3
                   ELSE 10
                 END,
                 available_at DESC, source_id
        """,
        [ids, reference, reference],
    ).fetchall()
    output: dict[int, list[dict[str, Any]]] = {}
    for raw in rows:
        row = dict(raw)
        output.setdefault(int(row["instrument_id"]), []).append(row)
    return output


def latest_completed_references(
    connection: Any,
    instrument_id: int,
    *,
    as_of: datetime,
    count: int = 3,
) -> tuple[ConfirmedDailyPrice, ...] | None:
    """Return latest completed close plus exact preceding US trading dates.

    ``None`` means the series is unsafe: a date is absent, non-distinct, or
    only became available after the decision cutoff.
    """

    reference = _utc(as_of)
    desired = completed_trading_dates(reference, count=count)
    if len(desired) != count:
        return None
    rows = confirmed_daily_bars(connection, [instrument_id], as_of=reference).get(int(instrument_id), [])
    by_date = {row["trading_date"]: row for row in rows}
    selected: list[ConfirmedDailyPrice] = []
    for trading_date in desired:
        row = by_date.get(trading_date)
        if row is None:
            return None
        close = _positive(row.get("close"))
        available_at = row.get("available_at")
        observed_at = row.get("observed_at")
        if close is None or not isinstance(available_at, datetime) or not isinstance(observed_at, datetime):
            return None
        if _utc(available_at) > reference or _utc(observed_at) > reference:
            return None
        selected.append(ConfirmedDailyPrice(
            instrument_id=int(row["instrument_id"]), trading_date=trading_date, close=close,
            source_id=str(row["source_id"]), observed_at=_utc(observed_at), available_at=_utc(available_at),
        ))
    return tuple(selected)


def completed_trading_dates(as_of: datetime, *, count: int = 3) -> tuple[date, ...]:
    """Latest completed US market date and its preceding exact dates."""

    if count <= 0:
        return ()
    local = _utc(as_of).astimezone(MARKET_TZ)
    cursor = local.date()
    # During RTH the current session has not completed.  After the cash close
    # it is eligible only if a confirmed bar is actually available; callers
    # still reject it when the source has not published it yet.
    if not is_us_market_day(cursor) or local < market_session_bounds(cursor)[1]:
        cursor -= timedelta(days=1)
    dates: list[date] = []
    while len(dates) < count:
        if is_us_market_day(cursor):
            dates.append(cursor)
        cursor -= timedelta(days=1)
    return tuple(dates)


def _positive(value: Any) -> float | None:
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and number > 0 else None


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
