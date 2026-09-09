"""Codex OAuth structured-output adapter for thesis-monitor automation."""

from __future__ import annotations

import copy
import json
from typing import Any, Mapping

from investment_panel.core.agent_providers import resolve_provider_selection
from investment_panel.core.continuous_advisor import CONTINUOUS_MAX_OUTPUT_TOKENS
from investment_panel.infrastructure.providers.advisory import StructuredProviderRequest, invoke_structured


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


CONTINUOUS_ADVISOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "symbol", "thesis", "countercase", "forecasts", "invalidations",
        "next_review_trigger", "evidence_refs", "change_since_prior",
    ],
    "properties": {
        "symbol": {"type": "string"},
        "countercase": {"type": "string"},
        "change_since_prior": {"type": "string"},
        "next_review_trigger": {"type": "string"},
        "next_review_at": {"type": ["string", "null"]},
        "outcome_date": {"type": ["string", "null"]},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "thesis": {
            **THESIS_MONITOR_SCHEMA["properties"]["thesis"],
            "required": [
                key for key in THESIS_MONITOR_SCHEMA["properties"]["thesis"]["required"]
                if key not in {"automation_policy", "lifecycle_status"}
            ],
            "properties": {
                key: value for key, value in THESIS_MONITOR_SCHEMA["properties"]["thesis"]["properties"].items()
                if key not in {"automation_policy", "lifecycle_status"}
            },
        },
        "forecasts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim_key", "statement", "horizon", "direction", "probability", "evidence_refs"],
                "properties": {
                    "claim_key": {"type": "string"},
                    "statement": {"type": "string"},
                    "horizon": {"type": "string"},
                    "direction": {"type": "string"},
                    "probability": {"type": "number"},
                    "target": {"type": ["number", "null"]},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "invalidations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim_key", "condition", "horizon", "probability", "evidence_refs"],
                "properties": {
                    "claim_key": {"type": "string"},
                    "condition": {"type": "string"},
                    "horizon": {"type": "string"},
                    "probability": {"type": "number"},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
    "$defs": THESIS_MONITOR_SCHEMA["$defs"],
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


def generate_codex_continuous_advisor(
    request_payload: dict[str, Any],
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    return _generate_continuous_advisor(
        request_payload, provider="codex", model=model, reasoning_effort=reasoning_effort,
    )


def generate_deepseek_continuous_advisor(
    request_payload: dict[str, Any],
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    return _generate_continuous_advisor(
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


def _generate_continuous_advisor(
    request_payload: dict[str, Any],
    *,
    provider: str,
    model: str | None,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    selection = resolve_provider_selection(provider, model, reasoning_effort or "high")
    template = request_payload.get("prompt_template")
    prompt_template = template if isinstance(template, Mapping) else {}
    provider_payload = _continuous_payload(request_payload, prompt_template)
    result = invoke_structured(
        StructuredProviderRequest(
            provider=selection.provider,  # type: ignore[arg-type]
            model=selection.model,
            timeout_seconds=90,
            reasoning_effort=selection.reasoning_effort,
            schema_name="continuous_advisor_v1",
            schema=CONTINUOUS_ADVISOR_SCHEMA,
            system_prompt=_continuous_system_prompt(prompt_template),
            payload=provider_payload,
            max_output_tokens=_positive_int(request_payload.get("max_output_tokens")) or CONTINUOUS_MAX_OUTPUT_TOKENS,
        )
    )
    metadata = result.metadata()
    metadata["provider_evidence_references"] = _packet_evidence_references(
        provider_payload.get("evidence_packet")
        if isinstance(provider_payload.get("evidence_packet"), Mapping) else {}
    )
    return {**result.payload, "_meta": metadata}


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


def _continuous_system_prompt(template: Mapping[str, Any] | None = None) -> str:
    prompt = (
        "You maintain a point-in-time portfolio and watchlist research advisor. "
        "Return only the strict JSON schema. Use only source references present "
        "in the supplied immutable evidence packet. State the thesis, countercase, "
        "probabilistic forecasts with explicit horizons, invalidation conditions, "
        "and the next review trigger. Missing or stale facts stay unavailable; "
        "never infer them. This is advisory, research-ranking-only, paper-only "
        "research. Never include orders, quantities, execution readiness, risk "
        "overrides, or instructions that change deterministic controls."
    )
    if not template:
        return prompt
    directives: list[str] = []
    for key, label in (
        ("system_focus", "Focus"),
        ("forecast_instruction", "Forecast instruction"),
        ("countercase_instruction", "Countercase instruction"),
        ("review_trigger_instruction", "Review-trigger instruction"),
    ):
        value = template.get(key)
        if isinstance(value, str) and value.strip():
            directives.append(f"{label}: {value.strip()[:800]}")
    order = template.get("evidence_order")
    if isinstance(order, list):
        names = [str(item).strip() for item in order if isinstance(item, str) and item.strip()]
        if names:
            directives.append("Evidence categories are presented in this priority order: " + ", ".join(names[:24]))
    budget = template.get("max_packet_tokens")
    if isinstance(budget, int) and not isinstance(budget, bool):
        directives.append(f"Use the supplied packet within its {budget}-token input budget.")
    return prompt + (" Approved advisory prompt directives:\n" + "\n".join(directives) if directives else "")


def _continuous_payload(
    request_payload: Mapping[str, Any],
    template: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply approved prompt controls to the provider copy, not stored lineage."""

    payload = dict(request_payload)
    packet = request_payload.get("evidence_packet")
    if not isinstance(packet, Mapping):
        return payload
    packet_copy = copy.deepcopy(dict(packet))
    evidence = packet_copy.get("evidence")
    if isinstance(evidence, Mapping):
        order = template.get("evidence_order")
        if isinstance(order, list):
            ordered: dict[str, Any] = {}
            for name in order:
                if isinstance(name, str) and name in evidence and name not in ordered:
                    ordered[name] = evidence[name]
            ordered.update({str(name): value for name, value in evidence.items() if str(name) not in ordered})
            packet_copy["evidence"] = ordered
    budget = template.get("max_packet_tokens")
    if isinstance(budget, int) and not isinstance(budget, bool):
        packet_copy = _fit_packet_budget(packet_copy, budget)
    payload["evidence_packet"] = packet_copy
    return payload


def _fit_packet_budget(packet: dict[str, Any], max_tokens: int) -> dict[str, Any]:
    """Trim only the provider copy until its JSON input is within the typed bound."""

    limit = max(256, max_tokens) * 4
    evidence = packet.get("evidence")
    if not isinstance(evidence, dict):
        return packet
    source_evidence = evidence.get("source_evidence")
    preserved_source = None
    if isinstance(source_evidence, list):
        preserved_index = next(
            (
                index for index, item in enumerate(source_evidence)
                if isinstance(item, dict) and str(item.get("reference") or "").strip()
            ),
            None,
        )
        if preserved_index is not None:
            preserved_source = source_evidence[preserved_index]
            source_evidence[0], source_evidence[preserved_index] = (
                source_evidence[preserved_index], source_evidence[0]
            )
    # Lower-priority collections are dropped first; the persisted packet is untouched.
    for category in (
        "source_evidence", "options", "catalysts", "macro_regime", "technicals",
        "fundamentals", "thesis", "portfolio_exposure", "prices",
    ):
        value = evidence.get(category)
        while _json_size(packet) > limit and value:
            if isinstance(value, list):
                if category == "source_evidence" and preserved_source is not None and len(value) == 1:
                    break
                value.pop()
            elif isinstance(value, dict):
                value.pop(next(reversed(value)))
            else:
                break
    for key in ("source_references", "blockers"):
        value = packet.get(key)
        while _json_size(packet) > limit and isinstance(value, list) and value:
            value.pop()
    if _json_size(packet) > limit and isinstance(preserved_source, dict):
        for key in tuple(preserved_source):
            if key == "reference":
                continue
            preserved_source.pop(key)
            if _json_size(packet) <= limit:
                break
    packet["source_references"] = _packet_evidence_references(packet)
    if _json_size(packet) > limit and preserved_source is not None:
        raise ValueError("max_packet_tokens cannot preserve source evidence")
    return packet


def _packet_evidence_references(packet: Mapping[str, Any]) -> list[str]:
    evidence = packet.get("evidence")
    source_evidence = evidence.get("source_evidence") if isinstance(evidence, Mapping) else []
    return sorted({
        str(item.get("reference"))
        for item in source_evidence if isinstance(item, Mapping) and str(item.get("reference") or "").strip()
    }) if isinstance(source_evidence, list) else []


def _json_size(value: Any) -> int:
    return len(json.dumps(value, default=str, separators=(",", ":")))


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


__all__ = [
    "CONTINUOUS_ADVISOR_SCHEMA",
    "THESIS_MONITOR_SCHEMA",
    "generate_codex_continuous_advisor",
    "generate_codex_thesis_monitor",
    "generate_deepseek_continuous_advisor",
    "generate_deepseek_thesis_monitor",
]
