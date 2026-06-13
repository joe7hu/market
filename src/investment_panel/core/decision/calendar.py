"""US market calendar, sessions, and freshness classification."""

from __future__ import annotations
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from investment_panel.core.source_status import normalize_source_status

from investment_panel.core.decision.constants import ARCO_STALE_DAYS, DAILY_STALE_DAYS, FILING_STALE_DAYS, INTRADAY_STALE_HOURS, MARKET_CLOSE, MARKET_OPEN, MARKET_TZ



def classify_freshness(source_type: str, observed: datetime | None, status: str, docs_only: bool, now: datetime | None = None) -> str:
    normalized_status = normalize_source_status(status)
    if docs_only or source_type == "documentation":
        return "documentation"
    if normalized_status in {"disabled", "documentation"}:
        return "not_applicable"
    if normalized_status == "unknown":
        return "unknown"
    if normalized_status == "failed":
        return "failed"
    if normalized_status == "degraded":
        return "stale"
    if observed is None:
        return "unknown"
    checked_at = normalized_utc(now or datetime.now(UTC))
    age = checked_at - observed
    if source_type in {"intraday_quote", "options", "news"}:
        market_age = market_session_elapsed(observed, checked_at)
        return "fresh" if market_age <= timedelta(hours=INTRADAY_STALE_HOURS) else "stale"
    if source_type == "crypto_quote":
        return "fresh" if age <= timedelta(hours=36) else "stale"
    if source_type == "closing_quote":
        if is_market_open(checked_at):
            return "stale"
        return "fresh" if trading_day_lag(observed.date(), checked_at) <= DAILY_STALE_DAYS else "stale"
    if source_type in {"daily"}:
        return "fresh" if trading_day_lag(observed.date(), checked_at) <= DAILY_STALE_DAYS else "stale"
    if source_type == "arco_thesis":
        return "fresh" if age <= timedelta(days=ARCO_STALE_DAYS) else "stale"
    if source_type in {"filing", "fundamental"}:
        return "fresh" if age <= timedelta(days=FILING_STALE_DAYS) else "stale"
    if source_type in {"provider_run", "provider_health"}:
        return "fresh" if age <= timedelta(days=1) else "stale"
    return "fresh"




def normalized_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)




def market_session_elapsed(start: datetime, end: datetime) -> timedelta:
    """Elapsed regular US equity market time between two timestamps."""

    start_utc = normalized_utc(start)
    end_utc = normalized_utc(end)
    if end_utc <= start_utc:
        return timedelta()

    start_local = start_utc.astimezone(MARKET_TZ)
    end_local = end_utc.astimezone(MARKET_TZ)
    current = start_local.date()
    total = timedelta()
    while current <= end_local.date():
        if is_us_market_day(current):
            open_at, close_at = market_session_bounds(current)
            window_start = max(start_local, open_at)
            window_end = min(end_local, close_at)
            if window_end > window_start:
                total += window_end - window_start
        current += timedelta(days=1)
    return total




def trading_day_lag(observed_date: date, now: datetime) -> int:
    latest_expected = latest_completed_market_day(now)
    if observed_date >= latest_expected:
        return 0
    lag = 0
    current = observed_date + timedelta(days=1)
    while current <= latest_expected:
        if is_us_market_day(current):
            lag += 1
        current += timedelta(days=1)
    return lag




def latest_completed_market_day(now: datetime) -> date:
    local_now = normalized_utc(now).astimezone(MARKET_TZ)
    current = local_now.date()
    if is_us_market_day(current) and local_now.time() >= MARKET_CLOSE:
        return current
    current -= timedelta(days=1)
    while not is_us_market_day(current):
        current -= timedelta(days=1)
    return current




def is_market_open(now: datetime) -> bool:
    local_now = normalized_utc(now).astimezone(MARKET_TZ)
    return is_us_market_day(local_now.date()) and MARKET_OPEN <= local_now.time() < MARKET_CLOSE




def market_session_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, MARKET_OPEN, tzinfo=MARKET_TZ),
        datetime.combine(day, MARKET_CLOSE, tzinfo=MARKET_TZ),
    )




def is_us_market_day(day: date) -> bool:
    return day.weekday() < 5 and day not in us_market_holidays(day.year)




@lru_cache(maxsize=None)
def us_market_holidays(year: int) -> frozenset[date]:
    return frozenset(
        day
        for day in {
            observed_fixed_holiday(year, 1, 1),
            nth_weekday(year, 1, 0, 3),
            nth_weekday(year, 2, 0, 3),
            easter_date(year) - timedelta(days=2),
            last_weekday(year, 5, 0),
            observed_fixed_holiday(year, 6, 19),
            observed_fixed_holiday(year, 7, 4),
            nth_weekday(year, 9, 0, 1),
            nth_weekday(year, 11, 3, 4),
            observed_fixed_holiday(year, 12, 25),
        }
        if day.year == year
    )




def observed_fixed_holiday(year: int, month: int, day: int) -> date:
    holiday = date(year, month, day)
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday




def nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> date:
    current = date(year, month, 1)
    offset = (weekday - current.weekday()) % 7
    return current + timedelta(days=offset + (ordinal - 1) * 7)




def last_weekday(year: int, month: int, weekday: int) -> date:
    current = date(year + int(month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current




def easter_date(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    weekday_offset = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * weekday_offset) // 451
    month = (h + weekday_offset - 7 * m + 114) // 31
    day = ((h + weekday_offset - 7 * m + 114) % 31) + 1
    return date(year, month, day)
