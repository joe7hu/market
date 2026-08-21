"""Codex OAuth structured-output adapter for thesis-monitor automation."""

from __future__ import annotations

from typing import Any

from investment_panel.core.agent_providers import resolve_provider_selection
from investment_panel.providers.advisory import StructuredProviderRequest, invoke_structured


THESIS_MONITOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["symbol", "thesis", "evidence_assessments", "change_rationale"],
    "properties": {
        "symbol": {"type": "string"},
        "change_rationale": {"type": "string"},
        "thesis": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "core_thesis", "why_owned_watched", "direction", "timeframe",
                "horizon_date", "conviction", "confidence", "pillars",
                "scenarios", "catalysts", "invalidation_rules",
                "review_cadence_days", "next_review_date", "lifecycle_status",
                "evidence_coverage_status", "automation_policy", "evidence_links",
            ],
            "properties": {
                "core_thesis": {"type": "string"},
                "why_owned_watched": {"type": "string"},
                "direction": {"type": "string"},
                "timeframe": {"type": "string"},
                "horizon_date": {"type": "string"},
                "conviction": {"type": "string"},
                "confidence": {"type": "string"},
                "pillars": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["id", "title", "claim", "evidence_refs"],
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "claim": {"type": "string"},
                            "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "scenarios": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["base", "bull", "bear"],
                    "properties": {
                        "base": {"$ref": "#/$defs/scenario"},
                        "bull": {"$ref": "#/$defs/scenario"},
                        "bear": {"$ref": "#/$defs/scenario"},
                    },
                },
                "catalysts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["id", "title", "date", "success_condition", "evidence_refs"],
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "date": {"type": "string"},
                            "success_condition": {"type": "string"},
                            "evidence_refs": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "invalidation_rules": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["id", "type", "operator", "text", "price", "metric", "event", "date"],
                        "properties": {
                            "id": {"type": "string"},
                            "type": {"type": "string", "enum": ["price", "fundamental", "event", "time"]},
                            "operator": {"type": "string"},
                            "text": {"type": "string"},
                            "price": {"type": ["number", "null"]},
                            "metric": {"type": ["string", "null"]},
                            "event": {"type": ["string", "null"]},
                            "date": {"type": ["string", "null"]},
                        },
                    },
                },
                "review_cadence_days": {"type": "integer"},
                "next_review_date": {"type": "string"},
                "lifecycle_status": {"type": "string"},
                "evidence_coverage_status": {"type": "string"},
                "automation_policy": {"type": "string"},
                "evidence_links": {"type": "array", "items": {"type": "string"}},
            },
        },
        "evidence_assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "evidence_reference", "evidence_title", "evidence_date",
                    "stance", "materiality", "affected_pillar_ids",
                    "confidence", "rationale",
                ],
                "properties": {
                    "evidence_reference": {"type": "string"},
                    "evidence_title": {"type": "string"},
                    "evidence_date": {"type": ["string", "null"]},
                    "stance": {"type": "string", "enum": ["support", "contradict", "neutral", "insufficient"]},
                    "materiality": {"type": "string", "enum": ["low", "medium", "high"]},
                    "affected_pillar_ids": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                },
            },
        },
    },
    "$defs": {
        "scenario": {
            "type": "object",
            "additionalProperties": False,
            "required": ["probability", "target", "rationale"],
            "properties": {
                "probability": {"type": "number"},
                "target": {"type": ["number", "null"]},
                "rationale": {"type": "string"},
            },
        }
    },
}


def generate_codex_thesis_monitor(
    request_payload: dict[str, Any],
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    return _generate_thesis_monitor(
        request_payload, provider="codex", model=model, reasoning_effort=reasoning_effort,
    )


def generate_deepseek_thesis_monitor(
    request_payload: dict[str, Any],
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """DeepSeek API variant of the thesis-monitor structured adapter."""

    return _generate_thesis_monitor(
        request_payload, provider="deepseek", model=model, reasoning_effort=reasoning_effort,
    )


def _generate_thesis_monitor(
    request_payload: dict[str, Any],
    *,
    provider: str,
    model: str | None,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    selection = resolve_provider_selection(provider, model, reasoning_effort or "high")
    result = invoke_structured(
        StructuredProviderRequest(
            provider=selection.provider,  # type: ignore[arg-type]
            model=selection.model,
            timeout_seconds=90,
            reasoning_effort=selection.reasoning_effort,
            schema_name="thesis_monitor_v3",
            schema=THESIS_MONITOR_SCHEMA,
            system_prompt=_system_prompt(),
            payload=request_payload,
        )
    )
    return {**result.payload, "_meta": result.metadata()}


def _system_prompt() -> str:
    return (
        "You maintain a professional portfolio thesis monitor. Return only the "
        "strict JSON schema. Use only supplied evidence_reference values; never "
        "invent sources. Evidence-thin names may still get an active thesis, but "
        "confidence must be low and evidence_coverage_status must be low or "
        "insufficient. You may revise human-authored theses, but your output is "
        "advisory and research-ranking-only. Never recommend orders, execution "
        "readiness, or clearing deterministic risk gates."
    )


__all__ = [
    "THESIS_MONITOR_SCHEMA",
    "generate_codex_thesis_monitor",
    "generate_deepseek_thesis_monitor",
]
