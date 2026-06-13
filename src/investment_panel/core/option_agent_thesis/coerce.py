"""Auto-split from raw_sources.py — see ARCHITECTURE.md."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from investment_panel.core.option_agent_thesis.constants import PRICE_RE, STOP_WORDS
from investment_panel.core.option_agent_thesis.dbutil import _json_or_value


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        decoded = _json_or_value(value)
        if isinstance(decoded, list):
            return _string_list(decoded)
        if decoded != value:
            return _string_list(decoded)
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _catalyst_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        decoded = _json_or_value(value)
        if decoded != value:
            return _catalyst_list(decoded)
    if not isinstance(value, list):
        return []
    catalysts: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            catalysts.append({str(key): item.get(key) for key in item if item.get(key) not in (None, "")})
        elif str(item).strip():
            catalysts.append({"type": "unknown", "summary": str(item).strip()})
    return catalysts


def _catalyst_summary(catalysts: list[dict[str, Any]]) -> str:
    summaries = []
    for catalyst in catalysts[:3]:
        label = catalyst.get("type") or catalyst.get("expected_window") or "catalyst"
        watch = catalyst.get("what_to_watch") or catalyst.get("summary") or catalyst.get("description")
        summaries.append(f"{label}: {watch}" if watch else str(label))
    return "; ".join(summaries)


def _content_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(text).lower())
        if len(token) >= 4 and token not in STOP_WORDS
    }


def _invalidation_price(invalidation: list[str]) -> float | None:
    for item in invalidation:
        match = PRICE_RE.search(item)
        if match:
            return _number(match.group(1))
    return None


def _list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        decoded = _json_or_value(value)
        if isinstance(decoded, list):
            return decoded
        return [value] if value else []
    return []


def _date_string(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text


def _iso_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _confidence_score(value: Any) -> float:
    confidence = _number(value)
    if confidence is None:
        return 50.0
    if 0.0 <= confidence < 1.0:
        confidence *= 100.0
    return max(0.0, min(100.0, confidence))


def _metric_number(metrics: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(metrics.get(key))
        if value is not None:
            return value
    return None
