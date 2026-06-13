"""Low-level technical / liquidity / convexity math."""

from __future__ import annotations

import math
from statistics import mean
from typing import Any

from investment_panel.core.options_radar.coerce import (_average, _coalesce_number, _normalize_symbol, _number)

def _zscore(value: float | None, sample: list[float | None]) -> float | None:
    clean = [v for v in sample if v is not None]
    if value is None or len(clean) < 3:
        return None
    avg = sum(clean) / len(clean)
    variance = sum((v - avg) ** 2 for v in clean) / (len(clean) - 1)
    sd = math.sqrt(variance)
    if sd <= 0:
        # Flat baseline: any deviation is, by definition, an extreme; cap at +/-4 sigma.
        if value == avg:
            return 0.0
        return 4.0 if value > avg else -4.0
    return round(max(-4.0, min(4.0, (value - avg) / sd)), 4)


def _percentile_rank(value: float | None, history: list[float]) -> float | None:
    if value is None or not history:
        return None
    return round(sum(1 for item in history if item <= value) / len(history) * 100, 2)


def _iv_rank(value: float | None, history: list[float]) -> float | None:
    if value is None or not history:
        return None
    low = min(history)
    high = max(history)
    if high == low:
        return 50.0
    return round((value - low) / (high - low) * 100, 2)


def _relative_strength(values: list[float | None], benchmark: list[float | None], period: int) -> float | None:
    clean = [value for value in values if value is not None]
    bench = [value for value in benchmark if value is not None]
    if len(clean) <= period or len(bench) <= period:
        return None
    stock_return = clean[-1] / clean[-period - 1] - 1
    benchmark_return = bench[-1] / bench[-period - 1] - 1
    return stock_return - benchmark_return


def _atr_pct(rows: list[dict[str, Any]], period: int = 14) -> float | None:
    if not rows:
        return None
    true_ranges: list[float] = []
    previous_close: float | None = None
    for row in rows[-period:]:
        high = _number(row.get("high"))
        low = _number(row.get("low"))
        close = _number(row.get("close"))
        if high is None or low is None:
            continue
        values = [high - low]
        if previous_close is not None:
            values.extend([abs(high - previous_close), abs(low - previous_close)])
        true_ranges.append(max(values))
        previous_close = close
    close = _number(rows[-1].get("close"))
    if not true_ranges or close is None or close <= 0:
        return None
    return mean(true_ranges) / close


def _realized_vol(closes: list[float], window: int) -> float | None:
    """Annualized close-to-close realized volatility over the trailing ``window`` days.

    Used as the cheap-convexity reference: ``iv_rv_ratio = atm_iv / rv_60d`` flags when
    option IV is cheap relative to how much the stock actually moves, and as the floor
    for the EV engine's scenario width when realized vol exceeds implied.
    """

    clean = [c for c in closes if c is not None and c > 0]
    if len(clean) < window + 1:
        return None
    rets = [math.log(clean[i] / clean[i - 1]) for i in range(len(clean) - window, len(clean))]
    if len(rets) < 2:
        return None
    avg = sum(rets) / len(rets)
    variance = sum((r - avg) ** 2 for r in rets) / (len(rets) - 1)
    return round(math.sqrt(variance) * math.sqrt(252), 6)


def _volume_ratio(volumes: list[float]) -> float | None:
    if len(volumes) < 20:
        return None
    recent = _average(volumes[-20:])
    baseline = _average(volumes[-60:]) if len(volumes) >= 60 else _average(volumes)
    if recent is None or baseline is None or baseline <= 0:
        return None
    return recent / baseline


def _base_length_days(closes: list[float], high_252: float) -> int | None:
    if not closes or high_252 <= 0:
        return None
    floor = high_252 * 0.75
    count = 0
    for close in reversed(closes):
        if close < floor:
            break
        count += 1
    return count


def _liquidity_score(spread_pct: float | None, open_interest: float | None, volume: float | None) -> float | None:
    components: list[float] = []
    weights: list[float] = []
    if spread_pct is not None:
        components.append(max(0.0, min(100.0, 100.0 - spread_pct * 300.0)))
        weights.append(0.60)
    if open_interest is not None:
        components.append(max(0.0, min(100.0, open_interest / 500.0 * 100.0)))
        weights.append(0.25)
    if volume is not None:
        components.append(max(0.0, min(100.0, volume / 100.0 * 100.0)))
        weights.append(0.15)
    if not components:
        return None
    score = sum(component * weight for component, weight in zip(components, weights, strict=False)) / sum(weights)
    if open_interest is None or volume is None:
        score = min(score, 70.0)
    return round(score, 2)


def _convexity_score(required_move_pct: float | None, delta: float | None, dte: int | None) -> float | None:
    if required_move_pct is None:
        return None
    move_score = max(0.0, min(100.0, 100.0 - required_move_pct * 25.0))
    delta_score = 100.0 - min(100.0, abs((abs(delta or 0.30) - 0.30) * 180.0))
    dte_score = 100.0 if dte is None else max(0.0, min(100.0, (dte - 180) / 720 * 100.0))
    return round(move_score * 0.60 + delta_score * 0.25 + dte_score * 0.15, 2)


def _iv_history_by_ticker(rows: list[dict[str, Any]]) -> dict[str, list[float]]:
    history: dict[str, list[float]] = {}
    for row in rows:
        iv = _number(row.get("iv"))
        if iv is None:
            continue
        history.setdefault(_normalize_symbol(row.get("ticker")), []).append(iv)
    return history


def _premium_mid(row: dict[str, Any], raw: dict[str, Any]) -> float | None:
    mid = _number(row.get("mid")) or _coalesce_number(raw, "mid", "mark")
    if mid is not None:
        return mid
    bid = _number(row.get("bid")) or _coalesce_number(raw, "bid")
    ask = _number(row.get("ask")) or _coalesce_number(raw, "ask")
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def _spread_pct(bid: float | None, ask: float | None, mid: float | None) -> float | None:
    if bid is None or ask is None or mid is None or mid <= 0:
        return None
    return max(0.0, (ask - bid) / mid)


def _required_move_pct(option_type: str, underlying: float | None, required_price: float) -> float | None:
    if underlying is None or underlying <= 0:
        return None
    if option_type == "put":
        return max(0.0, (underlying - required_price) / underlying)
    return max(0.0, (required_price - underlying) / underlying)


def _diff(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def _bounded_abs_delta(value: Any) -> float | None:
    delta = _number(value)
    if delta is None:
        return None
    return max(0.0, min(1.0, abs(delta)))


def _relative_diff(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    denominator = max(abs(left), abs(right))
    if denominator <= 0:
        return None
    return abs(left - right) / denominator
