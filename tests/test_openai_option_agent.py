from __future__ import annotations

import base64
import json as jsonlib
import subprocess
import time

import pytest

from investment_panel.jobs import openai_option_agent
from investment_panel.jobs import openai_option_agent_auth


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self) -> dict:
        return self._payload


def test_openai_thesis_agent_uses_responses_structured_outputs(monkeypatch) -> None:
    captured: dict = {}

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        captured["timeout"] = timeout
        return FakeResponse(
            {
                "output_text": jsonlib.dumps(
                    {
                        "ticker": "TSLA",
                        "bull_target_price": 650,
                        "bull_target_date": "2028-01-21",
                        "base_target_price": 520,
                        "core_thesis": "Autonomy and energy storage narratives re-rate the setup.",
                        "required_proofs": ["margins stabilize"],
                        "catalysts": [{"type": "earnings", "expected_window": "next 2 quarters", "what_to_watch": "margins"}],
                        "invalidation": ["stock breaks below $80 without recovery"],
                        "bear_case": "Demand softness can keep the stock below trend.",
                        "confidence": 55,
                        "evidence_refs": [{"type": "candidate_event", "id": "event-1"}],
                    }
                )
            }
        )

    monkeypatch.delenv("MARKET_OPENAI_AUTH_MODE", raising=False)
    monkeypatch.delenv("MARKET_OPENAI_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MARKET_OPENAI_MODEL", "gpt-test")
    monkeypatch.setattr(openai_option_agent.httpx, "post", fake_post)

    result = openai_option_agent.generate_openai_option_thesis(
        {"request": {"request_id": "req-1", "ticker": "TSLA"}, "prompt": "make thesis", "context": {}}
    )

    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["body"]["model"] == "gpt-test"
    assert captured["body"]["store"] is False
    assert captured["body"]["text"]["format"]["type"] == "json_schema"
    assert captured["body"]["text"]["format"]["strict"] is True
    assert captured["body"]["text"]["format"]["schema"]["additionalProperties"] is False
    assert "tools" not in captured["body"]
    system_prompt = captured["body"]["input"][0]["content"]
    assert "not technical analysis" in system_prompt
    assert "12-24 month business prediction" in system_prompt
    assert "not price action, moving averages, IV" in system_prompt
    assert result["ticker"] == "TSLA"
    assert result["evidence_refs"][0] == {"type": "agent_request", "id": "req-1"}


def test_openai_postmortem_agent_filters_null_parameter_changes(monkeypatch) -> None:
    def fake_post(url, headers, json, timeout):
        assert url == "https://api.openai.com/v1/responses"
        assert headers["Authorization"] == "Bearer test-key"
        assert json["text"]["format"]["name"] == "option_postmortem"
        assert timeout == 90.0
        return FakeResponse(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": jsonlib.dumps(
                                    {
                                        "ticker": "RBLX",
                                        "strategy_version": "leap_10x_reversal_v1",
                                        "source_type": "missed_winner",
                                        "source_id": "missed-1",
                                        "outcome_type": "missed_10x_winner",
                                        "failure_type": "delta_range_too_strict",
                                        "evidence": ["Contract was filtered before it ran."],
                                        "proposed_rule_change": "Test a lower-delta sleeve.",
                                        "proposed_parameter_changes": {
                                            "delta_min": 0.1,
                                            "delta_max": None,
                                            "dte_min": None,
                                            "dte_max": None,
                                            "max_spread_pct": None,
                                            "reject_spread_pct": None,
                                            "min_open_interest": None,
                                            "min_volume": None,
                                            "max_required_move_pct": None,
                                            "max_iv_percentile": None,
                                            "reject_iv_percentile": None,
                                            "require_price_above_ma50": None,
                                            "require_rs_improving": True,
                                            "candidate_note": "",
                                            "filter_reason": None,
                                            "setup_type": None,
                                        },
                                        "expected_effect": "Improve recall.",
                                        "risk": "More false positives.",
                                        "confidence": 60,
                                        "evidence_refs": [{"type": "missed_winner", "id": "missed-1"}],
                                    }
                                ),
                            }
                        ],
                    }
                ]
            }
        )

    monkeypatch.delenv("MARKET_OPENAI_AUTH_MODE", raising=False)
    monkeypatch.delenv("MARKET_OPENAI_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(openai_option_agent.httpx, "post", fake_post)

    result = openai_option_agent.generate_openai_option_postmortem(
        {
            "request": {
                "request_id": "req-post",
                "ticker": "RBLX",
                "strategy_version": "leap_10x_reversal_v1",
                "source_type": "missed_winner",
                "source_id": "missed-1",
            }
        }
    )

    assert result["proposed_parameter_changes"] == {"delta_min": 0.1, "require_rs_improving": True}
    assert result["evidence_refs"][0] == {"type": "agent_request", "id": "req-post"}


def test_openai_agent_prefers_api_key_over_ambient_access_token(monkeypatch) -> None:
    captured: dict = {}

    def fake_post(url, headers, json, timeout):
        captured["headers"] = headers
        return FakeResponse(
            {
                "output_text": jsonlib.dumps(
                    {
                        "ticker": "TSLA",
                        "bull_target_price": 650,
                        "bull_target_date": "2028-01-21",
                        "base_target_price": 520,
                        "core_thesis": "Autonomy and storage milestones re-rate the setup.",
                        "required_proofs": ["margins stabilize"],
                        "catalysts": [{"type": "earnings", "expected_window": "next 2 quarters", "what_to_watch": "margins"}],
                        "invalidation": ["stock breaks below $80 without recovery"],
                        "bear_case": "Demand softness can keep the stock below trend.",
                        "confidence": 55,
                        "evidence_refs": [],
                    }
                )
            }
        )

    monkeypatch.delenv("MARKET_OPENAI_AUTH_MODE", raising=False)
    monkeypatch.delenv("MARKET_OPENAI_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "ambient-token")
    monkeypatch.setenv("OPENAI_API_KEY", "configured-key")
    monkeypatch.setattr(openai_option_agent.httpx, "post", fake_post)

    openai_option_agent.generate_openai_option_thesis({"request": {"request_id": "req-1", "ticker": "TSLA"}})

    assert captured["headers"]["Authorization"] == "Bearer configured-key"


def test_openai_thesis_agent_can_use_codex_oauth_token(monkeypatch, tmp_path) -> None:
    captured: dict = {}
    token = _fake_jwt(
        {"aud": ["https://api.openai.com/v1"], "exp": int(time.time()) + 600, "scp": ["api.responses.write"]}
    )
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(jsonlib.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": token}}))

    def fake_post(url, headers, json, timeout):
        captured["headers"] = headers
        return FakeResponse(
            {
                "output_text": jsonlib.dumps(
                    {
                        "ticker": "TSLA",
                        "bull_target_price": 650,
                        "bull_target_date": "2028-01-21",
                        "base_target_price": 520,
                        "core_thesis": "Autonomy and storage milestones re-rate the setup.",
                        "required_proofs": ["margins stabilize"],
                        "catalysts": [{"type": "earnings", "expected_window": "next 2 quarters", "what_to_watch": "margins"}],
                        "invalidation": ["stock breaks below $80 without recovery"],
                        "bear_case": "Demand softness can keep the stock below trend.",
                        "confidence": 55,
                        "evidence_refs": [],
                    }
                )
            }
        )

    monkeypatch.setenv("MARKET_OPENAI_AUTH_MODE", "oauth")
    monkeypatch.setenv("MARKET_OPENAI_OAUTH_FILE", str(auth_path))
    monkeypatch.delenv("MARKET_OPENAI_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(openai_option_agent.httpx, "post", fake_post)

    result = openai_option_agent.generate_openai_option_thesis({"request": {"request_id": "req-1", "ticker": "TSLA"}})

    assert captured["headers"]["Authorization"] == f"Bearer {token}"
    assert result["ticker"] == "TSLA"


def test_codex_thesis_agent_uses_restricted_oauth_cli(monkeypatch, tmp_path) -> None:
    captured: dict = {}

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        captured["cmd"] = cmd
        captured["input"] = input
        captured["timeout"] = timeout
        captured["env"] = env
        output_path = cmd[cmd.index("-o") + 1]
        with open(output_path, "w") as handle:
            handle.write(
                jsonlib.dumps(
                    {
                        "ticker": "TSLA",
                        "bull_target_price": 650,
                        "bull_target_date": "2028-01-21",
                        "base_target_price": 520,
                        "core_thesis": "Autonomy and storage milestones re-rate the setup.",
                        "required_proofs": ["margins stabilize"],
                        "catalysts": [{"type": "earnings", "expected_window": "next 2 quarters", "what_to_watch": "margins"}],
                        "invalidation": ["stock breaks below $80 without recovery"],
                        "bear_case": "Demand softness can keep the stock below trend.",
                        "confidence": 55,
                        "evidence_refs": [],
                    }
                )
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="ignored stdout", stderr="")

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("OPENAI_ACCESS_TOKEN", "must-not-leak")
    monkeypatch.setenv("MARKET_OPENAI_ACCESS_TOKEN", "must-not-leak")
    monkeypatch.setenv("MARKET_CODEX_BIN", "codex-test")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("MARKET_CODEX_REASONING_EFFORT", "low")
    monkeypatch.delenv("MARKET_CODEX_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(openai_option_agent.subprocess, "run", fake_run)

    result = openai_option_agent.generate_codex_option_thesis({"request": {"request_id": "req-1", "ticker": "TSLA"}})

    assert captured["cmd"][:4] == ["codex-test", "-a", "never", "--disable"]
    for disabled in [
        "shell_tool",
        "apps",
        "browser_use",
        "browser_use_external",
        "in_app_browser",
        "computer_use",
        "multi_agent",
        "image_generation",
        "standalone_web_search",
        "plugins",
        "remote_plugin",
        "enable_mcp_apps",
    ]:
        assert disabled in captured["cmd"]
    assert "--ignore-user-config" in captured["cmd"]
    assert "--ignore-rules" in captured["cmd"]
    assert "--output-schema" in captured["cmd"]
    assert captured["timeout"] == 90.0
    assert "OPENAI_API_KEY" not in captured["env"]
    assert "OPENAI_ACCESS_TOKEN" not in captured["env"]
    assert "MARKET_OPENAI_ACCESS_TOKEN" not in captured["env"]
    assert captured["env"]["CODEX_HOME"] == str(tmp_path / "codex-home")
    assert jsonlib.loads(captured["input"])["request"]["ticker"] == "TSLA"
    assert result["evidence_refs"][0] == {"type": "agent_request", "id": "req-1"}


def test_openai_agent_requires_configured_credential(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("MARKET_OPENAI_AUTH_MODE", raising=False)
    monkeypatch.delenv("MARKET_OPENAI_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MARKET_OPENAI_OAUTH_FILE", str(tmp_path / "missing-auth.json"))

    with pytest.raises(openai_option_agent.OpenAIOptionAgentError, match="OPENAI_API_KEY or OpenAI OAuth"):
        openai_option_agent.generate_openai_option_thesis({"request": {"ticker": "TSLA"}})


def test_codex_oauth_file_rejects_wrong_audience_or_missing_write_scope(monkeypatch, tmp_path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        jsonlib.dumps(
            {
                "tokens": {
                    "access_token": _fake_jwt(
                        {"aud": ["https://other.example.test"], "exp": int(time.time()) + 600, "scp": ["api.responses.write"]}
                    )
                }
            }
        )
    )
    monkeypatch.setenv("MARKET_OPENAI_OAUTH_FILE", str(auth_path))
    assert openai_option_agent_auth.codex_oauth_access_token() == ""

    auth_path.write_text(
        jsonlib.dumps(
            {
                "tokens": {
                    "access_token": _fake_jwt(
                        {"aud": ["https://api.openai.com/v1"], "exp": int(time.time()) + 600, "scp": ["api.connectors.read"]}
                    )
                }
            }
        )
    )
    assert openai_option_agent_auth.codex_oauth_access_token() == ""


def test_codex_oauth_file_skips_malformed_access_token(monkeypatch, tmp_path) -> None:
    valid = _fake_jwt(
        {"aud": ["https://api.openai.com/v1"], "exp": int(time.time()) + 600, "scp": ["api.responses.write"]}
    )
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        jsonlib.dumps(
            {
                "tokens": {
                    "access_token": "not.a-valid-jwt",
                    "fallback": {"access_token": valid},
                }
            }
        )
    )
    monkeypatch.setenv("MARKET_OPENAI_OAUTH_FILE", str(auth_path))

    assert openai_option_agent_auth.codex_oauth_access_token() == valid


def _fake_jwt(payload: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}
    return ".".join(
        [
            _base64url_json(header),
            _base64url_json(payload),
            "signature",
        ]
    )


def _base64url_json(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(jsonlib.dumps(payload, separators=(",", ":")).encode()).decode()
    return encoded.rstrip("=")
