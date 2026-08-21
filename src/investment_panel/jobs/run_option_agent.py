"""Single typed CLI for the advisory option-agent workflow."""

from __future__ import annotations

import argparse
import os
from typing import Any

from investment_panel.core.agent_providers import resolve_provider_selection
from investment_panel.providers.advisory import (
    AgentProviderError,
    StructuredProviderRequest,
    invoke_structured,
)
from investment_panel.jobs.option_agent_workflow import (
    POSTMORTEM_SCHEMA,
    THESIS_SCHEMA,
    agent_system_prompt,
    agent_wrapper_schema,
    compact_agent_batch,
    compact_request_payload,
    dispatch_agent_batch_refs,
    ensure_request_ref,
    normalize_postmortem,
    postmortem_system_prompt,
    read_stdin_json,
    run_cli,
    thesis_system_prompt,
)


def generate(
    payload: dict[str, Any],
    *,
    provider: str,
    task: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    try:
        selection = resolve_provider_selection(
            provider,
            model or _model_from_environment(provider),
            reasoning_effort or _effort_from_environment(provider),
        )
    except ValueError as exc:
        raise AgentProviderError(str(exc), provider=provider, model=model) from exc
    schema, system_prompt, request_payload = _workflow_request(payload, task)
    result = invoke_structured(
        StructuredProviderRequest(
            provider=selection.provider,  # type: ignore[arg-type]
            model=selection.model,
            timeout_seconds=timeout_seconds or _timeout_from_environment(provider),
            reasoning_effort=selection.reasoning_effort,
            schema_name=_schema_name(task),
            schema=schema,
            system_prompt=system_prompt,
            payload=request_payload,
        )
    )
    if task == "batch":
        output = dispatch_agent_batch_refs(result.payload, payload)
    elif task == "thesis":
        output = ensure_request_ref(result.payload, payload)
    else:
        output = normalize_postmortem(result.payload, payload)
    return {**output, "_meta": result.metadata()}


def _workflow_request(
    payload: dict[str, Any], task: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    if task == "batch":
        return agent_wrapper_schema(), agent_system_prompt(), compact_agent_batch(payload)
    if task == "thesis":
        return THESIS_SCHEMA, thesis_system_prompt(), compact_request_payload(payload)
    if task == "postmortem":
        return POSTMORTEM_SCHEMA, postmortem_system_prompt(), compact_request_payload(payload)
    raise ValueError(f"unsupported option-agent task: {task}")


def _schema_name(task: str) -> str:
    return {
        "batch": "option_agent_batch",
        "thesis": "option_thesis",
        "postmortem": "option_postmortem",
    }[task]


def _model_from_environment(provider: str) -> str | None:
    key = "MARKET_DEEPSEEK_MODEL" if provider == "deepseek" else "MARKET_CODEX_MODEL"
    return os.environ.get(key) or None


def _effort_from_environment(provider: str) -> str | None:
    key = "MARKET_DEEPSEEK_REASONING_EFFORT" if provider == "deepseek" else "MARKET_CODEX_REASONING_EFFORT"
    return os.environ.get(key) or None


def _timeout_from_environment(provider: str) -> float:
    key = "MARKET_DEEPSEEK_TIMEOUT_SECONDS" if provider == "deepseek" else "MARKET_CODEX_TIMEOUT_SECONDS"
    try:
        return float(os.environ.get(key, "90"))
    except ValueError:
        return 90.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one advisory option-agent task.")
    parser.add_argument("--provider", choices=("codex", "deepseek"), required=True)
    parser.add_argument("--task", choices=("batch", "thesis", "postmortem"), required=True)
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--timeout-seconds", type=float)
    args = parser.parse_args()
    return run_cli(
        lambda payload: generate(
            payload,
            provider=args.provider,
            task=args.task,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            timeout_seconds=args.timeout_seconds,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["generate", "main"]
