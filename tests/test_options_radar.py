from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.free_sources import store_options_chain, store_yfinance_options_liquidity
from investment_panel.core.option_agent_thesis import upsert_agent_thesis
from investment_panel.core.options_radar import (
    DEFAULT_STRATEGY_VERSION,
    StrategyPromotionError,
    promote_strategy_mutation,
    refresh_options_radar,
)
from investment_panel.core.panel import load_panel_data


def test_options_radar_persists_fire_candidate_and_shadow_trade(tmp_path) -> None:
    db_path = tmp_path / "radar.duckdb"
    init_db(db_path)
    with db(db_path) as con:
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

        result = refresh_options_radar(con, ["TSLA"])
        strategy = query_rows(con, "SELECT strategy_version, status FROM option_strategy_versions")
        snapshots = query_rows(con, "SELECT contract_id, open_interest, volume FROM option_snapshot ORDER BY strike")
        feature = query_rows(con, "SELECT required_10x_price, required_move_10x_pct, iv_percentile FROM option_features WHERE contract_id = 'OPRA:TSLA270918C120'")[0]
        fire = query_rows(con, "SELECT * FROM candidate_event WHERE contract_id = 'OPRA:TSLA270918C120'")[0]
        rejects = query_rows(con, "SELECT state, trigger_reason FROM candidate_event WHERE contract_id = 'OPRA:TSLA270918C180'")[0]
        candidate_marks = query_rows(con, "SELECT contract_id, candidate_state, current_return FROM candidate_event_mark ORDER BY contract_id")
        trades = query_rows(con, "SELECT * FROM shadow_trade")
        transitions = query_rows(con, "SELECT contract_id, state, candidate_state FROM radar_state_transition ORDER BY contract_id")

    assert result["option_snapshots"] == 2
    assert result["candidate_events"] == 2
    assert result["candidate_event_marks"] == 2
    assert result["candidate_event_attributions"] == 0
    assert result["radar_state_transitions"] == 2
    assert strategy == [{"strategy_version": DEFAULT_STRATEGY_VERSION, "status": "shadow"}]
    assert snapshots[0]["open_interest"] == 250
    assert snapshots[0]["volume"] == 25
    assert round(feature["required_10x_price"], 2) == 165.0
    assert round(feature["required_move_10x_pct"], 3) == 0.618
    assert feature["iv_percentile"] == 50.0
    assert fire["state"] == "FIRE"
    assert fire["strategy_version"] == DEFAULT_STRATEGY_VERSION
    assert fire["buy_under"] > fire["premium_mid"]
    assert "10x_math_inside_cap" in fire["trigger_reason"]
    assert rejects["state"] == "REJECT"
    assert "iv_percentile_reject" in rejects["trigger_reason"]
    assert [row["candidate_state"] for row in candidate_marks] == ["FIRE", "REJECT"]
    assert all(row["current_return"] < 0 for row in candidate_marks)
    assert len(trades) == 1
    assert trades[0]["event_id"] == fire["event_id"]
    assert trades[0]["status"] == "open"
    assert transitions == [
        {"contract_id": "OPRA:TSLA270918C120", "state": "FIRE", "candidate_state": "FIRE"},
        {"contract_id": "OPRA:TSLA270918C180", "state": "REJECT", "candidate_state": "REJECT"},
    ]


def test_options_radar_preserves_missing_liquidity_candidate_without_trade(tmp_path) -> None:
    db_path = tmp_path / "radar-watch.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "RBLX", start_price=75, slope=0.11)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('RBLX', '2026-06-02T20:00:00Z', 100, 1, 1, 'USD', 'tradingview', '{}')"
        )
        store_options_chain(
            con,
            "RBLX",
            "2026-06-02T20:00:00Z",
            [
                option_row("2027-09-18", 120, "call", 4.3, 4.7, 0.25, 0.30, "OPRA:RBLX270918C120"),
                option_row("2027-09-18", 180, "call", 7.5, 8.5, 0.50, 0.30, "OPRA:RBLX270918C180"),
            ],
        )

        result = refresh_options_radar(con, ["RBLX"])
        event = query_rows(con, "SELECT state, trigger_reason FROM candidate_event WHERE contract_id = 'OPRA:RBLX270918C120'")[0]
        trades = query_rows(con, "SELECT * FROM shadow_trade")

    assert result["candidate_events"] == 2
    assert event["state"] == "WATCH"
    assert "missing_open_interest" in event["trigger_reason"]
    assert "missing_volume" in event["trigger_reason"]
    assert trades == []


def test_options_radar_uses_yfinance_liquidity_enrichment_for_fire_candidate(tmp_path) -> None:
    db_path = tmp_path / "radar-yfinance-liquidity.duckdb"
    init_db(db_path)
    chain_observed_at = "2026-06-02T20:00:00Z"
    with db(db_path) as con:
        seed_prices(con, "RBLX", start_price=75, slope=0.11)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute("INSERT INTO quotes_intraday VALUES ('RBLX', ?, 100, 1, 1, 'USD', 'tradingview', '{}')", [chain_observed_at])
        store_options_chain(
            con,
            "RBLX",
            chain_observed_at,
            [
                option_row("2027-09-18", 120, "call", 4.3, 4.7, 0.25, 0.30, "OPRA:RBLX270918C120"),
                option_row("2027-09-18", 180, "call", 7.5, 8.5, 0.50, 0.30, "OPRA:RBLX270918C180"),
            ],
        )
        updated = store_yfinance_options_liquidity(
            con,
            "RBLX",
            "2027-09-18",
            "2026-06-02T20:05:00Z",
            chain_observed_at,
            [
                {
                    "expiry": "2027-09-18",
                    "strike": 120,
                    "type": "call",
                    "volume": 25,
                    "openInterest": 250,
                    "contract_symbol": "RBLX270918C00120000",
                },
                {
                    "expiry": "2027-09-18",
                    "strike": 180,
                    "type": "call",
                    "volume": 25,
                    "openInterest": 250,
                    "contract_symbol": "RBLX270918C00180000",
                },
            ],
        )

        result = refresh_options_radar(con, ["RBLX"])
        snapshot = query_rows(con, "SELECT volume, open_interest FROM option_snapshot WHERE contract_id = 'OPRA:RBLX270918C120'")[0]
        event = query_rows(con, "SELECT state, trigger_reason FROM candidate_event WHERE contract_id = 'OPRA:RBLX270918C120'")[0]
        trades = query_rows(con, "SELECT * FROM shadow_trade")

    assert updated == 2
    assert snapshot == {"volume": 25, "open_interest": 250}
    assert result["shadow_trades"] == 1
    assert event["state"] == "FIRE"
    assert "open_interest_supported" in event["trigger_reason"]
    assert "volume_seen" in event["trigger_reason"]
    assert len(trades) == 1


def test_options_radar_tables_load_through_panel_contract(tmp_path) -> None:
    db_path = tmp_path / "panel-radar.duckdb"
    init_db(db_path)
    with db(db_path) as con:
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
        refresh_options_radar(con, ["TSLA"])

    panel = load_panel_data({"database": {"duckdb_path": str(db_path)}})
    assert panel["tables"]["option_snapshot"][0]["ticker"] == "TSLA"
    assert panel["tables"]["candidate_event"][0]["strategy_version"] == DEFAULT_STRATEGY_VERSION
    assert panel["tables"]["candidate_event_mark"][0]["candidate_state"] in {"FIRE", "REJECT"}
    assert "candidate_event_attribution" in panel["tables"]
    assert panel["tables"]["shadow_trade"][0]["status"] == "open"
    assert panel["tables"]["radar_state_transition"][0]["state"] in {"FIRE", "REJECT"}


def test_options_radar_attributes_shadow_trade_return(tmp_path) -> None:
    db_path = tmp_path / "radar-attribution.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "TSLA", slope=0.12)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-02T20:00:00Z', 102, 1, 1, 'USD', 'tradingview', '{}')"
        )
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-03T20:00:00Z', 112, 1, 1, 'USD', 'tradingview', '{}')"
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
            "2026-06-03T20:00:00Z",
            [option_row("2027-09-18", 120, "call", 9.0, 10.0, 0.28, 0.38, "OPRA:TSLA270918C120", volume=45, open_interest=275)],
        )

        result = refresh_options_radar(con, ["TSLA"])
        attribution = query_rows(con, "SELECT * FROM option_attribution")[0]
        candidate_attribution = query_rows(con, "SELECT * FROM candidate_event_attribution")[0]
        first_trade = query_rows(con, "SELECT * FROM shadow_trade ORDER BY entry_time LIMIT 1")[0]
        candidate_marks = query_rows(con, "SELECT * FROM candidate_event_mark ORDER BY mark_time")
        marks = query_rows(con, "SELECT * FROM shadow_trade_mark ORDER BY mark_time")
        transitions = query_rows(con, "SELECT state, previous_state, trigger_reason FROM radar_state_transition WHERE contract_id = 'OPRA:TSLA270918C120' ORDER BY snapshot_time")
        cohorts = query_rows(con, "SELECT * FROM strategy_cohort_result")
        setup_cohort = query_rows(con, "SELECT * FROM strategy_cohort_result WHERE cohort_type = 'setup_type'")[0]
        market_cohort = query_rows(con, "SELECT * FROM strategy_cohort_result WHERE cohort_value = 'qqq_above_200d'")[0]

    assert result["option_attributions"] == 1
    assert result["candidate_event_attributions"] == 1
    assert result["candidate_event_marks"] == 3
    assert result["shadow_trade_marks"] == 2
    assert result["radar_state_transitions"] == 2
    assert result["strategy_cohorts"] >= 4
    assert attribution["trade_id"] == first_trade["trade_id"]
    assert attribution["label"] == "good_convexity"
    assert attribution["option_return"] > 1.0
    assert attribution["underlying_return"] > 0
    assert candidate_attribution["event_id"] == first_trade["event_id"]
    assert candidate_attribution["candidate_state"] == "FIRE"
    assert candidate_attribution["label"] == "good_convexity"
    assert candidate_attribution["option_return"] > 1.0
    assert candidate_attribution["underlying_return"] > 0
    assert first_trade["max_return_seen"] > 1.0
    assert first_trade["time_to_2x"] == 1
    trade_candidate_marks = [row for row in candidate_marks if row["event_id"] == first_trade["event_id"]]
    assert len(candidate_marks) == 3
    assert len(trade_candidate_marks) == 2
    assert trade_candidate_marks[-1]["current_return"] > 1.0
    assert trade_candidate_marks[-1]["time_to_2x"] == 1
    assert len(marks) == 2
    assert marks[-1]["trade_id"] == first_trade["trade_id"]
    assert marks[-1]["current_return"] > 1.0
    assert marks[-1]["return_1d"] > 1.0
    assert marks[-1]["return_5d"] is None
    assert marks[-1]["max_return_since_alert"] == marks[-1]["current_return"]
    assert marks[-1]["expired_worthless_probability_change"] < 0
    assert [row["state"] for row in transitions] == ["FIRE", "HOLD"]
    assert transitions[-1]["previous_state"] == "FIRE"
    assert transitions[-1]["trigger_reason"] == "hit_2x_continue_tracking"
    assert setup_cohort["candidate_count"] == 1
    assert setup_cohort["hit_rate_2x"] == 1.0
    assert setup_cohort["good_convexity_rate"] == 1.0
    assert market_cohort["qqq_above_200d_rate"] == 1.0
    assert cohorts


def test_hard_red_team_validation_invalidates_and_closes_shadow_trade(tmp_path) -> None:
    db_path = tmp_path / "radar-red-team-exit.duckdb"
    init_db(db_path)
    with db(db_path) as con:
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
        first = refresh_options_radar(con, ["TSLA"])
        assert first["shadow_trades"] == 1

        con.execute(
            "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-03T20:00:00Z', 103, 1, 1, 'USD', 'tradingview', '{}')"
        )
        store_options_chain(
            con,
            "TSLA",
            "2026-06-03T20:00:00Z",
            [
                option_row("2027-09-18", 120, "call", 3.9, 4.1, 0.24, 0.29, "OPRA:TSLA270918C120", volume=30, open_interest=260),
                option_row("2027-09-18", 180, "call", 7.0, 8.0, 0.50, 0.30, "OPRA:TSLA270918C180", volume=30, open_interest=260),
            ],
        )
        con.execute(
            """
            INSERT INTO equity_fundamentals
            VALUES ('TSLA', '2026-03-31', '2026-05-01', '10-Q', ?, 'https://example.com/tsla')
            """,
            [
                json.dumps(
                    {
                        "free_cash_flow": -250000000,
                        "cash": 50000000,
                        "total_debt": 600000000,
                        "assets": 1000000000,
                        "liabilities": 800000000,
                    }
                ),
            ],
        )
        upsert_agent_thesis(
            con,
            {
                "ticker": "TSLA",
                "created_at": "2026-06-03T21:00:00Z",
                "bull_target_price": 180,
                "bull_target_date": "2028-01-21",
                "base_target_price": 95,
                "core_thesis": "Energy storage and autonomy narrative returns while margins stabilize.",
                "required_proofs": ["gross margin stabilizes"],
                "catalysts": [{"type": "earnings", "what_to_watch": "margins"}],
                "invalidation": ["stock breaks below $80 without recovery"],
                "bear_case": "Cash burn and balance sheet pressure can overwhelm the rebound thesis.",
                "confidence": 55,
                "evidence_refs": [{"type": "source_signal", "id": "agent-red-team"}],
            },
        )

        result = refresh_options_radar(con, ["TSLA"])
        transitions = query_rows(
            con,
            """
            SELECT state, previous_state, trigger_reason
            FROM radar_state_transition
            WHERE contract_id = 'OPRA:TSLA270918C120'
            ORDER BY snapshot_time
            """,
        )
        trades = query_rows(con, "SELECT status, exit_time, exit_price, exit_reason, raw FROM shadow_trade ORDER BY entry_time")
        validation = query_rows(con, "SELECT state, red_team_status FROM agent_thesis_validation WHERE ticker = 'TSLA'")[0]

    assert result["agent_thesis_validations"] == 1
    assert result["shadow_trades"] == 0
    assert result["shadow_trades_exited"] == 1
    assert validation == {"state": "pending", "red_team_status": "hard_risk_triggered"}
    assert transitions == [
        {"state": "FIRE", "previous_state": None, "trigger_reason": "premium_triggered_shadow_entry"},
        {"state": "INVALIDATED", "previous_state": "FIRE", "trigger_reason": "hard_red_team_risk"},
    ]
    assert len(trades) == 1
    assert trades[0]["status"] == "closed"
    assert str(trades[0]["exit_time"]).startswith("2026-06-03")
    assert trades[0]["exit_price"] == 4.0
    assert trades[0]["exit_reason"] == "hard_red_team_risk"
    assert "deterministic_radar_state" in str(trades[0]["raw"])


def test_candidate_scoped_thesis_validation_does_not_block_other_fire_events(tmp_path) -> None:
    db_path = tmp_path / "radar-candidate-scoped-validation.duckdb"
    init_db(db_path)
    with db(db_path) as con:
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
                option_row("2027-09-18", 130, "call", 4.0, 4.4, 0.25, 0.30, "OPRA:TSLA270918C130", volume=25, open_interest=250),
                option_row("2027-09-18", 180, "call", 7.5, 8.5, 0.50, 0.30, "OPRA:TSLA270918C180", volume=25, open_interest=250),
            ],
        )
        con.execute(
            """
            INSERT INTO equity_fundamentals
            VALUES ('TSLA', '2026-03-31', '2026-05-01', '10-Q', ?, 'https://example.com/tsla')
            """,
            [
                json.dumps(
                    {
                        "free_cash_flow": -250000000,
                        "cash": 50000000,
                        "total_debt": 600000000,
                        "assets": 1000000000,
                        "liabilities": 800000000,
                    }
                ),
            ],
        )
        upsert_agent_thesis(
            con,
            {
                "ticker": "TSLA",
                "created_at": "2026-06-03T21:00:00Z",
                "bull_target_price": 180,
                "bull_target_date": "2028-01-21",
                "base_target_price": 95,
                "core_thesis": "Energy storage and autonomy narrative returns while margins stabilize.",
                "required_proofs": ["gross margin stabilizes"],
                "catalysts": [{"type": "earnings", "what_to_watch": "margins"}],
                "invalidation": ["stock breaks below $80 without recovery"],
                "bear_case": "Cash burn and balance sheet pressure can overwhelm the rebound thesis.",
                "confidence": 55,
                "evidence_refs": [{"type": "source_signal", "id": "candidate-scope"}],
            },
        )

        result = refresh_options_radar(con, ["TSLA"])
        fire_events = query_rows(
            con,
            "SELECT event_id, contract_id FROM candidate_event WHERE state = 'FIRE' ORDER BY contract_id",
        )
        validation = query_rows(
            con,
            "SELECT candidate_event_id, red_team_status FROM agent_thesis_validation WHERE ticker = 'TSLA'",
        )[0]
        trades = query_rows(con, "SELECT event_id FROM shadow_trade ORDER BY event_id")

    blocked_event_id = validation["candidate_event_id"]
    traded_event_ids = {row["event_id"] for row in trades}
    assert result["agent_thesis_validations"] == 1
    assert validation["red_team_status"] == "hard_risk_triggered"
    assert len(fire_events) == 2
    assert blocked_event_id not in traded_event_ids
    assert traded_event_ids == {row["event_id"] for row in fire_events if row["event_id"] != blocked_event_id}


def test_options_radar_detects_missed_winner_and_requires_gated_strategy_proposal(tmp_path) -> None:
    db_path = tmp_path / "radar-missed.duckdb"
    init_db(db_path)
    with db(db_path) as con:
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

        result = refresh_options_radar(con, ["RBLX"])
        missed = query_rows(con, "SELECT * FROM missed_winner_event")[0]
        proposal = query_rows(con, "SELECT * FROM strategy_mutation_proposal")[0]
        backtest = query_rows(con, "SELECT * FROM strategy_backtest_result")[0]
        forward = query_rows(con, "SELECT * FROM strategy_forward_test_result")[0]
        candidate_marks = query_rows(con, "SELECT * FROM candidate_event_mark ORDER BY mark_time")
        candidate_attribution = query_rows(con, "SELECT * FROM candidate_event_attribution")[0]
        trades = query_rows(con, "SELECT * FROM shadow_trade")

    assert result["candidate_event_marks"] == 3
    assert result["candidate_event_attributions"] == 1
    assert result["missed_winners"] == 1
    assert result["strategy_mutation_proposals"] == 1
    assert result["strategy_backtests"] == 1
    assert result["strategy_forward_tests"] == 1
    assert trades == []
    assert missed["winner_threshold"] == "10x"
    best_candidate_mark = max(candidate_marks, key=lambda row: row["max_return_since_alert"])
    assert best_candidate_mark["candidate_state"] == "REJECT"
    assert best_candidate_mark["time_to_10x"] == 18
    assert best_candidate_mark["max_return_since_alert"] >= 9.0
    assert candidate_attribution["candidate_state"] == "REJECT"
    assert candidate_attribution["label"] == "good_convexity"
    assert candidate_attribution["option_return"] >= 9.0
    assert missed["filter_reason"] == "delta_outside_strategy_range"
    assert missed["proposed_strategy_family"] == "leap_10x_momentum_lottery"
    assert proposal["status"] == "forward_test_required"
    assert proposal["requires_backtest"] is True
    assert proposal["requires_forward_test"] is True
    assert proposal["human_approval_status"] == "required"
    assert "lower-delta" in proposal["proposed_parameter_changes"]
    assert backtest["verdict"] == "pass"
    assert backtest["proposed_candidate_count"] > backtest["baseline_candidate_count"]
    assert backtest["proposed_hit_rate_10x"] > backtest["baseline_hit_rate_10x"]
    assert forward["status"] == "active"
    assert forward["verdict"] == "collecting_data"


def test_strategy_promotion_requires_backtest_forward_test_and_human_approval(tmp_path) -> None:
    db_path = tmp_path / "radar-promotion.duckdb"
    init_db(db_path)
    with db(db_path) as con:
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
        refresh_options_radar(con, ["RBLX"])
        proposal_id = query_rows(con, "SELECT proposal_id FROM strategy_mutation_proposal")[0]["proposal_id"]

        with pytest.raises(StrategyPromotionError, match="forward shadow test"):
            promote_strategy_mutation(con, proposal_id, approved_by="joe")

        con.execute(
            """
            UPDATE strategy_forward_test_result
            SET verdict = 'pass', status = 'complete', days_observed = 30
            WHERE proposal_id = ?
            """,
            [proposal_id],
        )
        with pytest.raises(StrategyPromotionError, match="human approval"):
            promote_strategy_mutation(con, proposal_id)
        promoted = promote_strategy_mutation(con, proposal_id, approved_by="joe")
        strategy = query_rows(con, "SELECT strategy_version, status, supersedes FROM option_strategy_versions WHERE strategy_version = ?", [promoted])[0]
        proposal = query_rows(con, "SELECT status, human_approval_status FROM strategy_mutation_proposal WHERE proposal_id = ?", [proposal_id])[0]

    assert promoted == "leap_10x_momentum_lottery_proposed_v1"
    assert strategy == {"strategy_version": promoted, "status": "promoted", "supersedes": DEFAULT_STRATEGY_VERSION}
    assert proposal == {"status": "promoted", "human_approval_status": "approved"}


def seed_prices(con, symbol: str, start_price: float = 80.0, slope: float = 0.10) -> None:
    start = date(2025, 10, 26)
    for index in range(220):
        day = start + timedelta(days=index)
        close = start_price + slope * index
        con.execute(
            """
            INSERT OR REPLACE INTO prices_daily
            (symbol, date, open, high, low, close, volume, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [symbol, day.isoformat(), close * 0.995, close * 1.01, close * 0.99, close, 1_000_000 + index * 1000, "test"],
        )


def option_row(
    expiry: str,
    strike: float,
    option_type: str,
    bid: float,
    ask: float,
    iv: float,
    delta: float,
    symbol: str,
    *,
    volume: int | None = None,
    open_interest: int | None = None,
) -> dict[str, object]:
    return {
        "expiry": expiry,
        "dte": 473,
        "strike": strike,
        "type": option_type,
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2,
        "last": (bid + ask) / 2,
        "volume": volume,
        "open_interest": open_interest,
        "iv": iv,
        "delta": delta,
        "gamma": 0.01,
        "theta": -0.01,
        "vega": 0.2,
        "symbol": symbol,
    }
