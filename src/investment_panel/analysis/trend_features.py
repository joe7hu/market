"""Point-in-time daily trend features and deterministic state classification.

The functions in this module are pure.  Persistence and source selection stay in
the PostgreSQL database layer so replay tests can supply exact as-of bars.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
import math
from statistics import mean, pstdev
from typing import Any, Literal, Sequence

from investment_panel.core.decision import is_us_market_day


TrendState = Literal["trend_up", "trend_down", "range", "transition", "unavailable"]
VolatilityState = Literal["low", "normal", "high", "unstable"]

FEATURE_VERSION = "daily-trend-v1"


@dataclass(frozen=True)
class TrendFeature:
    as_of_date: date | None
    price: float | None
    ma_50: float | None
    ma_200: float | None
    momentum_5d: float | None
    momentum_20d: float | None
    relative_strength_20d: float | None
    relative_strength_60d: float | None
    kaufman_er_20d: float | None
    kaufman_er_60d: float | None
    kama_fast: float | None
    kama_slow: float | None
    kama_fast_slope: float | None
    kama_slow_slope: float | None
    atr_pct: float | None
    realized_vol_20d: float | None
    realized_vol_60d: float | None
    realized_vol_percentile: float | None
    trend_state: TrendState
    trend_confidence: float
    volatility_state: VolatilityState
    data_quality_status: str
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def kaufman_efficiency_ratio(values: Sequence[float], period: int) -> float | None:
    """Return Kaufman's direction / path-length ratio in the range 0..1."""

    if period <= 0 or len(values) <= period:
        return None
    window = [float(value) for value in values[-(period + 1) :]]
    path = sum(abs(current - previous) for previous, current in zip(window, window[1:], strict=False))
    if path == 0:
        return 0.0
    return abs(window[-1] - window[0]) / path


def kaufman_adaptive_moving_average(
    values: Sequence[float],
    *,
    er_period: int,
    fast_period: int,
    slow_period: int,
) -> list[float | None]:
    """Return a KAMA series with an SMA seed at ``er_period``.

    The smoothing constant follows Kaufman: ``(ER * (fast - slow) + slow)^2``.
    """

    clean = [float(value) for value in values]
    output: list[float | None] = [None] * len(clean)
    if er_period <= 0 or fast_period <= 0 or slow_period <= fast_period or len(clean) <= er_period:
        return output
    fast = 2.0 / (fast_period + 1.0)
    slow = 2.0 / (slow_period + 1.0)
    seed = mean(clean[: er_period + 1])
    output[er_period] = seed
    previous = seed
    for index in range(er_period + 1, len(clean)):
        er = kaufman_efficiency_ratio(clean[: index + 1], er_period) or 0.0
        smoothing = (er * (fast - slow) + slow) ** 2
        previous = previous + smoothing * (clean[index] - previous)
        output[index] = previous
    return output


def compute_trend_feature(
    bars: Sequence[dict[str, Any]],
    benchmark_bars: Sequence[dict[str, Any]],
    *,
    as_of_date: date | None = None,
    expected_last_date: date | None = None,
    require_relative_strength: bool = True,
) -> TrendFeature:
    """Compute the latest daily feature using only the supplied point-in-time bars."""

    ordered = _valid_bars(bars)
    reasons = _quality_reasons(ordered)
    if any(not is_us_market_day(row["trading_date"]) for row in ordered):
        reasons.append("non_market_daily_price_bar")
    if expected_last_date is not None and (
        not ordered or ordered[-1]["trading_date"] != expected_last_date
    ):
        reasons.append("terminal_daily_price_bar_missing")
    if len(ordered) != len(bars):
        reasons.append("invalid_or_duplicate_price_bars")
    if as_of_date is not None and ordered and (as_of_date - ordered[-1]["trading_date"]).days > 7:
        reasons.append("underlying_history_stale_relative_to_as_of")
    if len(ordered) < 200:
        reasons.append("insufficient_price_history")
    benchmark = _valid_bars(benchmark_bars)
    benchmark_dates = {row["trading_date"] for row in benchmark}
    required_benchmark_dates = {row["trading_date"] for row in ordered[-61:]}
    if ordered and not required_benchmark_dates.issubset(benchmark_dates):
        reasons.append("benchmark_history_incomplete")
    if reasons:
        return _unavailable(ordered, reasons)

    benchmark_by_date = {row["trading_date"]: float(row["close"]) for row in benchmark}
    closes = [float(row["close"]) for row in ordered]
    fast_series = kaufman_adaptive_moving_average(
        closes, er_period=10, fast_period=2, slow_period=30
    )
    slow_series = kaufman_adaptive_moving_average(
        closes, er_period=20, fast_period=5, slow_period=60
    )
    raw_states: list[TrendState] = []
    latest_metrics: dict[str, Any] = {}
    for end in range(200, len(ordered) + 1):
        window = ordered[:end]
        window_closes = closes[:end]
        metrics = _metrics(
            window,
            window_closes,
            benchmark_by_date,
            fast_series[:end],
            slow_series[:end],
        )
        raw_states.append(_raw_trend_state(metrics, require_relative_strength=require_relative_strength))
        latest_metrics = metrics
    trend_state = _confirmed_state(raw_states)
    trend_confidence = _trend_confidence(
        latest_metrics, trend_state, require_relative_strength=require_relative_strength
    )
    volatility_state = _volatility_state(closes)
    return TrendFeature(
        as_of_date=ordered[-1]["trading_date"],
        price=latest_metrics["price"],
        ma_50=latest_metrics["ma_50"],
        ma_200=latest_metrics["ma_200"],
        momentum_5d=latest_metrics["momentum_5d"],
        momentum_20d=latest_metrics["momentum_20d"],
        relative_strength_20d=latest_metrics["relative_strength_20d"],
        relative_strength_60d=latest_metrics["relative_strength_60d"],
        kaufman_er_20d=latest_metrics["kaufman_er_20d"],
        kaufman_er_60d=latest_metrics["kaufman_er_60d"],
        kama_fast=latest_metrics["kama_fast"],
        kama_slow=latest_metrics["kama_slow"],
        kama_fast_slope=latest_metrics["kama_fast_slope"],
        kama_slow_slope=latest_metrics["kama_slow_slope"],
        atr_pct=latest_metrics["atr_pct"],
        realized_vol_20d=realized_volatility(closes, 20),
        realized_vol_60d=realized_volatility(closes, 60),
        realized_vol_percentile=_realized_vol_percentile(closes),
        trend_state=trend_state,
        trend_confidence=trend_confidence,
        volatility_state=volatility_state,
        data_quality_status="complete",
        reason_codes=(),
    )


def _metrics(
    bars: Sequence[dict[str, Any]],
    closes: Sequence[float],
    benchmark_by_date: dict[date, float],
    fast_series: Sequence[float | None],
    slow_series: Sequence[float | None],
) -> dict[str, Any]:
    price = closes[-1]
    atr_pct = _atr_pct(bars)
    return {
        "price": price,
        "ma_50": mean(closes[-50:]),
        "ma_200": mean(closes[-200:]),
        "momentum_5d": _return(closes, 5),
        "momentum_20d": _return(closes, 20),
        "relative_strength_20d": _relative_strength(bars, closes, benchmark_by_date, 20),
        "relative_strength_60d": _relative_strength(bars, closes, benchmark_by_date, 60),
        "kaufman_er_20d": kaufman_efficiency_ratio(closes, 20),
        "kaufman_er_60d": kaufman_efficiency_ratio(closes, 60),
        "kama_fast": _last(fast_series),
        "kama_slow": _last(slow_series),
        "kama_fast_slope": _series_slope(fast_series, 5),
        "kama_slow_slope": _series_slope(slow_series, 5),
        "atr_pct": atr_pct,
    }


def _raw_trend_state(metrics: dict[str, Any], *, require_relative_strength: bool) -> TrendState:
    required = (
        "price", "ma_50", "ma_200", "momentum_20d", "relative_strength_20d",
        "kaufman_er_20d", "kama_fast_slope", "kama_slow_slope", "atr_pct",
    )
    if any(metrics.get(key) is None for key in required):
        return "unavailable"
    price = float(metrics["price"])
    er20 = float(metrics["kaufman_er_20d"])
    up = (
        price > float(metrics["ma_50"])
        and price > float(metrics["ma_200"])
        and float(metrics["momentum_20d"]) > 0
        and (not require_relative_strength or float(metrics["relative_strength_20d"]) > 0)
        and float(metrics["kama_fast_slope"]) > 0
        and float(metrics["kama_slow_slope"]) > 0
        and er20 >= 0.35
    )
    down = (
        price < float(metrics["ma_50"])
        and price < float(metrics["ma_200"])
        and float(metrics["momentum_20d"]) < 0
        and (not require_relative_strength or float(metrics["relative_strength_20d"]) < 0)
        and float(metrics["kama_fast_slope"]) < 0
        and float(metrics["kama_slow_slope"]) < 0
        and er20 >= 0.35
    )
    atr = price * float(metrics["atr_pct"])
    ma_distance_atr = (
        max(abs(price - float(metrics["ma_50"])), abs(price - float(metrics["ma_200"]))) / atr
        if atr > 0
        else math.inf
    )
    if up:
        return "trend_up"
    if down:
        return "trend_down"
    if er20 <= 0.20 and ma_distance_atr <= 1.0:
        return "range"
    return "transition"


def _confirmed_state(raw_states: Sequence[TrendState]) -> TrendState:
    confirmed: TrendState = "transition"
    pending: TrendState | None = None
    pending_days = 0
    for raw in raw_states:
        if raw == "unavailable":
            confirmed, pending, pending_days = "unavailable", None, 0
            continue
        if raw == confirmed:
            pending, pending_days = None, 0
            continue
        if raw == pending:
            pending_days += 1
        else:
            pending, pending_days = raw, 1
        if pending_days >= 2:
            confirmed, pending, pending_days = raw, None, 0
    return confirmed


def _trend_confidence(
    metrics: dict[str, Any], state: TrendState, *, require_relative_strength: bool
) -> float:
    if state == "unavailable":
        return 0.0
    er = float(metrics.get("kaufman_er_20d") or 0.0)
    if state == "range":
        return round(max(0.0, min(1.0, 1.0 - er / 0.35)), 4)
    direction = 1.0 if state == "trend_up" else -1.0 if state == "trend_down" else 0.0
    if not direction:
        return round(max(0.1, min(0.5, er)), 4)
    signals = [
        direction * float(metrics.get("momentum_20d") or 0.0) > 0,
        direction * float(metrics.get("kama_fast_slope") or 0.0) > 0,
        direction * float(metrics.get("kama_slow_slope") or 0.0) > 0,
    ]
    if require_relative_strength:
        signals.append(direction * float(metrics.get("relative_strength_20d") or 0.0) > 0)
    agreement = sum(signals) / len(signals)
    return round(max(0.0, min(1.0, 0.55 * agreement + 0.45 * er)), 4)


def _volatility_state(closes: Sequence[float]) -> VolatilityState:
    current = realized_volatility(closes, 20)
    slower = realized_volatility(closes, 60)
    percentile = _realized_vol_percentile(closes)
    if current is None or slower is None or percentile is None:
        return "unstable"
    if slower > 0 and (current / slower >= 1.6 or current / slower <= 0.55):
        return "unstable"
    if percentile <= 0.25:
        return "low"
    if percentile >= 0.75:
        return "high"
    return "normal"


def _realized_vol_percentile(closes: Sequence[float]) -> float | None:
    if len(closes) < 41:
        return None
    series = [
        value
        for end in range(max(21, len(closes) - 251), len(closes) + 1)
        if (value := realized_volatility(closes[:end], 20)) is not None
    ]
    if not series:
        return None
    return sum(value <= series[-1] for value in series) / len(series)


def realized_volatility(closes: Sequence[float], period: int) -> float | None:
    """Return annualized close-to-close realized volatility from log returns."""

    if len(closes) <= period:
        return None
    window = closes[-(period + 1) :]
    returns = [math.log(current / previous) for previous, current in zip(window, window[1:], strict=False)]
    return pstdev(returns) * math.sqrt(252) if len(returns) > 1 else None


def _atr_pct(bars: Sequence[dict[str, Any]], period: int = 14) -> float | None:
    if len(bars) <= period:
        return None
    ranges: list[float] = []
    for previous, current in zip(bars[-(period + 1) :], bars[-period:], strict=False):
        high = _number(current.get("high")) or _number(current.get("close"))
        low = _number(current.get("low")) or _number(current.get("close"))
        previous_close = _number(previous.get("close"))
        if high is None or low is None or previous_close is None:
            return None
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    price = float(bars[-1]["close"])
    return mean(ranges) / price if price > 0 else None


def _relative_strength(
    bars: Sequence[dict[str, Any]],
    closes: Sequence[float],
    benchmark_by_date: dict[date, float],
    period: int,
) -> float | None:
    if len(closes) <= period:
        return None
    start_date = bars[-(period + 1)]["trading_date"]
    end_date = bars[-1]["trading_date"]
    benchmark_start = benchmark_by_date.get(start_date)
    benchmark_end = benchmark_by_date.get(end_date)
    if not benchmark_start or not benchmark_end:
        return None
    benchmark_return = benchmark_end / benchmark_start
    return closes[-1] / closes[-(period + 1)] / benchmark_return - 1.0


def _quality_reasons(bars: Sequence[dict[str, Any]]) -> list[str]:
    if not bars:
        return ["missing_canonical_price_history"]
    closes = [float(row["close"]) for row in bars]
    for previous, current in zip(bars, bars[1:], strict=False):
        cursor = previous["trading_date"] + timedelta(days=1)
        while cursor < current["trading_date"]:
            if is_us_market_day(cursor):
                return ["missing_daily_price_bars"]
            cursor += timedelta(days=1)
    if any(abs(current / previous - 1.0) >= 0.45 for previous, current in zip(closes, closes[1:], strict=False)):
        return ["unresolved_corporate_action_in_price_history"]
    return []


def _valid_bars(bars: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    valid: list[dict[str, Any]] = []
    seen: set[date] = set()
    for raw in sorted(bars, key=lambda row: row.get("trading_date") or date.min):
        trading_date = raw.get("trading_date")
        close = _number(raw.get("close"))
        if not isinstance(trading_date, date) or close is None or close <= 0 or trading_date in seen:
            continue
        seen.add(trading_date)
        valid.append({**raw, "close": close})
    return valid


def _unavailable(bars: Sequence[dict[str, Any]], reasons: Sequence[str]) -> TrendFeature:
    return TrendFeature(
        as_of_date=bars[-1]["trading_date"] if bars else None,
        price=float(bars[-1]["close"]) if bars else None,
        ma_50=None, ma_200=None, momentum_5d=None, momentum_20d=None,
        relative_strength_20d=None, relative_strength_60d=None,
        kaufman_er_20d=None, kaufman_er_60d=None,
        kama_fast=None, kama_slow=None, kama_fast_slope=None, kama_slow_slope=None,
        atr_pct=None, realized_vol_20d=None, realized_vol_60d=None,
        realized_vol_percentile=None, trend_state="unavailable", trend_confidence=0.0,
        volatility_state="unstable", data_quality_status="unavailable",
        reason_codes=tuple(sorted(set(reasons))),
    )


def _return(values: Sequence[float], period: int) -> float | None:
    return values[-1] / values[-(period + 1)] - 1.0 if len(values) > period and values[-(period + 1)] else None


def _series_slope(values: Sequence[float | None], period: int) -> float | None:
    clean = [value for value in values if value is not None]
    return clean[-1] / clean[-(period + 1)] - 1.0 if len(clean) > period and clean[-(period + 1)] else None


def _last(values: Sequence[float | None]) -> float | None:
    return next((value for value in reversed(values) if value is not None), None)


def _number(value: Any) -> float | None:
    try:
        result = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return result if result is not None and math.isfinite(result) else None
