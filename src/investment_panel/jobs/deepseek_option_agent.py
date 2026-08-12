"""DeepSeek API structured-output adapters for Market AI workflows.

DeepSeek exposes an OpenAI-compatible ``/chat/completions`` surface.  The
``deepseek-v4-flash`` model is reasoning-capable, so the adapter requests JSON
objects via ``response_format`` and records the real token usage (including the
reasoning-token breakdown) instead of estimating it like the Codex CLI path.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from investment_panel.jobs.openai_option_agent import (
    OpenAIOptionAgentError,
    _agent_system_prompt,
    _agent_wrapper_schema,
    _compact_agent_batch,
    _compact_request_payload,
    _dispatch_agent_batch_refs,
    _ensure_request_ref,
    _postmortem_system_prompt,
    _read_stdin_json,
    _run_cli,
    _thesis_system_prompt,
)
from investment_panel.jobs.option_agent_contract import POSTMORTEM_SCHEMA, THESIS_SCHEMA


DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def _call_deepseek_structured(
    request_payload: dict[str, Any],
    *,
    schema_name: str,
    schema: dict[str, Any],
    system_prompt: str,
    compact: bool = True,
    meta_sink: dict[str, Any] | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Structured JSON call against the OpenAI-compatible DeepSeek API.

    Uses ``/chat/completions`` with ``response_format: json_object`` so the
    reasoning-capable ``deepseek-v4-flash`` model returns parseable JSON.  Unlike
    the OpenAI Responses API or the Codex CLI, DeepSeek does not enforce the
    schema server-side, so the schema is embedded in the system prompt to stop
    the model from mirroring the request shape (for example ``current_thesis``)
    instead of emitting the required output fields.
    """

    body_payload = _compact_request_payload(request_payload) if compact else request_payload
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise OpenAIOptionAgentError("DEEPSEEK_API_KEY is required for the deepseek provider")
    model_name = (model or os.environ.get("MARKET_DEEPSEEK_MODEL", "") or DEFAULT_DEEPSEEK_MODEL).strip()
    selected_reasoning_effort = (
        reasoning_effort
        if reasoning_effort is not None
        else os.environ.get("MARKET_DEEPSEEK_REASONING_EFFORT", "")
    ).strip().lower()
    base_url = os.environ.get("MARKET_DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL).rstrip("/")
    effective_timeout = timeout if timeout is not None else float(os.environ.get("MARKET_DEEPSEEK_TIMEOUT_SECONDS", "90"))
    max_output_tokens = int(os.environ.get("MARKET_DEEPSEEK_MAX_OUTPUT_TOKENS", "24000"))
    request_meta = {
        "provider": "deepseek",
        "model": model_name,
        "reasoning_effort": selected_reasoning_effort,
        "schema_name": schema_name,
        "estimated": False,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cached_input_tokens": 0,
        },
    }
    if meta_sink is not None:
        meta_sink.update(request_meta)
    body = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": (
                    f"{system_prompt}\n\n"
                    "Return exactly one JSON object conforming to this schema:\n"
                    f"{json.dumps(schema, default=str)}"
                ),
            },
            {"role": "user", "content": json.dumps(body_payload, default=str)},
        ],
        "max_tokens": max_output_tokens,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    # Reasoning models occasionally spend the whole output budget on
    # ``reasoning_content`` and return empty content; one bounded retry is much
    # cheaper than a failed scheduled run and resolves the transient.
    text = ""
    data: dict[str, Any] = {}
    for _attempt in range(2):
        try:
            response = httpx.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=effective_timeout,
            )
        except httpx.TimeoutException as exc:
            raise OpenAIOptionAgentError(
                f"DeepSeek request timed out after {effective_timeout:g}s",
                meta=request_meta,
            ) from exc
        except httpx.HTTPError as exc:
            raise OpenAIOptionAgentError(
                f"DeepSeek request failed: {exc}", meta=request_meta,
            ) from exc
        if response.status_code >= 400:
            raise OpenAIOptionAgentError(
                f"DeepSeek request failed {response.status_code}: {response.text[:500]}", meta=request_meta,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise OpenAIOptionAgentError("DeepSeek response was not valid JSON", meta=request_meta) from exc
        if data.get("error"):
            raise OpenAIOptionAgentError(f"DeepSeek response error: {data['error']}", meta=request_meta)
        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        if not choices:
            raise OpenAIOptionAgentError("DeepSeek response did not include choices", meta=request_meta)
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        text = _strip_json_fences(str(message.get("content") or "").strip())
        if text:
            break
    if not text:
        raise OpenAIOptionAgentError("DeepSeek response did not include content", meta=request_meta)
    if meta_sink is not None:
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        completion_details = (
            usage.get("completion_tokens_details")
            if isinstance(usage.get("completion_tokens_details"), dict)
            else {}
        )
        meta_sink.update({
            **request_meta,
            "usage": {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "reasoning_tokens": int(completion_details.get("reasoning_tokens") or 0),
                "cached_input_tokens": int(usage.get("prompt_cache_hit_tokens") or 0),
            },
        })
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OpenAIOptionAgentError(f"DeepSeek output was not JSON: {text[:500]}", meta=request_meta) from exc
    if not isinstance(parsed, dict):
        raise OpenAIOptionAgentError("DeepSeek output must be a JSON object", meta=request_meta)
    return parsed


def generate_deepseek_option_agent(
    payload: dict[str, Any], *, reasoning_effort: str | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    result = _call_deepseek_structured(
        _compact_agent_batch(payload),
        schema_name="option_agent_batch",
        schema=_agent_wrapper_schema(),
        system_prompt=_agent_system_prompt(),
        compact=False,
        meta_sink=meta,
        reasoning_effort=reasoning_effort,
    )
    return {**_dispatch_agent_batch_refs(result, payload), "_meta": meta}


def generate_deepseek_option_thesis(
    request_payload: dict[str, Any], *, reasoning_effort: str | None = None,
) -> dict[str, Any]:
    result = _call_deepseek_structured(
        request_payload,
        schema_name="option_thesis",
        schema=THESIS_SCHEMA,
        system_prompt=_thesis_system_prompt(),
        reasoning_effort=reasoning_effort,
    )
    return _ensure_request_ref(result, request_payload)


def generate_deepseek_option_postmortem(
    request_payload: dict[str, Any], *, reasoning_effort: str | None = None,
) -> dict[str, Any]:
    result = _call_deepseek_structured(
        request_payload,
        schema_name="option_postmortem",
        schema=POSTMORTEM_SCHEMA,
        system_prompt=_postmortem_system_prompt(),
        reasoning_effort=reasoning_effort,
    )
    changes = result.get("proposed_parameter_changes")
    if isinstance(changes, dict):
        result["proposed_parameter_changes"] = {
            key: value
            for key, value in changes.items()
            if value is not None and value != ""
        }
    return _ensure_request_ref(result, request_payload)


def _strip_json_fences(text: str) -> str:
    """Remove optional markdown code fences around a JSON payload."""

    candidate = text.strip()
    if candidate.startswith("```"):
        first_newline = candidate.find("\n")
        if first_newline != -1:
            candidate = candidate[first_newline + 1 :]
        if candidate.rstrip().endswith("```"):
            candidate = candidate.rstrip()[:-3].rstrip()
    return candidate.strip()


def main_deepseek_thesis() -> int:
    return _run_cli(generate_deepseek_option_thesis)


def main_deepseek_postmortem() -> int:
    return _run_cli(generate_deepseek_option_postmortem)


def main_deepseek_agent() -> int:
    return _run_cli(generate_deepseek_option_agent)


if __name__ == "__main__":
    raise SystemExit(main_deepseek_agent())
