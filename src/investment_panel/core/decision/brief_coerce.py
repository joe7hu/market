"""Row, JSON, numeric, and text coercion helpers for decision-brief synthesis.

These mirror the generic coercion helpers historically used by the API-normalize
layer (``app/data_access/coerce.py``); they live here so the brief synthesis is
self-contained inside the decision engine. Behavior is preserved exactly.
"""

from __future__ import annotations
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
from typing import Any


def _first_row(tables: dict[str, list[dict[str, Any]]], *keys: str) -> dict[str, Any]:
    for key in keys:
        rows = tables.get(key) or []
        if rows:
            return rows[0]
    return {}


def _latest_row(rows: list[dict[str, Any]], date_keys: tuple[str, ...]) -> dict[str, Any]:
    if not rows:
        return {}
    return max(rows, key=lambda row: max((_timestamp(row.get(key)) for key in date_keys), default=0.0))


def _timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).timestamp()
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _number(value: Any, fallback: float = 0.0) -> float:
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.replace("$", "").replace(",", "").replace("%", ""))
        except ValueError:
            return fallback
    return fallback


def _text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    return json.dumps(jsonable(value), sort_keys=True)


def _text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_text(item) for item in value if _text(item)]
    if isinstance(value, tuple):
        return [_text(item) for item in value if _text(item)]
    if isinstance(value, str) and value.strip():
        parsed = _parsed_json(value)
        if isinstance(parsed, list):
            return [_text(item) for item in parsed if _text(item)]
        return [item.strip() for item in value.replace("|", ";").split(";") if item.strip()]
    return []


def _parsed_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _text_join(value: Any) -> str:
    items = _text_list(value)
    if items:
        return " ".join(items)
    return _text(value)


def _fmt_money(value: float) -> str:
    if not value:
        return "-"
    return f"${value:,.2f}" if abs(value) < 1000 else f"${value:,.0f}"


def _fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value
