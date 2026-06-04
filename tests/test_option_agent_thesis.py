from __future__ import annotations

import pytest

from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.free_sources import store_options_chain
from investment_panel.core.option_agent_thesis import AgentThesisValidationError, refresh_option_agent_work, upsert_agent_thesis
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
    assert "Do not recommend or execute trades" in request["prompt"]


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
    assert validation["state"] == "validated"
    assert validation["candidate_state"] == "FIRE"
    assert validation["option_still_valid"] is True
    assert validation["proof_status"] == "supported"
    assert validation["catalyst_status"] == "scheduled"
    assert validation["invalidation_status"] == "clear"
    assert validation["evidence_status"] == "source_backed"


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
