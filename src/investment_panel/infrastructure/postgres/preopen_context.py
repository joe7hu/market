"""Compact, decision-complete context for the pre-open narrative adapter."""

from __future__ import annotations

import json
from typing import Any


OPTION_FIELDS = (
    "ticker", "symbol", "state", "action", "score", "tier", "structure",
    "expiration", "dte", "strike", "entry_price", "ask", "secured_cash",
    "max_loss", "effective_assignment_price", "probability_profit",
    "probability_assignment", "expected_value", "risk_adjusted_expectancy",
    "top_reasons", "reasons", "blockers", "invalidation", "exit_plan",
)
SOURCE_FIELDS = (
    "id", "symbol", "observed_at", "sentiment", "confidence", "thesis",
    "antithesis", "invalidation", "title", "summary", "url", "source_name",
)
EVENT_FIELDS = ("event_id", "symbol", "starts_at", "title", "expected_impact", "notes")
MAX_PREOPEN_CONTEXT_CHARACTERS = 20_000


def compact_preopen_context(
    *,
    brief_date: str,
    qqq_forecast: dict[str, Any],
    backtest: dict[str, Any],
    catalysts: list[dict[str, Any]],
    option_rows: list[dict[str, Any]],
    source_changes: list[dict[str, Any]],
) -> dict[str, Any]:
    original = {"events": len(catalysts[:8]), "options": len(option_rows[:8]), "sources": len(source_changes[:12])}
    for source_limit, option_limit, text_limit in ((12, 8, 600), (8, 8, 400), (6, 6, 300), (4, 4, 220)):
        context = {
            "brief_date": brief_date,
            "qqq_forecast": qqq_forecast,
            "backtest": backtest,
            "key_events": [_pick(row, EVENT_FIELDS, text_limit) for row in catalysts[:8]],
            "market_environment": [_pick(row, OPTION_FIELDS, text_limit) for row in option_rows[:option_limit]],
            "fresh_source_items": [_pick(row, SOURCE_FIELDS, text_limit) for row in source_changes[:source_limit]],
        }
        coverage = {
            "included": {
                "events": len(context["key_events"]),
                "options": len(context["market_environment"]),
                "sources": len(context["fresh_source_items"]),
            },
            "available": original,
            "truncated": any(
                len(context[key]) < original[name]
                for key, name in (
                    ("key_events", "events"),
                    ("market_environment", "options"),
                    ("fresh_source_items", "sources"),
                )
            ),
            "character_count": 0,
            "max_characters": MAX_PREOPEN_CONTEXT_CHARACTERS,
        }
        context["coverage"] = coverage
        # The count is part of the serialized payload, so converge after adding
        # coverage rather than checking the smaller pre-coverage object.
        for _ in range(3):
            character_count = len(json.dumps(context, default=str, separators=(",", ":")))
            coverage["character_count"] = character_count
        if character_count <= MAX_PREOPEN_CONTEXT_CHARACTERS:
            return context
    raise ValueError("pre-open narrative context exceeds hard character budget")


def _pick(row: dict[str, Any], fields: tuple[str, ...], text_limit: int) -> dict[str, Any]:
    return {
        field: _bounded(row.get(field), text_limit=text_limit)
        for field in fields
        if row.get(field) not in (None, "", [], {})
    }


def _bounded(value: Any, *, text_limit: int) -> Any:
    if isinstance(value, str):
        return value[:text_limit]
    if isinstance(value, list):
        return [_bounded(item, text_limit=text_limit) for item in value[:8]]
    if isinstance(value, dict):
        return {str(key): _bounded(item, text_limit=text_limit) for key, item in list(value.items())[:12]}
    return value
