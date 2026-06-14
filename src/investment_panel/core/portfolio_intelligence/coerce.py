"""Auto-split from portfolio_intelligence.py — see ARCHITECTURE.md."""
from __future__ import annotations

from typing import Any

from investment_panel.core.coercion import parse_json_dict as _json_obj
from investment_panel.core.coercion import parse_json_list as _json_list
from investment_panel.core.coercion import to_float_or_none as _float


BROAD_CATEGORIES = {"", "owned-portfolio", "portfolio", "manual", "watchlist", "market"}


def _compact_empty_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}


def _total_value(holdings: list[dict[str, Any]]) -> float:
    return sum(float(row.get("market_value") or 0.0) for row in holdings)


def _weight(value: float, total: float) -> float:
    return (value / total) * 100 if total else 0.0


def _money(value: float) -> str:
    return f"${value:,.0f}"


