from __future__ import annotations

from typing import Any

import pytest

from investment_panel.providers import advisory
from investment_panel.providers.advisory import (
    AgentProviderError,
    StructuredProviderRequest,
    invoke_structured,
)


class _Response:
    def __init__(self, payload: Any, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = "mock response"

    def json(self) -> Any:
        return self._payload


def _request(
    provider: str = "deepseek",
    model: str = "deepseek-v4-flash",
    schema: dict[str, Any] | None = None,
) -> StructuredProviderRequest:
    return StructuredProviderRequest(
        provider=provider,  # type: ignore[arg-type]
        model=model,
        timeout_seconds=5,
        reasoning_effort="high",
        schema_name="test_schema",
        schema=schema or {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}},
        system_prompt="Return JSON.",
        payload={"request": {"request_id": "req-1"}},
    )


def _deepseek_payload(content: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 40,
            "completion_tokens_details": {"reasoning_tokens": 25},
            "prompt_cache_hit_tokens": 4,
        },
    }


def test_deepseek_result_has_identity_and_token_metadata(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> _Response:
        captured["url"] = url
        captured["body"] = kwargs["json"]
        return _Response(_deepseek_payload('{"ok": true}'))

    monkeypatch.setattr(advisory.httpx, "post", fake_post)
    result = invoke_structured(_request())

    assert result.payload == {"ok": True}
    assert (result.provider, result.model, result.reasoning_effort) == (
        "deepseek", "deepseek-v4-flash", "high",
    )
    assert result.token_metadata.input_tokens == 120
    assert result.token_metadata.output_tokens == 40
    assert result.token_metadata.reasoning_tokens == 25
    assert result.token_metadata.cached_input_tokens == 4
    assert captured["url"].endswith("/chat/completions")
    assert captured["body"]["response_format"] == {"type": "json_object"}


def test_deepseek_retries_empty_content_once(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = {"count": 0}

    def fake_post(url: str, **kwargs: Any) -> _Response:
        calls["count"] += 1
        return _Response(_deepseek_payload("" if calls["count"] == 1 else '{"ok": true}'))

    monkeypatch.setattr(advisory.httpx, "post", fake_post)
    assert invoke_structured(_request()).payload == {"ok": True}
    assert calls["count"] == 2


def test_deepseek_invalid_json_is_typed_failure(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(advisory.httpx, "post", lambda *_args, **_kwargs: _Response(_deepseek_payload("not json")))

    with pytest.raises(AgentProviderError, match="not JSON"):
        invoke_structured(_request())


def test_schema_failure_is_typed_failure(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(advisory.httpx, "post", lambda *_args, **_kwargs: _Response(_deepseek_payload('{"wrong": true}')))

    with pytest.raises(AgentProviderError, match="schema validation failed"):
        invoke_structured(_request())


def test_provider_identity_mismatch_fails_before_transport() -> None:
    with pytest.raises(AgentProviderError, match="not supported by provider codex"):
        invoke_structured(_request(provider="codex", model="deepseek-v4-flash"))


def test_codex_subprocess_failure_is_typed(monkeypatch) -> None:
    monkeypatch.setattr(advisory, "resolve_codex_bin", lambda: "codex-test")

    def fake_run(*_args: Any, **_kwargs: Any):
        return type("Completed", (), {"returncode": 3, "stderr": "child failed", "stdout": ""})()

    monkeypatch.setattr(advisory.subprocess, "run", fake_run)
    request = _request(provider="codex", model="gpt-5.6-luna")
    with pytest.raises(AgentProviderError, match="Codex agent failed 3"):
        invoke_structured(request)


def test_codex_timeout_is_typed(monkeypatch) -> None:
    monkeypatch.setattr(advisory, "resolve_codex_bin", lambda: "codex-test")

    def fake_run(*_args: Any, **_kwargs: Any):
        raise advisory.subprocess.TimeoutExpired("codex-test", 5, output="partial")

    monkeypatch.setattr(advisory.subprocess, "run", fake_run)
    with pytest.raises(AgentProviderError, match="timed out"):
        invoke_structured(_request(provider="codex", model="gpt-5.6-luna"))


def test_codex_invalid_json_is_typed(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(advisory, "resolve_codex_bin", lambda: "codex-test")

    def fake_run(cmd, **kwargs: Any):
        output_path = cmd[cmd.index("-o") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("not json")
        return type("Completed", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr(advisory.subprocess, "run", fake_run)
    with pytest.raises(AgentProviderError, match="invalid JSON"):
        invoke_structured(_request(provider="codex", model="gpt-5.6-luna"))
