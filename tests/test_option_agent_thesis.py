from __future__ import annotations

import json

import pytest

from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.db import upsert_instrument
from investment_panel.core.free_sources import store_options_chain
from investment_panel.core.option_agent_thesis import AgentThesisValidationError, refresh_agent_thesis_requests, refresh_option_agent_work, upsert_agent_thesis
from investment_panel.core.options_radar import refresh_options_radar
from tests.test_options_radar import option_row, seed_prices


def test_options_radar_opens_agent_thesis_request_for_top_candidate(tmp_path) -> None:
    db_path = tmp_path / "agent-request.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_fire_candidate(con)

        result = refresh_options_radar(con, ["TSLA"])
        request = query_rows(con, "SELECT * FROM agent_thesis_request")[0]

    assert result["agent_thesis_requests"] == 1
    assert request["ticker"] == "TSLA"
    assert request["status"] == "open"
    assert "Return JSON only" in request["prompt"]
    assert "product-and-technology grounded" in request["prompt"]
    assert "12-24 month prediction" in request["prompt"]
    assert "do not use price action or option Greeks as proof" in request["prompt"]
    assert "Do not recommend or execute trades" in request["prompt"]


def test_agent_thesis_request_includes_business_and_fundamental_context(tmp_path) -> None:
    db_path = tmp_path / "agent-request-context.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        upsert_instrument(
            con,
            {
                "symbol": "TSLA",
                "name": "Tesla",
                "asset_class": "equity",
                "sector": "Consumer Cyclical",
                "industry": "Auto Manufacturers",
                "category": "physical AI robotics",
            },
        )
        seed_fire_candidate(con)
        con.execute(
            """
            INSERT INTO equity_fundamentals
            VALUES ('TSLA', '2026-03-31', '2026-05-01', '10-Q', ?, 'https://example.com/tsla')
            """,
            [json.dumps({"revenue_growth": 0.08, "gross_margin": 0.19})],
        )

        refresh_options_radar(con, ["TSLA"])
        request = query_rows(con, "SELECT context FROM agent_thesis_request WHERE ticker = 'TSLA'")[0]

    context = json.loads(request["context"])
    assert context["instrument"]["category"] == "physical AI robotics"
    assert context["fundamentals"]["form_type"] == "10-Q"
    assert context["fundamentals"]["metrics"]["gross_margin"] == 0.19


def test_agent_thesis_queue_keeps_only_current_top_ranked_candidates_open(tmp_path) -> None:
    db_path = tmp_path / "agent-request-cap.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        for ticker, state, score in [
            ("AAA", "FIRE", 80),
            ("BBB", "SETUP", 99),
            ("CCC", "SETUP", 98),
        ]:
            con.execute(
                """
                INSERT INTO candidate_event
                (event_id, snapshot_time, ticker, contract_id, strategy_version,
                 state, premium_mid, premium_fill_assumption, required_10x_price,
                 required_move_pct, buy_under, trigger_reason, thesis_id, score,
                 quality_status, quality_flags, raw)
                VALUES (?, '2026-06-02T20:00:00Z', ?, ?, 'leap_10x_reversal_v1',
                        ?, 1, 1.03, 100, 1.0, 10, 'test', NULL, ?, 'ok', '[]', '{}')
                """,
                [f"event-{ticker}", ticker, f"OPRA:{ticker}", state, score],
            )
        con.execute(
            """
            INSERT INTO agent_thesis_request
            (request_id, created_at, ticker, event_id, strategy_version,
             priority_score, status, prompt, context, raw)
            VALUES ('stale-ccc', '2026-06-01T20:00:00Z', 'CCC', 'event-CCC',
                    'leap_10x_reversal_v1', 98, 'open', 'old', '{}', '{}')
            """
        )

        result = refresh_agent_thesis_requests(con, strategy_version="leap_10x_reversal_v1", limit=2)
        requests = query_rows(
            con,
            """
            SELECT ticker, event_id, status
            FROM agent_thesis_request
            ORDER BY ticker
            """,
        )

    assert result == {"requested": 2, "superseded": 1}
    assert {row["ticker"] for row in requests if row["status"] == "open"} == {"AAA", "BBB"}
    assert [row for row in requests if row["ticker"] == "CCC"][0]["status"] == "superseded"


def test_agent_thesis_upsert_attaches_to_candidates_and_validates(tmp_path) -> None:
    db_path = tmp_path / "agent-validation.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_fire_candidate(con)
        refresh_options_radar(con, ["TSLA"])
        con.execute(
            """
            INSERT INTO ticker_source_signals
            (id, source_item_id, source_id, symbol, observed_at, signal_type,
             sentiment, direction, confidence, thesis, antithesis, catalysts,
             risks, invalidation, evidence_refs, needs_market_context, raw)
            VALUES (
             'sig-tsla-proof', 'source-tsla-proof', 'test_research', 'TSLA',
             '2026-06-03T12:00:00Z', 'earnings', 'positive', 'bullish', 0.9,
             'gross margin stabilizes while deliveries recover into the next report',
             'pricing pressure remains the bear case',
             '[{"type":"earnings","what_to_watch":"margins and delivery guide"}]',
             '["pricing pressure"]',
             'stock breaks below $80 without recovery',
             '[{"type":"source_item","id":"source-tsla-proof"}]',
             true,
             '{}'
            )
            """
        )
        con.execute(
            """
            INSERT INTO catalysts
            (id, symbol, event_date, event, expected_impact, source, verification_status, raw)
            VALUES ('cat-tsla-earnings', 'TSLA', '2026-06-15', 'earnings', 'high', 'test', 'confirmed', '{}')
            """
        )

        thesis_id = upsert_agent_thesis(
            con,
            {
                "ticker": "TSLA",
                "created_at": "2026-06-03T12:00:00Z",
                "bull_target_price": 180,
                "bull_target_date": "2028-01-21",
                "base_target_price": 95,
                "core_thesis": "Energy storage and autonomy narrative returns while margins stabilize.",
                "required_proofs": ["gross margin stabilizes", "deliveries recover"],
                "catalysts": [{"type": "earnings", "what_to_watch": "margins and delivery guide"}],
                "invalidation": ["stock breaks below $80 without recovery"],
                "bear_case": "Demand weakness and pricing pressure can keep the stock below trend.",
                "confidence": 72,
                "evidence_refs": [{"type": "source_signal", "id": "s1"}],
            },
        )
        result = refresh_option_agent_work(con, strategy_version="leap_10x_reversal_v1")
        attached = query_rows(con, "SELECT thesis_id FROM candidate_event WHERE ticker = 'TSLA' LIMIT 1")[0]
        request = query_rows(con, "SELECT status FROM agent_thesis_request WHERE ticker = 'TSLA'")[0]
        validation = query_rows(con, "SELECT * FROM agent_thesis_validation WHERE ticker = 'TSLA'")[0]

    assert result["agent_theses_attached"] >= 1
    assert attached["thesis_id"] == thesis_id
    assert request["status"] == "fulfilled"
    assert validation["thesis_id"] == thesis_id
    assert validation["strategy_version"] == "leap_10x_reversal_v1"
    assert str(validation["validation_date"]) == "2026-06-02"
    assert validation["candidate_event_id"]
    assert str(validation["candidate_snapshot_time"]).startswith("2026-06-02")
    assert validation["state"] == "validated"
    assert validation["candidate_state"] == "FIRE"
    assert validation["option_still_valid"] is True
    assert validation["proof_status"] == "supported"
    assert validation["catalyst_status"] == "scheduled"
    assert validation["invalidation_status"] == "clear"
    assert validation["evidence_status"] == "source_backed"
    assert validation["red_team_status"] == "source_backed"


def test_agent_thesis_normalizes_probability_confidence(tmp_path) -> None:
    db_path = tmp_path / "agent-confidence.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        thesis_id = upsert_agent_thesis(
            con,
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
                "confidence": 0.62,
                "evidence_refs": [{"type": "agent_request", "id": "req-1"}],
            },
        )
        low_score_id = upsert_agent_thesis(
            con,
            {
                "ticker": "TSLA",
                "bull_target_price": 650,
                "bull_target_date": "2028-01-21",
                "base_target_price": 520,
                "core_thesis": "A low-confidence 0-100 score stays low.",
                "required_proofs": ["margins stabilize"],
                "catalysts": [{"type": "earnings", "expected_window": "next 2 quarters", "what_to_watch": "margins"}],
                "invalidation": ["stock breaks below $80 without recovery"],
                "bear_case": "Demand softness can keep the stock below trend.",
                "confidence": 1,
                "evidence_refs": [{"type": "agent_request", "id": "req-2"}],
            },
        )
        row = query_rows(con, "SELECT confidence FROM agent_thesis WHERE thesis_id = ?", [thesis_id])[0]
        low_score = query_rows(con, "SELECT confidence FROM agent_thesis WHERE thesis_id = ?", [low_score_id])[0]

    assert row["confidence"] == 62
    assert low_score["confidence"] == 1


def test_agent_thesis_validations_are_strategy_scoped_daily_rows(tmp_path) -> None:
    db_path = tmp_path / "agent-validation-strategy.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_fire_candidate(con)
        refresh_options_radar(con, ["TSLA"])
        thesis_id = upsert_agent_thesis(
            con,
            {
                "ticker": "TSLA",
                "created_at": "2026-06-03T12:00:00Z",
                "bull_target_price": 180,
                "bull_target_date": "2028-01-21",
                "base_target_price": 120,
                "core_thesis": "Energy storage and autonomy narrative returns while margins stabilize.",
                "required_proofs": ["gross margin stabilizes"],
                "catalysts": [{"type": "earnings", "what_to_watch": "margins"}],
                "invalidation": ["stock breaks below $80 without recovery"],
                "bear_case": "Demand weakness can keep the stock below trend.",
                "confidence": 60,
                "evidence_refs": [{"type": "source_signal", "id": "agent-strategy-scope"}],
            },
        )

        first = refresh_option_agent_work(con, strategy_version="leap_10x_reversal_v1")
        second = refresh_option_agent_work(con, strategy_version="leap_10x_reversal_v1")
        refresh_options_radar(con, ["TSLA"], strategy_version="leap_10x_reversal_v2")
        validations = query_rows(
            con,
            """
            SELECT thesis_id, strategy_version, validation_date,
                   candidate_event_id, candidate_snapshot_time
            FROM agent_thesis_validation
            ORDER BY strategy_version
            """,
        )

    assert first["agent_thesis_validations"] == 1
    assert second["agent_thesis_validations"] == 1
    assert len(validations) == 2
    assert {row["strategy_version"] for row in validations} == {"leap_10x_reversal_v1", "leap_10x_reversal_v2"}
    assert all(row["thesis_id"] == thesis_id for row in validations)
    assert all(str(row["validation_date"]) == "2026-06-02" for row in validations)
    assert all(row["candidate_event_id"] for row in validations)
    assert all(str(row["candidate_snapshot_time"]).startswith("2026-06-02") for row in validations)


def test_agent_thesis_requires_structured_hypothesis_fields(tmp_path) -> None:
    db_path = tmp_path / "agent-invalid.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        with pytest.raises(AgentThesisValidationError, match="core_thesis"):
            upsert_agent_thesis(
                con,
                {
                    "ticker": "TSLA",
                    "bull_target_price": 180,
                    "bull_target_date": "2028-01-21",
                    "base_target_price": 95,
                    "required_proofs": ["deliveries recover"],
                    "catalysts": [{"type": "earnings"}],
                    "invalidation": ["stock breaks below $80"],
                    "bear_case": "Demand weakens.",
                },
            )


def test_agent_thesis_red_team_flags_hard_fundamental_risks(tmp_path) -> None:
    db_path = tmp_path / "agent-red-team.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_fire_candidate(con)
        refresh_options_radar(con, ["TSLA"])
        con.execute(
            """
            INSERT INTO equity_fundamentals
            VALUES ('TSLA', '2026-03-31', '2026-05-01', '10-Q', ?, 'https://example.com/tsla')
            """,
            [
                json.dumps(
                    {
                        "free_cash_flow": -200000000,
                        "operating_cash_flow": -100000000,
                        "cash": 50000000,
                        "total_debt": 500000000,
                        "assets": 1000000000,
                        "liabilities": 800000000,
                        "revenue_growth": -0.12,
                    }
                ),
            ],
        )
        upsert_agent_thesis(
            con,
            {
                "ticker": "TSLA",
                "created_at": "2026-06-03T12:00:00Z",
                "bull_target_price": 180,
                "bull_target_date": "2028-01-21",
                "base_target_price": 95,
                "core_thesis": "Energy storage and autonomy narrative returns while margins stabilize.",
                "required_proofs": ["gross margin stabilizes"],
                "catalysts": [{"type": "earnings", "what_to_watch": "margins"}],
                "invalidation": ["stock breaks below $80 without recovery"],
                "bear_case": "Cash burn, debt load, and negative growth pressure can overwhelm the rebound thesis.",
                "confidence": 55,
                "evidence_refs": [{"type": "source_signal", "id": "agent-red-team"}],
            },
        )
        result = refresh_option_agent_work(con, strategy_version="leap_10x_reversal_v1")
        validation = query_rows(con, "SELECT red_team_status, red_team_flags, raw FROM agent_thesis_validation WHERE ticker = 'TSLA'")[0]

    assert result["agent_thesis_validations"] == 1
    assert validation["red_team_status"] == "hard_risk_triggered"
    assert "cash_burn_risk" in str(validation["red_team_flags"])
    assert "balance_sheet_risk" in str(validation["red_team_flags"])
    assert "growth_deceleration_risk" in str(validation["raw"])


def seed_fire_candidate(con) -> None:
    seed_prices(con, "TSLA", slope=0.12)
    seed_prices(con, "QQQ", start_price=100, slope=0.02)
    con.execute(
        "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-02T20:00:00Z', 102, 1, 1, 'USD', 'tradingview', '{}')"
    )
    store_options_chain(
        con,
        "TSLA",
        "2026-06-02T20:00:00Z",
        [
            option_row("2027-09-18", 120, "call", 4.3, 4.7, 0.25, 0.30, "OPRA:TSLA270918C120", volume=25, open_interest=250),
            option_row("2027-09-18", 180, "call", 7.5, 8.5, 0.50, 0.30, "OPRA:TSLA270918C180", volume=25, open_interest=250),
        ],
    )
