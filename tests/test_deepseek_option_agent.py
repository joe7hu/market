"""DeepSeek API provider adapter tests."""

from __future__ import annotations

from typing import Any

import pytest

from investment_panel.jobs import deepseek_option_agent as adapter_mod
from investment_panel.jobs.codex_thesis_monitor import generate_deepseek_thesis_monitor
from investment_panel.jobs.deepseek_option_agent import (
    generate_deepseek_option_agent,
    generate_deepseek_option_postmortem,
    generate_deepseek_option_thesis,
)
from investment_panel.jobs.openai_option_agent import OpenAIOptionAgentError
from investment_panel.database.agent_process import agent_env


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "mock"

    def json(self) -> dict[str, Any]:
        return self._payload


def _chat_payload(content: str = '{"ok": true}') -> dict[str, Any]:
    return {
        "model": "deepseek-v4-flash",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 40,
            "total_tokens": 160,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 25},
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 120,
        },
    }


def _request_payload() -> dict[str, Any]:
    return {"request": {"request_id": "req_1"}, "prompt": "analyze", "context": {"ticker": "NVDA"}}


def test_deepseek_structured_uses_v4_flash_json_mode_and_usage(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["body"] = kwargs["json"]
        return _FakeResponse(payload=_chat_payload())

    monkeypatch.setattr(adapter_mod.httpx, "post", fake_post)
    meta: dict[str, Any] = {}
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    result = adapter_mod._call_deepseek_structured(
        _request_payload(),
        schema_name="option_thesis",
        schema=schema,
        system_prompt="You emit JSON.",
        meta_sink=meta,
        reasoning_effort="high",
    )

    assert result == {"ok": True}
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["body"]["model"] == "deepseek-v4-flash"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert "reasoning_effort" not in captured["body"]
    assert captured["body"]["temperature"] == 0.0
    assert captured["body"]["max_tokens"] == 24000
    assert captured["body"]["messages"][0]["role"] == "system"
    assert "Return exactly one JSON object conforming to this schema:" in captured["body"]["messages"][0]["content"]
    assert '"required": ["ok"]' in captured["body"]["messages"][0]["content"]
    assert meta == {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "reasoning_effort": "high",
        "schema_name": "option_thesis",
        "estimated": False,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 40,
            "reasoning_tokens": 25,
            "cached_input_tokens": 0,
        },
    }


def test_deepseek_structured_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(OpenAIOptionAgentError, match="DEEPSEEK_API_KEY"):
        adapter_mod._call_deepseek_structured({}, schema_name="x", schema={}, system_prompt="p")


def test_deepseek_structured_http_error(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(status_code=401, payload={"error": {"message": "bad key"}})

    monkeypatch.setattr(adapter_mod.httpx, "post", fake_post)
    with pytest.raises(OpenAIOptionAgentError, match="401") as exc_info:
        adapter_mod._call_deepseek_structured({}, schema_name="x", schema={}, system_prompt="p")
    assert exc_info.value.meta["provider"] == "deepseek"
    assert exc_info.value.meta["model"] == "deepseek-v4-flash"


def test_deepseek_structured_strips_markdown_fences(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    fenced = '```json\n{"ok": true}\n```'
    monkeypatch.setattr(
        adapter_mod.httpx,
        "post",
        lambda url, **kwargs: _FakeResponse(payload=_chat_payload(content=fenced)),
    )
    result = adapter_mod._call_deepseek_structured({}, schema_name="x", schema={}, system_prompt="p")
    assert result == {"ok": True}


def test_deepseek_structured_retries_empty_content_once(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = {"count": 0}

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        calls["count"] += 1
        if calls["count"] == 1:
            return _FakeResponse(payload=_chat_payload(content=""))
        return _FakeResponse(payload=_chat_payload())

    monkeypatch.setattr(adapter_mod.httpx, "post", fake_post)
    result = adapter_mod._call_deepseek_structured({}, schema_name="x", schema={}, system_prompt="p")
    assert result == {"ok": True}
    assert calls["count"] == 2


def test_deepseek_structured_raises_after_empty_retries(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = {"count": 0}

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        calls["count"] += 1
        return _FakeResponse(payload=_chat_payload(content=""))

    monkeypatch.setattr(adapter_mod.httpx, "post", fake_post)
    with pytest.raises(OpenAIOptionAgentError, match="did not include content"):
        adapter_mod._call_deepseek_structured({}, schema_name="x", schema={}, system_prompt="p")
    assert calls["count"] == 2


def test_deepseek_generators_dispatch_to_adapter(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls: list[dict[str, Any]] = []

    def fake_call(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append({"payload": payload, **kwargs})
        return {"thesis": [{"ticker": "NVDA"}], "postmortem": [{"ticker": "TSLA"}]}

    monkeypatch.setattr(adapter_mod, "_call_deepseek_structured", fake_call)
    batch = {
        "thesis": [{"request": {"request_id": "req_a"}}],
        "postmortem": [{"request": {"request_id": "req_b"}}],
    }
    result = generate_deepseek_option_agent(batch)

    assert len(calls) == 1
    assert calls[0]["schema_name"] == "option_agent_batch"
    assert calls[0]["compact"] is False
    assert result["_meta"] == {}
    assert result["thesis"][0]["evidence_refs"][0]["id"] == "req_a"
    assert result["postmortem"][0]["evidence_refs"][0]["id"] == "req_b"

    calls.clear()
    generate_deepseek_option_thesis(_request_payload())
    assert calls[0]["schema_name"] == "option_thesis"
    assert calls[0].get("compact", True) is True

    calls.clear()
    generate_deepseek_option_postmortem(_request_payload())
    assert calls[0]["schema_name"] == "option_postmortem"
    assert calls[0].get("compact", True) is True


def test_deepseek_thesis_monitor_uses_adapter(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_call(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append({"payload": payload, **kwargs})
        return {"symbol": "NVDA", "thesis": {}, "evidence_assessments": []}

    monkeypatch.setattr("investment_panel.jobs.codex_thesis_monitor._call_deepseek_structured", fake_call)
    result = generate_deepseek_thesis_monitor({"request": {"request_id": "r"}}, model="deepseek-v4-flash")

    assert len(calls) == 1
    assert calls[0]["schema_name"] == "thesis_monitor_v3"
    assert calls[0]["compact"] is False
    assert calls[0]["model"] == "deepseek-v4-flash"
    assert calls[0]["reasoning_effort"] is None
    assert result["_meta"] == {}


def test_deepseek_agent_environment_is_provider_specific() -> None:
    assert agent_env(
        provider="deepseek", model="deepseek-v4-flash", reasoning_effort="high", timeout_seconds=90,
    ) == {
        "MARKET_OPTION_AGENT_PROVIDER": "deepseek",
        "MARKET_DEEPSEEK_MODEL": "deepseek-v4-flash",
        "MARKET_DEEPSEEK_REASONING_EFFORT": "high",
        "MARKET_DEEPSEEK_TIMEOUT_SECONDS": "75",
    }
