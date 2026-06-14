"""Symbol/number/date coercion and rate-limit detection helpers."""

from __future__ import annotations
from datetime import date, datetime
from typing import Any

from investment_panel.core.coercion import parse_json_dict_copy as parse_json_object
from investment_panel.core.coercion import stable_id
from investment_panel.core.coercion import to_finite_float as as_float
from investment_panel.core.coercion import to_int_or_none as as_int
from investment_panel.core.coercion import unique_strings as _unique_strings
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




def normalize_symbol(value: Any) -> str:
    symbol = str(value or "").upper()
    return symbol.split(":")[-1]




