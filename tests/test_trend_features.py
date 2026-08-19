from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import math
from types import SimpleNamespace

import pytest

from investment_panel.core.decision import is_us_market_day
from investment_panel.analysis.trend_features import (
    compute_trend_feature,
    kaufman_adaptive_moving_average,
    kaufman_efficiency_ratio,
)
from investment_panel.database.symbol_trends import market_regime_from_features


def _bars(values: list[float]) -> list[dict[str, object]]:
    start = date(2025, 1, 1)
    dates = []
    cursor = start
    while len(dates) < len(values):
        if is_us_market_day(cursor):
            dates.append(cursor)
        cursor += timedelta(days=1)
    return [
        {
            "trading_date": dates[index],
            "open": value,
            "high": value * 1.002,
            "low": value * 0.998,
            "close": value,
        }
        for index, value in enumerate(values)
    ]


def test_incremental_quote_cutoffs_use_common_completed_daily_bars_for_breadth() -> None:
    market_date = date(2026, 8, 18)
    qqq = SimpleNamespace(
        as_of_date=market_date, trend_state="trend_up", trend_confidence=0.8,
        kaufman_er_20d=0.5, volatility_state="normal",
    )
    current = SimpleNamespace(as_of_date=market_date, trend_state="trend_up")
    stale = SimpleNamespace(as_of_date=date(2026, 8, 17), trend_state="trend_down")
    unavailable = SimpleNamespace(as_of_date=market_date, trend_state="unavailable")
    features = [
        ({"symbol": "QQQ", "symbol_as_of": datetime(2026, 8, 19, 15, tzinfo=UTC)}, qqq, []),
        ({"symbol": "NVDA", "symbol_as_of": datetime(2026, 8, 19, 14, tzinfo=UTC)}, current, []),
        ({"symbol": "AAPL", "symbol_as_of": datetime(2026, 8, 18, 20, tzinfo=UTC)}, stale, []),
        ({"symbol": "MSFT", "symbol_as_of": datetime(2026, 8, 19, 14, tzinfo=UTC)}, unavailable, []),
    ]

    regime = market_regime_from_features(
        features, as_of=datetime(2026, 8, 19, 15, tzinfo=UTC),
        universe_size=5, universe_budget=4,
    )

    assert regime["quality_status"] == "complete"
    assert regime["breadth_denominator"] == 1
    assert regime["breadth_excluded_stale"] == 1
    assert regime["breadth_excluded_unavailable"] == 1
    assert regime["breadth_excluded_truncated"] == 1
    assert regime["breadth_coverage"] == pytest.approx(0.25)
    assert regime["trend_confidence"] == pytest.approx(0.2)
    assert regime["universe_truncated"] is True


def test_efficiency_ratio_zero_denominator_is_zero() -> None:
    assert kaufman_efficiency_ratio([10.0] * 21, 20) == 0.0


def test_efficiency_ratio_one_way_trend_is_one() -> None:
    assert kaufman_efficiency_ratio([float(index) for index in range(1, 22)], 20) == 1.0


def test_efficiency_ratio_zigzag_is_low() -> None:
    values = [100.0 + (1.0 if index % 2 else 0.0) for index in range(21)]
    assert kaufman_efficiency_ratio(values, 20) == 0.0


def test_kama_uses_sma_seed_and_then_moves_toward_price() -> None:
    values = [float(index) for index in range(1, 15)]
    series = kaufman_adaptive_moving_average(values, er_period=10, fast_period=2, slow_period=30)
    assert series[:10] == [None] * 10
    assert series[10] == pytest.approx(sum(values[:11]) / 11)
    assert series[11] is not None and series[11] > series[10]
    assert series[11] < values[11]


def test_monotonic_outperformance_classifies_trend_up() -> None:
    benchmark = [100.0 * math.exp(index * 0.001) for index in range(260)]
    symbol = [100.0 * math.exp(index * 0.003) for index in range(260)]
    feature = compute_trend_feature(_bars(symbol), _bars(benchmark))
    assert feature.trend_state == "trend_up"
    assert feature.kaufman_er_20d == pytest.approx(1.0)
    assert feature.momentum_20d is not None and feature.momentum_20d > 0
    assert feature.relative_strength_20d is not None and feature.relative_strength_20d > 0


def test_market_proxy_can_classify_direction_without_self_relative_strength() -> None:
    qqq = [100.0 * math.exp(index * 0.003) for index in range(260)]
    feature = compute_trend_feature(
        _bars(qqq), _bars(qqq), require_relative_strength=False
    )
    assert feature.relative_strength_20d == pytest.approx(0.0)
    assert feature.trend_state == "trend_up"


def test_monotonic_underperformance_classifies_trend_down() -> None:
    benchmark = [100.0 * math.exp(index * 0.001) for index in range(260)]
    symbol = [200.0 * math.exp(index * -0.003) for index in range(260)]
    feature = compute_trend_feature(_bars(symbol), _bars(benchmark))
    assert feature.trend_state == "trend_down"
    assert feature.momentum_20d is not None and feature.momentum_20d < 0
    assert feature.relative_strength_20d is not None and feature.relative_strength_20d < 0


def test_state_change_requires_two_valid_days() -> None:
    benchmark = [100.0] * 260
    base = [100.0 * math.exp(index * 0.002) for index in range(258)]
    one_day_reversal = [*base, base[-1] * 0.80]
    two_day_reversal = [*base, base[-1] * 0.80, base[-1] * 0.70]
    first = compute_trend_feature(_bars(one_day_reversal), _bars(benchmark[: len(one_day_reversal)]))
    second = compute_trend_feature(_bars(two_day_reversal), _bars(benchmark))
    assert first.trend_state != "trend_down"
    assert second.trend_state in {"transition", "trend_down"}


def test_split_like_price_jump_fails_closed() -> None:
    values = [100.0 + index * 0.1 for index in range(220)]
    values[210] = values[209] * 0.4
    feature = compute_trend_feature(_bars(values), _bars([100.0] * len(values)))
    assert feature.trend_state == "unavailable"
    assert feature.data_quality_status == "unavailable"
    assert "unresolved_corporate_action_in_price_history" in feature.reason_codes


def test_missing_history_fails_closed() -> None:
    feature = compute_trend_feature(_bars([100.0] * 50), _bars([100.0] * 50))
    assert feature.trend_state == "unavailable"
    assert feature.trend_confidence == 0.0
    assert "insufficient_price_history" in feature.reason_codes


def test_missing_daily_bar_gap_fails_closed() -> None:
    bars = _bars([100.0 + index for index in range(220)])
    for row in bars[150:]:
        row["trading_date"] = row["trading_date"] + timedelta(days=10)
    feature = compute_trend_feature(bars, _bars([100.0] * 220))
    assert feature.trend_state == "unavailable"
    assert "missing_daily_price_bars" in feature.reason_codes


def test_one_missing_weekday_fails_closed() -> None:
    bars = _bars([100.0 + index for index in range(220)])
    bars = [row for index, row in enumerate(bars) if index != 149]
    feature = compute_trend_feature(bars, _bars([100.0] * 220))
    assert feature.trend_state == "unavailable"
    assert "missing_daily_price_bars" in feature.reason_codes


def test_missing_benchmark_dates_fail_closed() -> None:
    feature = compute_trend_feature(_bars([100.0 + index for index in range(220)]), _bars([100.0] * 200))
    assert feature.trend_state == "unavailable"
    assert "benchmark_history_incomplete" in feature.reason_codes
