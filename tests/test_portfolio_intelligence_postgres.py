from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from investment_panel.database.portfolio_intelligence import _performance_rows, portfolio_risk_rows
from investment_panel.database.portfolio_math import adjacent_session_dates, aligned_pair_returns


def test_aligned_pair_returns_use_identical_price_intervals() -> None:
    first = date(2026, 7, 1)
    middle = date(2026, 7, 2)
    last = date(2026, 7, 3)

    dates, left, right = aligned_pair_returns(
        {first: 100, middle: 110, last: 121},
        {first: 200, last: 242},
    )

    assert dates == [last]
    assert left[last] == pytest.approx(0.21)
    assert right[last] == pytest.approx(0.21)


def test_aligned_pair_returns_ignore_nonpositive_prices() -> None:
    first = date(2026, 7, 1)
    last = date(2026, 7, 2)
    dates, left, right = aligned_pair_returns({first: 0, last: 100}, {first: 50, last: 55})
    assert dates == []
    assert left == {}
    assert right == {}


def test_aligned_pair_returns_excludes_intervals_touching_a_split() -> None:
    first = date(2026, 7, 1)
    split_day = date(2026, 7, 2)
    last = date(2026, 7, 3)

    dates, left, right = aligned_pair_returns(
        {first: 100, split_day: 50, last: 55},
        {first: 100, split_day: 101, last: 102},
        excluded_dates={split_day},
    )

    assert dates == []
    assert left == right == {}


def test_performance_all_history_is_not_silently_truncated() -> None:
    start = date(2024, 1, 1)
    transactions = [{
        "instrument_id": 1,
        "symbol": "NVDA",
        "transaction_type": "opening_balance",
        "quantity": 1,
        "price": 100,
        "amount": 100,
        "fees": 0,
        "executed_at": datetime(2024, 1, 1, 15, tzinfo=UTC),
    }]
    bars = [
        {"instrument_id": 1, "symbol": "NVDA", "trading_date": start + timedelta(days=index), "close": 100 + index}
        for index in range(800)
    ]
    assert len(_performance_rows(transactions, bars, [])) == 800


def test_performance_buckets_executions_on_new_york_market_date() -> None:
    transactions = [{
        "instrument_id": 1,
        "symbol": "NVDA",
        "transaction_type": "buy",
        "quantity": 1,
        "price": 100,
        "amount": 100,
        "fees": 0,
        "executed_at": datetime(2026, 7, 2, 1, 0, tzinfo=UTC),
    }]
    bars = [{"instrument_id": 1, "symbol": "NVDA", "trading_date": date(2026, 7, 1), "close": 100}]
    assert [row["date"] for row in _performance_rows(transactions, bars, [])] == ["2026-07-01"]


def test_performance_refreshes_cost_fallback_after_each_unpriced_acquisition() -> None:
    transactions = [
        {
            "instrument_id": 1,
            "symbol": "NVDA",
            "transaction_type": "buy",
            "quantity": 1,
            "price": 100,
            "amount": 100,
            "fees": 0,
            "executed_at": datetime(2026, 7, 1, 15, tzinfo=UTC),
        },
        {
            "instrument_id": 1,
            "symbol": "NVDA",
            "transaction_type": "buy",
            "quantity": 1,
            "price": 200,
            "amount": 200,
            "fees": 0,
            "executed_at": datetime(2026, 7, 2, 15, tzinfo=UTC),
        },
    ]

    last = _performance_rows(transactions, [], [])[-1]

    assert last["portfolio_value"] == 300
    assert last["total_pnl"] == 0


def test_performance_uses_latest_known_price_before_first_acquisition() -> None:
    transactions = [{
        "instrument_id": 1,
        "symbol": "NVDA",
        "transaction_type": "buy",
        "quantity": 10,
        "price": 100,
        "amount": 1000,
        "fees": 0,
        "executed_at": datetime(2026, 7, 2, 15, tzinfo=UTC),
    }]
    bars = [{
        "instrument_id": 1,
        "symbol": "NVDA",
        "trading_date": date(2026, 7, 1),
        "close": 90,
        "observed_at": datetime(2026, 7, 1, 20, tzinfo=UTC),
    }]

    last = _performance_rows(transactions, bars, [])[-1]

    assert last["portfolio_value"] == 900
    assert last["total_pnl"] == -100


def test_performance_adjusts_pre_split_same_day_close_for_after_close_split() -> None:
    transactions = [
        {
            "instrument_id": 1,
            "symbol": "NVDA",
            "transaction_type": "buy",
            "quantity": 10,
            "price": 100,
            "amount": 1000,
            "fees": 0,
            "executed_at": datetime(2026, 6, 30, 15, tzinfo=UTC),
        },
        {
            "instrument_id": 1,
            "symbol": "NVDA",
            "transaction_type": "split",
            "quantity": 2,
            "price": 0,
            "amount": 0,
            "fees": 0,
            "executed_at": datetime(2026, 7, 1, 22, tzinfo=UTC),
        },
    ]
    bars = [{
        "instrument_id": 1,
        "symbol": "NVDA",
        "trading_date": date(2026, 7, 1),
        "close": 120,
        "observed_at": datetime(2026, 7, 1, 20, tzinfo=UTC),
    }]

    last = _performance_rows(transactions, bars, [])[-1]

    assert last["portfolio_value"] == 1000
    assert last["total_pnl"] == 0


def test_adjacent_session_dates_respect_missing_weekdays_weekends_and_holidays() -> None:
    assert adjacent_session_dates("2026-07-13", "2026-07-16") is False
    assert adjacent_session_dates("2026-07-10", "2026-07-13") is True
    assert adjacent_session_dates("2026-07-02", "2026-07-06") is True
    assert adjacent_session_dates("2026-07-11", "2026-07-12", continuous=True) is True


def test_recovered_historical_drawdown_is_not_an_active_warning() -> None:
    cards = portfolio_risk_rows(
        {},
        positions=[],
        summary={"holdings_count": 0},
        correlations=[],
        performance=[{"drawdown_pct": -18}, {"drawdown_pct": 0}],
    )
    assert all(card["risk_type"] != "drawdown" for card in cards)
