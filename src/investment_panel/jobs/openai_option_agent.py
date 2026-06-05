"""OpenAI-backed command adapters for options-radar agent handoffs."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx


DEFAULT_MODEL = "gpt-5.2"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIOptionAgentError(RuntimeError):
    """Raised when the OpenAI option agent command cannot return JSON."""


THESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "ticker",
        "bull_target_price",
        "bull_target_date",
        "base_target_price",
        "core_thesis",
        "required_proofs",
        "catalysts",
        "invalidation",
        "bear_case",
        "confidence",
        "evidence_refs",
    ],
    "properties": {
        "ticker": {"type": "string"},
        "bull_target_price": {"type": "number"},
        "bull_target_date": {"type": "string"},
        "base_target_price": {"type": "number"},
        "core_thesis": {"type": "string"},
        "required_proofs": {"type": "array", "items": {"type": "string"}},
        "catalysts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "expected_window", "what_to_watch"],
                "properties": {
                    "type": {"type": "string"},
                    "expected_window": {"type": "string"},
                    "what_to_watch": {"type": "string"},
                },
            },
        },
        "invalidation": {"type": "array", "items": {"type": "string"}},
        "bear_case": {"type": "string"},
        "confidence": {"type": "number"},
        "evidence_refs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "id"],
                "properties": {
                    "type": {"type": "string"},
                    "id": {"type": "string"},
                },
            },
        },
    },
}


POSTMORTEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "ticker",
        "strategy_version",
        "source_type",
        "source_id",
        "outcome_type",
        "failure_type",
        "evidence",
        "proposed_rule_change",
        "proposed_parameter_changes",
        "expected_effect",
        "risk",
        "confidence",
        "evidence_refs",
    ],
    "properties": {
        "ticker": {"type": "string"},
        "strategy_version": {"type": "string"},
        "source_type": {"type": "string"},
        "source_id": {"type": "string"},
        "outcome_type": {"type": "string"},
        "failure_type": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "proposed_rule_change": {"type": "string"},
        "proposed_parameter_changes": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "delta_min",
                "delta_max",
                "dte_min",
                "dte_max",
                "max_spread_pct",
                "reject_spread_pct",
                "min_open_interest",
                "min_volume",
                "max_required_move_pct",
                "max_iv_percentile",
                "reject_iv_percentile",
                "require_price_above_ma50",
                "require_rs_improving",
                "candidate_note",
                "filter_reason",
                "setup_type",
            ],
            "properties": {
                "delta_min": {"type": ["number", "null"]},
                "delta_max": {"type": ["number", "null"]},
                "dte_min": {"type": ["number", "null"]},
                "dte_max": {"type": ["number", "null"]},
                "max_spread_pct": {"type": ["number", "null"]},
                "reject_spread_pct": {"type": ["number", "null"]},
                "min_open_interest": {"type": ["number", "null"]},
                "min_volume": {"type": ["number", "null"]},
                "max_required_move_pct": {"type": ["number", "null"]},
                "max_iv_percentile": {"type": ["number", "null"]},
                "reject_iv_percentile": {"type": ["number", "null"]},
                "require_price_above_ma50": {"type": ["boolean", "null"]},
                "require_rs_improving": {"type": ["boolean", "null"]},
                "candidate_note": {"type": ["string", "null"]},
                "filter_reason": {"type": ["string", "null"]},
                "setup_type": {"type": ["string", "null"]},
            },
        },
        "expected_effect": {"type": "string"},
        "risk": {"type": "string"},
        "confidence": {"type": "number"},
        "evidence_refs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "id"],
                "properties": {
                    "type": {"type": "string"},
                    "id": {"type": "string"},
                },
            },
        },
    },
}


def generate_openai_option_thesis(request_payload: dict[str, Any]) -> dict[str, Any]:
    result = _call_openai_structured(
        request_payload,
        schema_name="option_thesis",
        schema=THESIS_SCHEMA,
        system_prompt=_thesis_system_prompt(),
    )
    return _ensure_request_ref(result, request_payload)


def generate_openai_option_postmortem(request_payload: dict[str, Any]) -> dict[str, Any]:
    result = _call_openai_structured(
        request_payload,
        schema_name="option_postmortem",
        schema=POSTMORTEM_SCHEMA,
        system_prompt=_postmortem_system_prompt(),
    )
    changes = result.get("proposed_parameter_changes")
    if isinstance(changes, dict):
        result["proposed_parameter_changes"] = {
            key: value
            for key, value in changes.items()
            if value is not None and value != ""
        }
    return _ensure_request_ref(result, request_payload)


def _call_openai_structured(
    request_payload: dict[str, Any],
    *,
    schema_name: str,
    schema: dict[str, Any],
    system_prompt: str,
) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise OpenAIOptionAgentError("OPENAI_API_KEY is required")
    model = os.environ.get("MARKET_OPENAI_MODEL", DEFAULT_MODEL)
    base_url = os.environ.get("MARKET_OPENAI_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    timeout = float(os.environ.get("MARKET_OPENAI_TIMEOUT_SECONDS", "90"))
    max_output_tokens = int(os.environ.get("MARKET_OPENAI_MAX_OUTPUT_TOKENS", "2000"))
    body = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(_compact_request_payload(request_payload), default=str)},
        ],
        "max_output_tokens": max_output_tokens,
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        },
    }
    response = httpx.post(
        f"{base_url}/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise OpenAIOptionAgentError(f"OpenAI request failed {response.status_code}: {response.text[:500]}")
    data = response.json()
    if data.get("error"):
        raise OpenAIOptionAgentError(f"OpenAI response error: {data['error']}")
    text = _extract_output_text(data)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OpenAIOptionAgentError(f"OpenAI output was not JSON: {text[:500]}") from exc
    if not isinstance(parsed, dict):
        raise OpenAIOptionAgentError("OpenAI output must be a JSON object")
    return parsed


def _extract_output_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    texts: list[str] = []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "refusal":
                raise OpenAIOptionAgentError(f"OpenAI refused request: {part.get('refusal') or part}")
            if part.get("type") == "output_text" and part.get("text"):
                texts.append(str(part["text"]))
    text = "".join(texts).strip()
    if not text:
        raise OpenAIOptionAgentError("OpenAI response did not include output_text")
    return text


def _compact_request_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "request": payload.get("request") or {},
        "prompt": payload.get("prompt") or "",
        "context": payload.get("context") or {},
        "guardrails": payload.get("guardrails") or {},
    }


def _ensure_request_ref(result: dict[str, Any], request_payload: dict[str, Any]) -> dict[str, Any]:
    request_id = str((request_payload.get("request") or {}).get("request_id") or "")
    if not request_id:
        return result
    refs = result.get("evidence_refs")
    refs = refs if isinstance(refs, list) else []
    if not any(isinstance(ref, dict) and ref.get("id") == request_id for ref in refs):
        refs.insert(0, {"type": "agent_request", "id": request_id})
    return {**result, "evidence_refs": refs}


def _thesis_system_prompt() -> str:
    return (
        "You generate structured 10x options-radar hypotheses only. "
        "Use the supplied candidate, stock, option, source-signal, and news context. "
        "Do not recommend or execute trades. Do not change deterministic scores. "
        "Create falsifiable thesis, proof, catalyst, invalidation, and red-team material. "
        "Use stored evidence references from context whenever possible."
    )


def _postmortem_system_prompt() -> str:
    return (
        "You write structured options-radar postmortems only. "
        "Use the supplied outcome, attribution, candidate, and thesis context. "
        "You may propose rule or parameter changes, but deterministic code decides "
        "backtests, forward tests, and promotion. Do not recommend trades."
    )


def _read_stdin_json() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise OpenAIOptionAgentError("stdin must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise OpenAIOptionAgentError("stdin must be a JSON object")
    return payload


def _write_stdout_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")


def main_thesis() -> int:
    try:
        _write_stdout_json(generate_openai_option_thesis(_read_stdin_json()))
    except OpenAIOptionAgentError as exc:
        sys.stderr.write(f"OpenAI option agent error: {exc}\n")
        return 1
    return 0


def main_postmortem() -> int:
    try:
        _write_stdout_json(generate_openai_option_postmortem(_read_stdin_json()))
    except OpenAIOptionAgentError as exc:
        sys.stderr.write(f"OpenAI option agent error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main_thesis())
