from __future__ import annotations

import shlex
import sys

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.option_agent_runner import (
    run_external_agent_postmortem_requests,
    run_external_agent_thesis_requests,
)
from investment_panel.core.options_radar import DEFAULT_STRATEGY_VERSION, refresh_options_radar
from tests.test_option_agent_postmortem import seed_missed_winner
from tests.test_option_agent_thesis import seed_fire_candidate


def test_agent_command_env_override_enables_default_disabled_config(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
agents:
  option_thesis:
    enabled: false
    command: ""
  option_postmortem:
    enabled: false
    command: ""
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MARKET_OPTION_THESIS_AGENT_COMMAND", "fake-thesis-agent")
    monkeypatch.setenv("MARKET_OPTION_POSTMORTEM_AGENT_COMMAND", "fake-postmortem-agent")

    config = load_config(config_path)

    assert config.agents.option_thesis.enabled is True
    assert config.agents.option_thesis.command == "fake-thesis-agent"
    assert config.agents.option_postmortem.enabled is True
    assert config.agents.option_postmortem.command == "fake-postmortem-agent"


def test_external_agent_thesis_runner_fulfills_open_requests(tmp_path) -> None:
    script = tmp_path / "fake_thesis_agent.py"
    script.write_text(
        """
import json
import sys

request = json.load(sys.stdin)["request"]
print(json.dumps({
    "ticker": request["ticker"],
    "bull_target_price": 180,
    "bull_target_date": "2028-01-21",
    "base_target_price": 120,
    "core_thesis": "Energy storage and autonomy narrative returns while margins stabilize.",
    "required_proofs": ["gross margin stabilizes"],
    "catalysts": [{"type": "earnings", "expected_window": "next 2 quarters", "what_to_watch": "margins"}],
    "invalidation": ["stock breaks below $80 without recovery"],
    "bear_case": "Demand weakness can keep the stock below trend.",
    "confidence": 61,
    "evidence_refs": [{"type": "agent_context", "id": request["request_id"]}]
}))
""",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    db_path = tmp_path / "agent-runner-thesis.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_fire_candidate(con)
        refresh_options_radar(con, ["TSLA"])

        result = run_external_agent_thesis_requests(
            con,
            strategy_version=DEFAULT_STRATEGY_VERSION,
            command=command,
            limit=5,
            timeout_seconds=10,
        )
        thesis_count = query_rows(con, "SELECT count(*) AS count FROM agent_thesis")[0]["count"]
        request = query_rows(con, "SELECT status FROM agent_thesis_request WHERE ticker = 'TSLA'")[0]
        validation = query_rows(con, "SELECT ticker, strategy_version, candidate_event_id FROM agent_thesis_validation")[0]

    assert result["attempted"] == 1
    assert result["accepted"] == 1
    assert result["failed"] == 0
    assert result["agent_work"]["agent_thesis_validations"] == 1
    assert thesis_count == 1
    assert request["status"] == "fulfilled"
    assert validation["ticker"] == "TSLA"
    assert validation["strategy_version"] == DEFAULT_STRATEGY_VERSION
    assert validation["candidate_event_id"]


def test_external_agent_postmortem_runner_creates_gated_strategy_proposal(tmp_path) -> None:
    script = tmp_path / "fake_postmortem_agent.py"
    script.write_text(
        """
import json
import sys

request = json.load(sys.stdin)["request"]
print(json.dumps({
    "ticker": request["ticker"],
    "strategy_version": request["strategy_version"],
    "source_type": request["source_type"],
    "source_id": request["source_id"],
    "outcome_type": "missed_10x_winner",
    "failure_type": "delta_range_too_strict",
    "evidence": ["Contract was filtered by delta before reaching a 10x mark."],
    "proposed_rule_change": "Test a lower-delta sleeve for strong momentum reversals.",
    "proposed_parameter_changes": {"delta_min": 0.10, "candidate_note": "agent runner lower-delta sleeve"},
    "expected_effect": "Increase recall for lower-delta 10x winners.",
    "risk": "May increase false positives and earlier entries.",
    "confidence": 70,
    "evidence_refs": [{"type": request["source_type"], "id": request["source_id"]}]
}))
""",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    db_path = tmp_path / "agent-runner-postmortem.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_missed_winner(con)
        refresh_options_radar(con, ["RBLX"])

        result = run_external_agent_postmortem_requests(
            con,
            strategy_version=DEFAULT_STRATEGY_VERSION,
            command=command,
            limit=5,
            timeout_seconds=10,
        )
        request = query_rows(con, "SELECT status FROM agent_postmortem_request WHERE ticker = 'RBLX'")[0]
        proposal = query_rows(
            con,
            """
            SELECT status, requires_backtest, requires_forward_test, human_approval_status
            FROM strategy_mutation_proposal
            WHERE source_type = 'agent_postmortem'
            """,
        )[0]
        backtests = query_rows(con, "SELECT count(*) AS count FROM strategy_backtest_result")[0]["count"]
        forward_tests = query_rows(con, "SELECT count(*) AS count FROM strategy_forward_test_result")[0]["count"]

    assert result["attempted"] == 1
    assert result["accepted"] == 1
    assert result["failed"] == 0
    assert result["postmortem_work"]["agent_postmortem_strategy_proposals"] >= 0
    assert request["status"] == "fulfilled"
    assert proposal["requires_backtest"] is True
    assert proposal["requires_forward_test"] is True
    assert proposal["human_approval_status"] == "required"
    assert proposal["status"] in {"forward_test_required", "backtest_failed", "ready_for_human_review"}
    assert backtests >= 1
    assert forward_tests >= 1
