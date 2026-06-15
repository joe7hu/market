"""Agent control plane: token/cost capture, on-demand requests, context toggles."""

from __future__ import annotations

from typing import Any

import pytest

from app.data_access.settings import _sanitize_option_agent_settings
from investment_panel.core import option_agent_runner as runner_mod
from investment_panel.core.db import db, init_db, json_dumps, query_rows
from investment_panel.core.option_agent_runner import estimate_agent_cost, run_consolidated_option_agents
from investment_panel.core.option_agent_thesis import build_ondemand_agent_request, build_ticker_agent_context
from investment_panel.core.options_radar import DEFAULT_STRATEGY_VERSION


def _seed_thesis(con: Any) -> None:
    con.execute(
        """
        INSERT INTO agent_thesis_request (request_id, created_at, ticker, strategy_version, priority_score, status, prompt, context, raw)
        VALUES (?, now(), ?, ?, ?, 'open', ?, ?, ?)
        """,
        ["req1", "NVDA", DEFAULT_STRATEGY_VERSION, 9.0, "p", json_dumps({}), json_dumps({})],
    )


def test_consolidated_run_records_tokens_and_cost(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "a.duckdb"
    init_db(db_path)

    def fake_invoke(command: str, payload: dict[str, Any], *, timeout_seconds: int, env=None) -> dict[str, Any]:
        return {
            "thesis": [{"ticker": "NVDA", "core_thesis": "x", "evidence_refs": []}],
            "postmortem": [],
            "_meta": {"provider": "openai", "model": "gpt-5.2", "estimated": False, "usage": {"input_tokens": 10000, "output_tokens": 2000}},
        }

    monkeypatch.setattr(runner_mod, "_invoke_agent_command", fake_invoke)
    monkeypatch.setattr(runner_mod, "upsert_agent_thesis", lambda con, output: None)
    monkeypatch.setattr(runner_mod, "_refresh_after_agent_theses", lambda con, *, strategy_version: {})

    pricing = {"gpt-5.2": {"input_per_1m": 1.25, "output_per_1m": 10.0}}
    with db(db_path, read_only=False) as con:
        _seed_thesis(con)
        run_consolidated_option_agents(con, command="fake", pricing=pricing, model="gpt-5.2", provider="openai")
        runs = query_rows(con, "SELECT * FROM agent_runs")

    assert len(runs) == 1
    row = runs[0]
    assert row["input_tokens"] == 10000 and row["output_tokens"] == 2000
    # 10000/1e6*1.25 + 2000/1e6*10 = 0.0125 + 0.02 = 0.0325
    assert abs(float(row["est_cost_usd"]) - 0.0325) < 1e-6
    assert row["provider"] == "openai" and row["model"] == "gpt-5.2"


def test_estimate_cost_falls_back_to_default_pricing() -> None:
    meta = {"model": "unknown-model", "usage": {"input_tokens": 1_000_000, "output_tokens": 0}}
    assert estimate_agent_cost(meta, {"default": {"input_per_1m": 2.0, "output_per_1m": 5.0}}) == 2.0


def test_ondemand_request_for_non_candidate_ticker_with_custom_prompt(tmp_path) -> None:
    db_path = tmp_path / "b.duckdb"
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        req = build_ondemand_agent_request(con, "abcd", strategy_version=DEFAULT_STRATEGY_VERSION, custom_prompt="focus on AI demand")
        rows = query_rows(con, "SELECT request_id, ticker, status, prompt FROM agent_thesis_request WHERE ticker = 'ABCD'")
    assert rows and rows[0]["status"] == "open"
    assert "focus on AI demand" in rows[0]["prompt"]
    assert req["event_id"].startswith("ondemand:ABCD:")


def test_context_sources_toggle_omits_disabled_sources(tmp_path) -> None:
    db_path = tmp_path / "c.duckdb"
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        ctx = build_ticker_agent_context(
            con, "NVDA", {"ticker": "NVDA", "contract_id": None, "raw": {}}, {"news": False, "fundamentals": True}
        )
    assert "news" not in ctx, "disabled source must be omitted"
    assert "fundamentals" in ctx
    assert "candidate_event" in ctx  # core context is always present


def test_option_agent_sanitizer_accepts_and_filters_new_fields() -> None:
    clean = _sanitize_option_agent_settings(
        {
            "provider": "openai",
            "model": "gpt-5.2",
            "reasoning_effort": "high",
            "auto_run_seconds": 3600,
            "max_runs_per_day": 4,
            "context_sources": {"news": False, "bogus": True},
        }
    )
    assert clean["provider"] == "openai" and clean["model"] == "gpt-5.2"
    assert clean["reasoning_effort"] == "high"
    assert clean["context_sources"] == {"news": False}  # unknown key filtered out


def test_option_agent_sanitizer_rejects_bad_provider() -> None:
    with pytest.raises(ValueError):
        _sanitize_option_agent_settings({"provider": "anthropic"})


def test_partial_context_sources_patch_merges_existing(tmp_path) -> None:
    from app.data_access.settings import update_agent_settings_config

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "agents:\n  option_agent:\n    context_sources:\n      news: true\n      fundamentals: true\n",
        encoding="utf-8",
    )
    update_agent_settings_config(cfg, {"option_agent": {"context_sources": {"news": False}}})
    import yaml

    sources = yaml.safe_load(cfg.read_text())["agents"]["option_agent"]["context_sources"]
    assert sources["news"] is False and sources["fundamentals"] is True  # merged, not replaced
