from __future__ import annotations

import json

import pytest

from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.free_sources import store_options_chain
from investment_panel.core.option_agent_postmortem import AgentPostmortemValidationError, refresh_agent_postmortem_requests, upsert_agent_postmortem
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


def test_shadow_big_winner_opens_agent_postmortem_request(tmp_path) -> None:
    db_path = tmp_path / "postmortem-shadow-winner.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_shadow_outcome(con, latest_bid=24.0, latest_ask=26.0)

        result = refresh_options_radar(con, ["TSLA"])
        request = query_rows(con, "SELECT * FROM agent_postmortem_request WHERE source_type = 'shadow_big_winner_5x'")[0]
        context = json.loads(request["context"])

    assert result["agent_postmortem_requests"] == 1
    assert request["ticker"] == "TSLA"
    assert request["strategy_version"] == DEFAULT_STRATEGY_VERSION
    assert request["priority_score"] >= 4.0
    assert context["candidate_event"]["state"] == "FIRE"
    assert context["latest_attribution"]["label"] == "good_convexity"


def test_shadow_big_loser_opens_agent_postmortem_request(tmp_path) -> None:
    db_path = tmp_path / "postmortem-shadow-loser.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_shadow_outcome(con, latest_bid=1.4, latest_ask=1.6)

        result = refresh_options_radar(con, ["TSLA"])
        request = query_rows(con, "SELECT * FROM agent_postmortem_request WHERE source_type = 'shadow_big_loser'")[0]
        context = json.loads(request["context"])

    assert result["agent_postmortem_requests"] == 1
    assert request["ticker"] == "TSLA"
    assert request["priority_score"] > 0.40
    assert context["candidate_event"]["state"] == "FIRE"
    assert context["latest_attribution"]["option_return"] < -0.40


def test_thesis_postmortem_requests_are_strategy_scoped_and_exact_context(tmp_path) -> None:
    db_path = tmp_path / "postmortem-thesis-scope.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        insert_thesis_validation(con, "validation-other", "leap_10x_reversal_v2", "invalidated", "2026-06-05T20:00:00Z")
        insert_thesis_validation(con, "validation-default", DEFAULT_STRATEGY_VERSION, "invalidated", "2026-06-04T20:00:00Z")

        created = refresh_agent_postmortem_requests(con, strategy_version=DEFAULT_STRATEGY_VERSION, limit=10)
        requests = query_rows(con, "SELECT * FROM agent_postmortem_request")
        context = json.loads(requests[0]["context"])

    assert created == 1
    assert len(requests) == 1
    assert requests[0]["source_type"] == "thesis_invalidated"
    assert requests[0]["source_id"] == "validation-default"
    assert requests[0]["strategy_version"] == DEFAULT_STRATEGY_VERSION
    assert context["latest_thesis_validation"]["validation_id"] == "validation-default"
    assert context["latest_thesis_validation"]["strategy_version"] == DEFAULT_STRATEGY_VERSION


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


def seed_shadow_outcome(con, *, latest_bid: float, latest_ask: float) -> None:
    seed_prices(con, "TSLA", start_price=100, slope=0.12)
    seed_prices(con, "QQQ", start_price=100, slope=0.02)
    con.execute(
        "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-02T20:00:00Z', 102, 1, 1, 'USD', 'tradingview', '{}')"
    )
    con.execute(
        "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-20T20:00:00Z', 120, 1, 1, 'USD', 'tradingview', '{}')"
    )
    store_options_chain(
        con,
        "TSLA",
        "2026-06-02T20:00:00Z",
        [option_row("2027-09-18", 120, "call", 4.3, 4.7, 0.25, 0.30, "OPRA:TSLA270918C120", volume=25, open_interest=250)],
    )
    store_options_chain(
        con,
        "TSLA",
        "2026-06-20T20:00:00Z",
        [option_row("2027-09-18", 120, "call", latest_bid, latest_ask, 0.30, 0.30, "OPRA:TSLA270918C120", volume=40, open_interest=275)],
    )


def insert_thesis_validation(con, validation_id: str, strategy_version: str, state: str, validated_at: str) -> None:
    con.execute(
        """
        INSERT INTO agent_thesis_validation
        (validation_id, thesis_id, ticker, strategy_version, validation_date,
         candidate_event_id, candidate_snapshot_time, validated_at, state, reason,
         option_still_valid, stock_progress, iv_status, candidate_state,
         proof_status, catalyst_status, invalidation_status, evidence_status,
         red_team_status, red_team_flags, evidence_refs, raw)
        VALUES (?, 'thesis-tsla', 'TSLA', ?, DATE '2026-06-04',
                NULL, NULL, ?, ?, 'validation state changed',
                true, 'tracking', 'neutral', 'FIRE',
                'unknown', 'unknown', 'triggered', 'source_backed',
                'source_backed', '[]', '[]', ?)
        """,
        [validation_id, strategy_version, validated_at, state, json.dumps({"validation_id": validation_id})],
    )
