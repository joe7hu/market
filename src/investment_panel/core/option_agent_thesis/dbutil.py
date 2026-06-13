"""Auto-split from raw_sources.py — see ARCHITECTURE.md."""
from __future__ import annotations

import json
from typing import Any
from investment_panel.core.db import json_dumps, query_rows


def first_row(con: Any, sql: str, params: list[Any], json_fields: tuple[str, ...] = ()) -> dict[str, Any] | None:
    rows = query_decoded(con, sql, params, json_fields)
    return rows[0] if rows else None


def query_decoded(con: Any, sql: str, params: list[Any], json_fields: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return [decode_json_fields(row, json_fields) for row in query_rows(con, sql, params)]


def decode_json_fields(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    decoded = dict(row)
    for field in fields:
        if field in decoded:
            decoded[field] = _json_or_value(decoded[field])
    return decoded


def _json_or_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _json(value: Any) -> dict[str, Any]:
    decoded = _json_or_value(value)
    return decoded if isinstance(decoded, dict) else {}
