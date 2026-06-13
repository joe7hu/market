"""Auto-split from core/disclosures.py — see ARCHITECTURE.md."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any


def days_since(value: str) -> int:
    return max(1, (date.today() - datetime.strptime(value[:10], "%Y-%m-%d").date()).days)


def disclosure_amount_range(row: dict[str, Any]) -> tuple[float | None, float | None]:
    low = _float_or_none(row.get("amount_min"))
    high = _float_or_none(row.get("amount_max"))
    if low is not None or high is not None:
        return low, high
    raw = str(row.get("amount") or row.get("amount_range") or "")
    cleaned = raw.replace("$", "").replace(",", "").replace("(", "").replace(")", "").replace("Over", "")
    numbers = [_float_or_none(part) for part in cleaned.split("-")]
    numbers = [number for number in numbers if number is not None]
    if not numbers:
        return None, None
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    return numbers[0], numbers[1]


def amount_midpoint(low: float | None, high: float | None) -> float | None:
    if low is None and high is None:
        return None
    if low is None:
        return high
    if high is None:
        return low
    return (low + high) / 2


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None
