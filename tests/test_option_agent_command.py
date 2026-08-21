from __future__ import annotations

from typing import Any

from investment_panel.jobs import run_option_agent
from investment_panel.providers.advisory import (
    ProviderTokenMetadata,
    StructuredProviderResult,
)


def test_batch_command_uses_workflow_compaction_and_returns_provider_metadata(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_invoke(request):
        captured["request"] = request
        return StructuredProviderResult(
            payload={
                "thesis": [{"ticker": "NVDA"}],
                "postmortem": [{"ticker": "TSLA", "proposed_parameter_changes": {"x": None}}],
            },
            provider="codex",
            model="gpt-5.6-luna",
            reasoning_effort="high",
            token_metadata=ProviderTokenMetadata(input_tokens=10, output_tokens=4, estimated=True),
        )

    monkeypatch.setattr(run_option_agent, "invoke_structured", fake_invoke)
    payload = {
        "thesis": [{"request": {"request_id": "thesis-1"}, "context": {"secret": "not sent"}, "prompt": "p"}],
        "postmortem": [{"request": {"request_id": "post-1"}, "context": {"outcome": "loss"}}],
        "guardrails": {"advisory_only": True},
    }

    result = run_option_agent.generate(payload, provider="codex", task="batch")

    request = captured["request"]
    assert request.provider == "codex"
    assert request.schema_name == "option_agent_batch"
    assert request.payload["thesis"][0]["request"]["request_id"] == "thesis-1"
    assert set(request.payload["thesis"][0]) == {"request", "prompt", "context", "guardrails"}
    assert result["thesis"][0]["evidence_refs"][0]["id"] == "thesis-1"
    assert result["postmortem"][0]["evidence_refs"][0]["id"] == "post-1"
    assert result["_meta"]["provider"] == "codex"
    assert result["_meta"]["usage"]["output_tokens"] == 4


def test_task_command_normalizes_postmortem_reference(monkeypatch) -> None:
    def fake_invoke(request):
        return StructuredProviderResult(
            payload={"ticker": "TSLA", "proposed_parameter_changes": {"delta_min": None}},
            provider="deepseek",
            model="deepseek-v4-flash",
            reasoning_effort="high",
            token_metadata=ProviderTokenMetadata(),
        )

    monkeypatch.setattr(run_option_agent, "invoke_structured", fake_invoke)
    result = run_option_agent.generate(
        {"request": {"request_id": "post-1"}}, provider="deepseek", task="postmortem",
    )

    assert result["proposed_parameter_changes"] == {}
    assert result["evidence_refs"] == [{"type": "agent_request", "id": "post-1"}]
