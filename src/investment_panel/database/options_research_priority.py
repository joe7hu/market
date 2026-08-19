"""Symmetric daily research priority without changing the expectancy champion."""

from __future__ import annotations

from typing import Any


def research_priority(row: dict[str, Any]) -> dict[str, Any]:
    momentum_20d = _number(row.get("momentum_20d"))
    momentum_5d = _number(row.get("momentum_5d"))
    relative_20d = _number(row.get("relative_strength_20d"))
    relative_60d = _number(row.get("relative_strength_60d"))
    efficiency = _number(row.get("kaufman_er_20d"))
    if momentum_20d is None:
        return {
            "direction_pool": "unavailable",
            "research_priority_score": None,
            "why_ticker": "Daily momentum is unavailable",
        }
    if momentum_20d == 0:
        return {
            "direction_pool": "neutral",
            "research_priority_score": 0.0,
            "why_ticker": "20-day momentum is neutral",
        }
    direction = 1.0 if momentum_20d > 0 else -1.0
    acceleration = direction * ((momentum_5d or 0.0) - momentum_20d / 4.0)
    score = (
        acceleration
        + direction * (relative_20d or 0.0)
        + 0.5 * direction * (relative_60d or 0.0)
        + 0.25 * (efficiency or 0.0)
    )
    pool = "bullish" if direction > 0 else "bearish"
    return {
        "direction_pool": pool,
        "research_priority_score": round(score, 8),
        "why_ticker": (
            f"{pool.title()} pool: 20-day momentum, 5-day acceleration, "
            "relative strength, and trend efficiency"
        ),
    }


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None
