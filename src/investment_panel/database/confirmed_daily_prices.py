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
    fact_id: int | None = None
    fact_table: str = "raw.price_bar"
    ingest_run_id: str | None = None


def confirmed_daily_bars(
    connection: Any,
    instrument_ids: Iterable[int],
    *,
    as_of: datetime,
    max_bars: int | None = None,
    include_versions: bool = False,
    max_fact_versions: int | None = None,
    trading_dates: Iterable[date] | None = None,
    require_session_close: bool = False,
) -> dict[int, list[dict[str, Any]]]:
    """Confirmed daily bars, optionally retaining point-in-time fact versions."""

    ids = sorted({int(item) for item in instrument_ids})
    if not ids:
        return {}
    reference = _utc(as_of)
    dates = sorted(set(trading_dates)) if trading_dates is not None else None
    if require_session_close and dates is None:
        raise ValueError("session-close confirmation requires exact trading dates")
    confirmation_dates = dates if require_session_close else []
    session_closes = [market_session_bounds(day)[1] for day in confirmation_dates]
    rows = connection.execute(
        """
        WITH session_closes AS (
            SELECT * FROM unnest(%s::date[], %s::timestamptz[]) AS session(trading_date, not_before)
        ), facts AS (
            SELECT fact.*, 'raw.price_bar'::text AS fact_table
            FROM raw.price_bar fact
            UNION ALL
            SELECT fact.*, 'raw.price_bar_history'::text AS fact_table
            FROM raw.price_bar_history fact
        ), confirmed AS (
            SELECT fact.*, confirmation_run.finished_at AS confirmed_at
            FROM facts fact
            JOIN ingest.source source
              ON source.id = fact.source_id
             AND source.enabled
             AND source.operational_state = 'active'
            LEFT JOIN session_closes ON session_closes.trading_date = fact.trading_date
            JOIN LATERAL (
                SELECT price_run.finished_at
                FROM raw.price_bar_fact_availability availability
                JOIN ingest.run price_run ON price_run.id = availability.ingest_run_id
                WHERE availability.fact_id = fact.id
                  AND availability.fact_available_at = fact.available_at
                  AND price_run.status IN ('succeeded', 'partial')
                  AND price_run.finished_at IS NOT NULL
                  AND price_run.finished_at <= %s
                  AND price_run.finished_at >= COALESCE(session_closes.not_before, '-infinity'::timestamptz)
                ORDER BY price_run.finished_at, price_run.id
                LIMIT 1
            ) confirmation_run ON true
            WHERE fact.instrument_id = ANY(%s) AND fact.interval = '1d' AND fact.close > 0
              AND fact.observed_at <= %s AND fact.available_at <= %s
              AND (%s::date[] IS NULL OR fact.trading_date = ANY(%s::date[]))
        ), versioned AS (
            SELECT fact.*,
                   row_number() OVER (
                     PARTITION BY fact.instrument_id, fact.trading_date
                     ORDER BY CASE fact.source_id
                       WHEN 'polygon' THEN 1
                       WHEN 'yahoo_chart' THEN 2
                       WHEN 'yfinance' THEN 3
                       ELSE 10
                     END, fact.available_at DESC, fact.confirmed_at, fact.source_id
                   ) AS canonical_rank
            FROM confirmed fact
        ), selected AS (
            SELECT * FROM versioned WHERE %s OR canonical_rank = 1
        ), ranked AS (
            SELECT selected.*,
                   dense_rank() OVER (PARTITION BY instrument_id ORDER BY trading_date DESC) AS recency_rank
            FROM selected
        ), bounded AS (
            SELECT ranked.*,
                   row_number() OVER (
                     PARTITION BY instrument_id
                     ORDER BY trading_date DESC, available_at DESC, confirmed_at, source_id
                   ) AS fact_rank
            FROM ranked
            WHERE %s::integer IS NULL OR recency_rank <= %s
        )
        SELECT id AS fact_id, fact_table, ingest_run_id,
               instrument_id, trading_date, open, high, low, close, volume, currency,
               source_id, observed_at, available_at, confirmed_at,
               confirmed_at AS run_finished_at, fact_rank
        FROM bounded
        WHERE %s::integer IS NULL OR fact_rank <= %s::integer + 1
        ORDER BY instrument_id, trading_date, available_at, confirmed_at, source_id
        """,
        [
            confirmation_dates, session_closes, reference, ids, reference, reference, dates, dates, include_versions,
            max_bars, max_bars, max_fact_versions, max_fact_versions,
        ],
    ).fetchall()
    output: dict[int, list[dict[str, Any]]] = {}
    overflowed: set[int] = set()
    for raw in rows:
        row = dict(raw)
        instrument_id = int(row["instrument_id"])
        if max_fact_versions is not None and int(row["fact_rank"]) > max_fact_versions:
            overflowed.add(instrument_id)
            continue
        row.pop("fact_rank", None)
        output.setdefault(instrument_id, []).append(row)
    for instrument_id in overflowed:
        output[instrument_id] = []
    return output


def forward_trading_dates(as_of: datetime, *, count: int) -> tuple[date, ...]:
    """The exact next US market sessions after a decision's local date."""

    cursor = _utc(as_of).astimezone(MARKET_TZ).date()
    dates: list[date] = []
    while len(dates) < count:
        cursor += timedelta(days=1)
        if is_us_market_day(cursor):
            dates.append(cursor)
    return tuple(dates)


def confirmed_forward_bars(
    connection: Any, instrument_id: int, *, as_of: datetime, cutoff: datetime, sessions: int,
) -> list[dict[str, Any]]:
    """Canonical completed bars within an exact forward session window."""

    dates = forward_trading_dates(as_of, count=sessions)
    rows = confirmed_daily_bars(
        connection, [instrument_id], as_of=cutoff, trading_dates=dates, require_session_close=True,
    ).get(int(instrument_id), [])
    output = []
    for row in rows:
        observed = _utc(row["observed_at"])
        available = max(_utc(row["available_at"]), _utc(row["confirmed_at"]))
        close_at = market_session_bounds(row["trading_date"])[1]
        measured = max(observed, close_at)
        if available < measured:
            continue
        output.append({**row, "bar_observed_at": observed, "observed_at": measured, "available_at": available})
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
            fact_id=int(row["fact_id"]) if row.get("fact_id") is not None else None,
            fact_table=str(row.get("fact_table") or "raw.price_bar"),
            ingest_run_id=str(row["ingest_run_id"]) if row.get("ingest_run_id") is not None else None,
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
