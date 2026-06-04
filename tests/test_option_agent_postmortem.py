from __future__ import annotations

import pytest

from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.free_sources import store_options_chain
from investment_panel.core.option_agent_postmortem import AgentPostmortemValidationError, upsert_agent_postmortem
from investment_panel.core.options_radar import DEFAULT_STRATEGY_VERSION, refresh_options_radar
from tests.test_options_radar import option_row, seed_prices


def test_missed_winner_opens_agent_postmortem_request(tmp_path) -> None:
    db_path = tmp_path / "postmortem-request.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_missed_winner(con)

        result = refresh_options_radar(con, ["RBLX"])
        request = query_rows(con, "SELECT * FROM agent_postmortem_request")[0]

    assert result["agent_postmortem_requests"] == 1
    assert request["ticker"] == "RBLX"
    assert request["source_type"] == "missed_winner"
    assert request["status"] == "open"
    assert "Return JSON only" in request["prompt"]
    assert "do not recommend, pick, execute, or promote trades" in request["prompt"]


def test_agent_postmortem_creates_gated_strategy_mutation_proposal(tmp_path) -> None:
    db_path = tmp_path / "postmortem-proposal.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_missed_winner(con)
        refresh_options_radar(con, ["RBLX"])
        request = query_rows(con, "SELECT * FROM agent_postmortem_request")[0]

        postmortem_id = upsert_agent_postmortem(
            con,
            {
                "request_id": request["request_id"],
                "ticker": "RBLX",
                "strategy_version": DEFAULT_STRATEGY_VERSION,
                "source_type": request["source_type"],
                "source_id": request["source_id"],
                "outcome_type": "missed_10x_winner",
                "failure_type": "delta_range_too_strict",
                "evidence": ["Contract was rejected for delta_outside_strategy_range before reaching 10x."],
                "proposed_rule_change": "Test a lower-delta sleeve for strong momentum reversals.",
                "proposed_parameter_changes": {"delta_min": 0.10, "candidate_note": "agent postmortem lower-delta sleeve"},
                "expected_effect": "Increase recall for lower-delta 10x winners.",
                "risk": "May increase false positives and earlier entries.",
                "confidence": 70,
                "evidence_refs": [{"type": "missed_winner_event", "id": request["source_id"]}],
            },
        )
        refresh_options_radar(con, ["RBLX"])
        stored = query_rows(con, "SELECT * FROM agent_postmortem WHERE postmortem_id = ?", [postmortem_id])[0]
        request_status = query_rows(con, "SELECT status FROM agent_postmortem_request WHERE request_id = ?", [request["request_id"]])[0]
        proposal = query_rows(con, "SELECT * FROM strategy_mutation_proposal WHERE source_type = 'agent_postmortem'")[0]
        backtest = query_rows(con, "SELECT * FROM strategy_backtest_result WHERE proposal_id = ?", [proposal["proposal_id"]])
        forward = query_rows(con, "SELECT * FROM strategy_forward_test_result WHERE proposal_id = ?", [proposal["proposal_id"]])

    assert stored["failure_type"] == "delta_range_too_strict"
    assert request_status["status"] == "fulfilled"
    assert proposal["requires_backtest"] is True
    assert proposal["requires_forward_test"] is True
    assert proposal["human_approval_status"] == "required"
    assert proposal["status"] in {"forward_test_required", "backtest_failed", "ready_for_human_review"}
    assert "lower-delta" in proposal["rationale"]
    assert backtest
    assert forward


def test_agent_postmortem_requires_structured_fields(tmp_path) -> None:
    db_path = tmp_path / "postmortem-invalid.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        with pytest.raises(AgentPostmortemValidationError, match="failure_type"):
            upsert_agent_postmortem(
                con,
                {
                    "ticker": "RBLX",
                    "strategy_version": DEFAULT_STRATEGY_VERSION,
                    "source_type": "missed_winner",
                    "source_id": "m1",
                    "outcome_type": "missed_10x_winner",
                    "evidence": ["The system missed a winner."],
                    "proposed_rule_change": "Test a narrower gate.",
                    "expected_effect": "Improve recall.",
                    "risk": "More false positives.",
                },
            )


def seed_missed_winner(con) -> None:
    seed_prices(con, "RBLX", start_price=75, slope=0.11)
    seed_prices(con, "QQQ", start_price=100, slope=0.02)
    con.execute(
        "INSERT INTO quotes_intraday VALUES ('RBLX', '2026-06-02T20:00:00Z', 100, 1, 1, 'USD', 'tradingview', '{}')"
    )
    con.execute(
        "INSERT INTO quotes_intraday VALUES ('RBLX', '2026-06-20T20:00:00Z', 160, 1, 1, 'USD', 'tradingview', '{}')"
    )
    store_options_chain(
        con,
        "RBLX",
        "2026-06-02T20:00:00Z",
        [option_row("2027-09-18", 120, "call", 4.3, 4.7, 0.25, 0.12, "OPRA:RBLX270918C120", volume=25, open_interest=250)],
    )
    store_options_chain(
        con,
        "RBLX",
        "2026-06-20T20:00:00Z",
        [option_row("2027-09-18", 120, "call", 49.0, 51.0, 0.30, 0.18, "OPRA:RBLX270918C120", volume=80, open_interest=450)],
    )
