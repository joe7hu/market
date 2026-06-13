"""Symbol/number/date coercion and rate-limit detection helpers."""

from __future__ import annotations
import hashlib
import json
import math
from datetime import date, datetime
from typing import Any

from investment_panel.core.free_sources.constants import _RATE_LIMIT_HINT



def _is_rate_limit_error(exc: Exception) -> bool:
    """Whether an upstream exception looks like an HTTP 429 / rate limit."""

    return bool(_RATE_LIMIT_HINT.search(str(exc)))




def _radar_expiry_targets(min_dte: int, max_dte: int, count: int) -> list[int]:
    if count <= 1:
        return [min_dte]
    step = (max_dte - min_dte) / max(1, count - 1)
    return [round(min_dte + step * index) for index in range(count)]




def _dte_from_expiry(expiry: str, observed_at: str) -> int | None:
    try:
        expiry_date = date.fromisoformat(expiry[:10])
        observed_date = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None
    return (expiry_date - observed_date).days




def _unique_strings(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output




def unique_symbols(symbols: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        normalized = normalize_symbol(symbol)
        if not normalized or normalized in seen or normalized.endswith("-USD"):
            continue
        seen.add(normalized)
        output.append(normalized)
    return output




def infer_event_date(events: dict[str, Any]) -> str | None:
    calendar = events.get("calendar")
    if isinstance(calendar, dict):
        preferred_keys = ["Earnings Date", "earningsDate", "earnings_date"]
        for key in preferred_keys:
            value = calendar.get(key)
            inferred = first_date_value(value)
            if inferred:
                return inferred
        for key, value in calendar.items():
            if "earnings" in str(key).lower() and "date" in str(key).lower():
                inferred = first_date_value(value)
                if inferred:
                    return inferred
    return None




def first_date_value(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            if item:
                return str(item)[:10]
        return None
    if value:
        return str(value)[:10]
    return None




def parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}




def normalize_symbol(value: Any) -> str:
    symbol = str(value or "").upper()
    return symbol.split(":")[-1]




def as_float(value: Any) -> float | None:
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if number is None or not math.isfinite(number):
        return None
    return number




def as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None




def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
