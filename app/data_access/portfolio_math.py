"""Pure calendar and paired-return math for portfolio intelligence."""

from __future__ import annotations

from datetime import date, timedelta
from math import isfinite

from investment_panel.core.decision import is_us_market_day


def aligned_pair_returns(
    left_prices: dict[date, float],
    right_prices: dict[date, float],
    *,
    excluded_dates: set[date] | None = None,
) -> tuple[list[date], dict[date, float], dict[date, float]]:
    common_dates = sorted(
        day for day in set(left_prices) & set(right_prices)
        if isfinite(left_prices[day]) and left_prices[day] > 0
        and isfinite(right_prices[day]) and right_prices[day] > 0
    )
    excluded_dates = excluded_dates or set()
    intervals = [
        (previous, current)
        for previous, current in zip(common_dates, common_dates[1:])
        if not any(previous <= split_date <= current for split_date in excluded_dates)
    ]
    interval_dates = [current for _previous, current in intervals]
    left_returns = {current: left_prices[current] / left_prices[previous] - 1 for previous, current in intervals}
    right_returns = {current: right_prices[current] / right_prices[previous] - 1 for previous, current in intervals}
    return interval_dates, left_returns, right_returns


def adjacent_session_dates(previous: str, current: str, *, continuous: bool = False) -> bool:
    if not previous or not current:
        return False
    try:
        previous_date = date.fromisoformat(previous)
        current_date = date.fromisoformat(current)
    except ValueError:
        return False
    if continuous:
        return (current_date - previous_date).days == 1
    if current_date <= previous_date or not is_us_market_day(previous_date) or not is_us_market_day(current_date):
        return False
    sessions = 0
    cursor = previous_date + timedelta(days=1)
    while cursor <= current_date:
        if is_us_market_day(cursor):
            sessions += 1
        cursor += timedelta(days=1)
    return sessions == 1
