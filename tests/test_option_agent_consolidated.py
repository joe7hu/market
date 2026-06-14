"""Consolidated single-pass option agent runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from investment_panel.core.db import db, init_db, json_dumps, query_rows
from investment_panel.core import option_agent_runner as runner_mod
from investment_panel.core.option_agent_runner import run_consolidated_option_agents
from investment_panel.core.options_radar import DEFAULT_STRATEGY_VERSION


def _seed_requests(con: Any) -> None:
    con.execute(
        """
        INSERT INTO agent_thesis_request (request_id, created_at, ticker, strategy_version, priority_score, status, prompt, context, raw)
        VALUES (?, now(), ?, ?, ?, 'open', ?, ?, ?)
        """,
        ["req_thesis_1", "NVDA", DEFAULT_STRATEGY_VERSION, 9.0, "thesis prompt", json_dumps({}), json_dumps({})],
    )
    con.execute(
        """
        INSERT INTO agent_postmortem_request (request_id, created_at, source_type, source_id, ticker, strategy_version, priority_score, status, prompt, context, raw)
        VALUES (?, now(), 'loser', 'radar', ?, ?, ?, 'open', ?, ?, ?)
        """,
        ["req_pm_1", "TSLA", DEFAULT_STRATEGY_VERSION, 8.0, "postmortem prompt", json_dumps({}), json_dumps({})],
    )


def test_consolidated_runner_invokes_command_once_for_mixed_batch(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "agent.duckdb"
    init_db(db_path)

    invoke_calls: list[dict[str, Any]] = []
    thesis_upserts: list[dict[str, Any]] = []
    postmortem_upserts: list[dict[str, Any]] = []

    def fake_invoke(command: str, payload: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
        invoke_calls.append(payload)
        return {
            "thesis": [{"ticker": "NVDA", "core_thesis": "x", "evidence_refs": []}],
            "postmortem": [{"ticker": "TSLA", "outcome_type": "loser", "evidence_refs": []}],
        }

    monkeypatch.setattr(runner_mod, "_invoke_agent_command", fake_invoke)
    monkeypatch.setattr(runner_mod, "upsert_agent_thesis", lambda con, output: thesis_upserts.append(output))
    monkeypatch.setattr(runner_mod, "upsert_agent_postmortem", lambda con, output: postmortem_upserts.append(output))
    # Keep the post-dispatch refresh cheap/no-op so the test stays focused.
    monkeypatch.setattr(runner_mod, "_refresh_after_agent_theses", lambda con, *, strategy_version: {})
    monkeypatch.setattr(runner_mod, "_refresh_after_agent_postmortems", lambda con, *, strategy_version: {})

    with db(db_path, read_only=False) as con:
        _seed_requests(con)
        result = run_consolidated_option_agents(con, command="fake-agent", limit_thesis=8, limit_postmortem=4)

    assert len(invoke_calls) == 1, "consolidated runner must invoke the command exactly once"
    payload = invoke_calls[0]
    assert len(payload["thesis"]) == 1
    assert len(payload["postmortem"]) == 1
    assert "output_schemas" in payload and "guardrails" in payload
    assert len(thesis_upserts) == 1
    assert len(postmortem_upserts) == 1
    assert result["accepted"] == 2
    assert result["attempted"] == 2


def test_consolidated_runner_noops_when_no_open_requests(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "agent_empty.duckdb"
    init_db(db_path)
    calls: list[Any] = []
    monkeypatch.setattr(runner_mod, "_invoke_agent_command", lambda *a, **k: calls.append(a))
    with db(db_path, read_only=False) as con:
        result = run_consolidated_option_agents(con, command="fake-agent")
    assert calls == []
    assert result["attempted"] == 0
    assert result.get("skipped_reason") == "no_open_requests"


def test_consolidated_runner_disabled_without_command(tmp_path: Path) -> None:
    db_path = tmp_path / "agent_off.duckdb"
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        result = run_consolidated_option_agents(con, command="")
    assert result["enabled"] is False
