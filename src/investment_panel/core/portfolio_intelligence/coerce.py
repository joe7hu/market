"""Auto-split from portfolio_intelligence.py — see ARCHITECTURE.md."""
from __future__ import annotations

import json
from typing import Any


BROAD_CATEGORIES = {"", "owned-portfolio", "portfolio", "manual", "watchlist", "market"}


def _compact_empty_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}


def _total_value(holdings: list[dict[str, Any]]) -> float:
    return sum(float(row.get("market_value") or 0.0) for row in holdings)


def _weight(value: float, total: float) -> float:
    return (value / total) * 100 if total else 0.0


def _money(value: float) -> str:
    return f"${value:,.0f}"


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_list(value: Any) -> list[dict[str, Any]]:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}
