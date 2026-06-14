"""Pure value-coercion helpers for the options radar.

The radar reads loosely-typed DuckDB rows (``dict[str, Any]``) and JSON blobs, so
almost every function leans on these to coerce scalars, dates and JSON safely.
They depend on nothing in the package, which is why they live in their own leaf
module.
"""

from __future__ import annotations

import json
from statistics import mean
from typing import Any

from investment_panel.core.coercion import iso_string as _iso
from investment_panel.core.coercion import parse_date as _date
from investment_panel.core.coercion import parse_json as _json_or_list
from investment_panel.core.coercion import parse_json_dict as _json
from investment_panel.core.coercion import parse_naive_datetime as _datetime
from investment_panel.core.coercion import to_float_or_none as _number
from investment_panel.core.coercion import to_int_or_none as _integer


def _days_to_expiration(expiration: Any, snapshot_time: str) -> int | None:
    expiry_date = _date(expiration)
    snapshot_date = _date(snapshot_time)
    if expiry_date is None or snapshot_date is None:
        return None
    return (expiry_date - snapshot_date).days


def _elapsed_days(start: Any, end: Any) -> int | None:
    start_date = _date(start)
    end_date = _date(end)
    if start_date is None or end_date is None:
        return None
    return (end_date - start_date).days


def _elapsed_hours(start: Any, end: Any) -> float | None:
    start_dt = _datetime(start)
    end_dt = _datetime(end)
    if start_dt is None or end_dt is None:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds() / 3600)


def _list_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, tuple):
        return [str(item) for item in value if item]
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return [value] if value else []
        if isinstance(decoded, list):
            return [str(item) for item in decoded if item]
    return []


def _coalesce_number(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def _average(values: list[float]) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return mean(clean)


def _median(values: list[float]) -> float | None:
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return None
    midpoint = len(clean) // 2
    if len(clean) % 2:
        return clean[midpoint]
    return (clean[midpoint - 1] + clean[midpoint]) / 2


def _normalize_symbol(value: Any) -> str:
    return str(value or "").upper().split(":")[-1]
