"""One typed structured-request contract for Codex and DeepSeek adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from investment_panel.core.agent_providers import resolve_provider_selection
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

    selection = resolve_provider_selection(
        request.provider, request.model, request.reasoning_effort,
    )

    common = {
        "schema_name": request.schema_name,
        "schema": request.schema,
        "system_prompt": request.system_prompt,
        "compact": request.compact,
        "meta_sink": meta_sink,
        "model": selection.model,
        "reasoning_effort": selection.reasoning_effort,
        "timeout": request.timeout_seconds,
    }
    if selection.provider == "deepseek":
        result = _call_deepseek_structured(request.payload, **common)
    elif selection.provider == "codex":
        result = _call_codex_structured(request.payload, **common)
    else:  # pragma: no cover - selection is exhaustive, retained as a guard.
        raise ValueError(f"unsupported provider: {selection.provider}")
    if meta_sink is not None:
        reported_provider = str(meta_sink.get("provider") or selection.provider).strip().lower()
        reported_model = str(meta_sink.get("model") or selection.model).strip()
        if reported_provider != selection.provider or reported_model != selection.model:
            raise ValueError(
                "provider adapter identity does not match the registry selection: "
                f"{reported_provider}/{reported_model}"
            )
        meta_sink.update({
            "provider": selection.provider,
            "model": selection.model,
            "reasoning_effort": selection.reasoning_effort,
        })
    return result
