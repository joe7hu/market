from __future__ import annotations

import json as jsonlib

import pytest

from investment_panel.jobs import openai_option_agent


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


def test_openai_agent_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(openai_option_agent.OpenAIOptionAgentError, match="OPENAI_API_KEY"):
        openai_option_agent.generate_openai_option_thesis({"request": {"ticker": "TSLA"}})
