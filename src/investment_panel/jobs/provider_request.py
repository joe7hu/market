"""One typed structured-request contract for Codex and DeepSeek adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from investment_panel.jobs.deepseek_option_agent import _call_deepseek_structured
from investment_panel.jobs.openai_option_agent import _call_codex_structured


ProviderName = Literal["codex", "deepseek"]


@dataclass(frozen=True)
class StructuredProviderRequest:
    provider: ProviderName
    model: str
    timeout_seconds: float
    reasoning_effort: str
    schema_name: str
    schema: dict[str, Any]
    system_prompt: str
    payload: dict[str, Any]
    compact: bool = False


def invoke_structured(request: StructuredProviderRequest, *, meta_sink: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dispatch only the typed adapter arguments each provider supports."""

    common = {
        "schema_name": request.schema_name,
        "schema": request.schema,
        "system_prompt": request.system_prompt,
        "compact": request.compact,
        "meta_sink": meta_sink,
        "model": request.model,
        "reasoning_effort": request.reasoning_effort,
        "timeout": request.timeout_seconds,
    }
    if request.provider == "deepseek":
        return _call_deepseek_structured(request.payload, **common)
    if request.provider == "codex":
        return _call_codex_structured(request.payload, **common)
    raise ValueError(f"unsupported provider: {request.provider}")
