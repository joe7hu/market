"""Indicator, theme, and candidate scoring helpers for the options radar."""

from __future__ import annotations

from statistics import mean
from typing import Any

from investment_panel.core.options_radar_coerce import _average, _json, _normalize_symbol, _number
from investment_panel.core.options_radar_constants import THEME_WATCH_KEYWORDS


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


def _buy_under(row: dict[str, Any], strategy: dict[str, Any]) -> float | None:
    underlying = _number(row.get("underlying_price"))
    strike = _number(row.get("strike"))
    if underlying is None or strike is None:
        return None
    max_move = float(strategy["max_required_move_pct"])
    option_type = str(row.get("option_type") or "").lower()
    if option_type == "put":
        return max(0.0, (strike - underlying * (1 - max_move)) / 10)
    return max(0.0, (underlying * (1 + max_move) - strike) / 10)


def _candidate_score(row: dict[str, Any], state: str, watch_themes: list[str] | None = None) -> float:
    if state == "REJECT":
        return 0.0
    required_move = _number(row.get("required_move_10x_pct")) or 10
    liquidity = _number(row.get("liquidity_score")) or 0
    convexity = _number(row.get("convexity_score")) or 0
    rs = _number(row.get("rs_vs_qqq_20d")) or 0
    technical = 100.0 if (_number(row.get("price")) or 0) >= (_number(row.get("ma_50")) or 10**9) else 45.0
    score = (max(0.0, 100.0 - required_move * 20.0) * 0.35) + (liquidity * 0.20) + (convexity * 0.30) + (technical * 0.10) + (max(-20.0, min(20.0, rs * 100)) + 20) * 0.05
    score += _theme_watch_score(watch_themes or _theme_watch_matches(row))
    if state == "WATCH":
        score *= 0.70
    if state == "SETUP":
        score *= 0.88
    return round(max(0.0, min(100.0, score)), 2)


def _theme_watch_score(themes: list[str]) -> float:
    if not themes:
        return 0.0
    return min(8.0, 4.0 + max(0, len(themes) - 1) * 2.0)


def _theme_watch_matches(row: dict[str, Any]) -> list[str]:
    text = _theme_context_text(row)
    if not text:
        return []
    matches: list[str] = []
    for theme, keywords in THEME_WATCH_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            matches.append(theme)
    return matches


def _theme_context_text(row: dict[str, Any]) -> str:
    parts = [
        row.get("ticker"),
        row.get("instrument_name"),
        row.get("asset_class"),
        row.get("sector"),
        row.get("industry"),
        row.get("category"),
    ]
    return f" {' '.join(str(part or '').lower() for part in parts)} "


def _has_missing_data(blockers: list[str]) -> bool:
    return any(blocker.startswith("missing_") for blocker in blockers)


def _is_delayed_feed(row: dict[str, Any]) -> bool:
    """Whether a candidate's quotes came from a delayed (non-real-time) feed."""

    raw = _json(row.get("raw"))
    marker = str(
        raw.get("market_data")
        or raw.get("market_data_type")
        or raw.get("data_status")
        or row.get("data_status")
        or ""
    ).lower()
    return "delayed" in marker
