"""Freshness contract for broad-market display inputs."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from investment_panel.core.decision import (
    is_market_open,
    is_us_market_day,
    latest_completed_market_day,
    MARKET_CLOSE,
    MARKET_OPEN,
    MARKET_TZ,
    normalized_utc,
)


def market_freshness(tables: dict[str, list[dict[str, Any]]], now: datetime | None = None) -> dict[str, Any]:
    checked_at = normalized_utc(now or datetime.now(UTC))
    local_now = checked_at.astimezone(MARKET_TZ)
    expected_date = latest_completed_market_day(checked_at)
    phase = market_phase(checked_at)
    reference_rows = tables.get("market_valuation_reference_charts") or []
    reference_date = max_row_date(reference_rows, ("latest_date", "as_of", "date"))
    asset_date = max_row_date(tables.get("market_environment_assets") or [], ("as_of", "date"))
    valuation_series = {
        str(row.get("metric") or row.get("label") or "unknown"): freshness_check(
            date_value(row.get("latest_date") or row.get("as_of") or row.get("date")),
            expected_date,
            phase,
        )
        for row in reference_rows
    }
    checks = {
        "valuation_reference": freshness_check(reference_date, expected_date, phase) | {"series": valuation_series},
        "asset_matrix": freshness_check(asset_date, expected_date, phase),
    }
    stale = {name: check for name, check in checks.items() if check["status"] == "stale"}
    stale_series = {name: check for name, check in valuation_series.items() if check["status"] == "stale"}
    unknown = {name: check for name, check in checks.items() if check["status"] == "unknown"}
    off_market = {name: check for name, check in checks.items() if check["status"] == "off_market_hours"}
    off_market_series = {name: check for name, check in valuation_series.items() if check["status"] == "off_market_hours"}
    if stale_series:
        status = "stale"
        reason = "; ".join(f"{name} latest {check['latest_date']} missed expected market date {check['expected_date']}" for name, check in stale_series.items())
    elif stale:
        status = "stale"
        reason = "; ".join(f"{name} latest {check['latest_date']} missed expected market date {check['expected_date']}" for name, check in stale.items())
    elif unknown:
        status = "unknown"
        reason = "Market freshness dates are not loaded."
    elif off_market_series or off_market:
        status = "off_market_hours"
        reason = f"Broad-market inputs are current for the last completed market session ({expected_date.isoformat()}); market phase is {phase}."
    else:
        status = "fresh"
        reason = f"Broad-market inputs are current for the expected market date ({expected_date.isoformat()})."
    return {
        "status": status,
        "as_of": local_now.date().isoformat(),
        "checked_at": checked_at.isoformat(),
        "market_phase": phase,
        "expected_date": expected_date.isoformat(),
        "reason": reason,
        "checks": checks,
    }


def freshness_check(value: date | None, expected_date: date, market_phase_value: str) -> dict[str, Any]:
    if value is None:
        return {"status": "unknown", "latest_date": None, "expected_date": expected_date.isoformat(), "trading_day_lag": None}
    lag = market_day_lag(value, expected_date)
    status = "stale" if lag > 0 else ("off_market_hours" if market_phase_value in {"premarket", "postmarket", "weekend_or_holiday"} else "fresh")
    return {
        "status": status,
        "latest_date": value.isoformat(),
        "expected_date": expected_date.isoformat(),
        "trading_day_lag": lag,
        "market_phase": market_phase_value,
    }


def market_day_lag(value: date, expected_date: date) -> int:
    if value >= expected_date:
        return 0
    cursor = value + timedelta(days=1)
    lag = 0
    while cursor <= expected_date:
        if is_us_market_day(cursor):
            lag += 1
        cursor += timedelta(days=1)
    return lag


def market_phase(now: datetime) -> str:
    local_now = normalized_utc(now).astimezone(MARKET_TZ)
    if is_market_open(now):
        return "regular_session"
    if not is_us_market_day(local_now.date()):
        return "weekend_or_holiday"
    if local_now.time() < MARKET_OPEN:
        return "premarket"
    if local_now.time() >= MARKET_CLOSE:
        return "postmarket"
    return "weekend_or_holiday"


def max_row_date(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> date | None:
    dates = [date_value(row.get(key)) for row in rows for key in keys]
    valid_dates = [value for value in dates if value is not None]
    return max(valid_dates) if valid_dates else None


def date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None
