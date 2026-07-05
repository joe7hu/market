from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from investment_panel.core.db import db, init_db, query_rows, upsert_instrument
from investment_panel.core.free_sources import store_options_chain, store_yfinance_market_snapshot, store_yfinance_options_liquidity
from investment_panel.core.option_agent_thesis import upsert_agent_thesis
from investment_panel.core.options_radar import (
    DEFAULT_STRATEGY_VERSION,
    StrategyPromotionError,
    promote_strategy_mutation,
    refresh_option_radar_opportunities,
    refresh_options_radar,
)
from investment_panel.core.options_radar.opportunities import _rank_opportunity_details


def test_options_radar_persists_fire_candidate_and_shadow_trade(tmp_path) -> None:
    db_path = tmp_path / "radar.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "TSLA", slope=0.12)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-02T19:00:00Z', 102, 1, 1, 'USD', 'tradingview', '{}')"
        )
        store_options_chain(
            con,
            "TSLA",
            "2026-06-02T19:00:00Z",
            [
                option_row("2027-09-18", 120, "call", 2.9, 3.0, 0.25, 0.30, "OPRA:TSLA270918C120", volume=25, open_interest=250),
                option_row("2027-09-18", 180, "call", 7.5, 8.5, 0.50, 0.30, "OPRA:TSLA270918C180", volume=25, open_interest=250),
            ],
        )

        result = refresh_options_radar(con, ["TSLA"])
        strategy = query_rows(con, "SELECT strategy_version, status FROM option_strategy_versions WHERE strategy_version = ?", [DEFAULT_STRATEGY_VERSION])
        snapshots = query_rows(con, "SELECT contract_id, open_interest, volume FROM option_snapshot ORDER BY strike")
        feature = query_rows(con, "SELECT required_10x_price, required_move_10x_pct, iv_percentile FROM option_features WHERE contract_id = 'OPRA:TSLA270918C120'")[0]
        fire = query_rows(con, "SELECT * FROM candidate_event WHERE contract_id = 'OPRA:TSLA270918C120' AND strategy_version = ?", [DEFAULT_STRATEGY_VERSION])[0]
        rejects = query_rows(con, "SELECT state, trigger_reason FROM candidate_event WHERE contract_id = 'OPRA:TSLA270918C180' AND strategy_version = ?", [DEFAULT_STRATEGY_VERSION])[0]
        candidate_marks = query_rows(con, "SELECT contract_id, candidate_state, current_return FROM candidate_event_mark WHERE strategy_version = ? ORDER BY contract_id", [DEFAULT_STRATEGY_VERSION])
        trades = query_rows(con, "SELECT * FROM shadow_trade")
        transitions = query_rows(con, "SELECT contract_id, state, candidate_state FROM radar_state_transition WHERE strategy_version = ? ORDER BY contract_id", [DEFAULT_STRATEGY_VERSION])
        default_candidates = query_rows(con, "SELECT contract_id FROM candidate_event WHERE strategy_version = ?", [DEFAULT_STRATEGY_VERSION])

    assert result["option_snapshots"] == 2
    assert len(default_candidates) == 2
    assert len(candidate_marks) == 2
    assert result["candidate_event_attributions"] == 0
    assert len(transitions) == 2
    assert strategy == [{"strategy_version": DEFAULT_STRATEGY_VERSION, "status": "shadow"}]
    assert snapshots[0]["open_interest"] == 250
    assert snapshots[0]["volume"] == 25
    assert round(feature["required_10x_price"], 2) == 149.5
    assert round(feature["required_move_10x_pct"], 3) == 0.466
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


def test_options_radar_runs_learning_and_opportunities_for_deep_otm_family(tmp_path) -> None:
    db_path = tmp_path / "radar-deep-family.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "NVDA", slope=0.35)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('NVDA', '2026-06-02T19:00:00Z', 102, 1, 1, 'USD', 'tradingview', '{}')"
        )
        store_options_chain(
            con,
            "NVDA",
            "2026-06-02T19:00:00Z",
            [
                option_row(
                    "2027-09-18",
                    240,
                    "call",
                    1.2,
                    1.3,
                    0.75,
                    0.10,
                    "OPRA:NVDA270918C240",
                    dte=473,
                    volume=0,
                    open_interest=120,
                ),
                option_row(
                    "2027-09-18",
                    260,
                    "call",
                    1.8,
                    2.2,
                    1.40,
                    0.08,
                    "OPRA:NVDA270918C260",
                    dte=473,
                    volume=0,
                    open_interest=80,
                ),
            ],
        )

        result = refresh_options_radar(con, ["NVDA"])
        deep_candidate = query_rows(
            con,
            """
            SELECT state, trigger_reason
            FROM candidate_event
            WHERE contract_id = 'OPRA:NVDA270918C240'
              AND strategy_version = 'deep_otm_lottery_call_v1'
            """,
        )[0]
        primary_candidate = query_rows(
            con,
            """
            SELECT state, trigger_reason
            FROM candidate_event
            WHERE contract_id = 'OPRA:NVDA270918C240'
              AND strategy_version = ?
            """,
            [DEFAULT_STRATEGY_VERSION],
        )[0]
        deep_trade = query_rows(
            con,
            """
            SELECT st.status
            FROM shadow_trade st
            JOIN candidate_event ce ON ce.event_id = st.event_id
            WHERE ce.contract_id = 'OPRA:NVDA270918C240'
              AND ce.strategy_version = 'deep_otm_lottery_call_v1'
            """,
        )
        deep_marks = query_rows(
            con,
            """
            SELECT candidate_state
            FROM candidate_event_mark
            WHERE contract_id = 'OPRA:NVDA270918C240'
              AND strategy_version = 'deep_otm_lottery_call_v1'
            """,
        )
        deep_opportunity = query_rows(
            con,
            """
            SELECT primary_contract_id
            FROM option_radar_opportunity
            WHERE ticker = 'NVDA'
              AND strategy_version = 'deep_otm_lottery_call_v1'
            """,
        )

    assert result["shadow_trades"] >= 1
    assert result["candidate_event_marks"] >= 1
    assert result["option_radar_opportunities"] >= 1
    assert deep_candidate["state"] == "FIRE"
    assert "delta_in_range" in deep_candidate["trigger_reason"]
    assert primary_candidate["state"] == "REJECT"
    assert "delta_outside_strategy_range" in primary_candidate["trigger_reason"]
    assert deep_trade == [{"status": "open"}]
    assert deep_marks == [{"candidate_state": "FIRE"}]
    assert deep_opportunity == [{"primary_contract_id": "OPRA:NVDA270918C240"}]


def test_option_radar_opportunity_groups_one_primary_contract_and_blocks_without_evidence(tmp_path) -> None:
    db_path = tmp_path / "radar-opportunity-blocked.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_extreme_opportunity_candidates(con)

        result = refresh_options_radar(con, ["TSLA"])
        opportunity = query_rows(con, "SELECT * FROM option_radar_opportunity WHERE ticker = 'TSLA'")[0]

    blockers = json.loads(opportunity["blockers"]) if isinstance(opportunity["blockers"], str) else opportunity["blockers"]
    alternatives = json.loads(opportunity["alternative_contracts"]) if isinstance(opportunity["alternative_contracts"], str) else opportunity["alternative_contracts"]
    assert result["option_radar_opportunities"] == 1
    assert opportunity["tier"] == "Service Bug"
    assert opportunity["data_contract_status"] == "repair_required"
    assert opportunity["primary_contract_id"] == "OPRA:TSLA270918C115"
    failures = json.loads(opportunity["data_contract_failures"]) if isinstance(opportunity["data_contract_failures"], str) else opportunity["data_contract_failures"]
    repair_jobs = json.loads(opportunity["service_repair_jobs"]) if isinstance(opportunity["service_repair_jobs"], str) else opportunity["service_repair_jobs"]
    assert "needs_source_backed_thesis" not in blockers
    assert "needs_source_evidence" not in blockers
    assert "source_evidence_sync_gap" in failures
    assert "thesis_synthesis_sync_gap" in failures
    assert repair_jobs == ["update_free_sources", "update_arco_data", "run_option_agents", "refresh_options_radar"]
    assert len(alternatives) == 1


def test_opportunity_primary_selection_optimizes_money_not_nearest_expiry() -> None:
    near = opportunity_detail(
        "near",
        expiration="2027-06-17",
        dte=365,
        ev_multiple=1.25,
        p_2x=0.20,
        p_5x=0.04,
        p_10x=0.01,
        conviction_score=82,
    )
    farther = opportunity_detail(
        "farther",
        expiration="2028-01-21",
        dte=583,
        ev_multiple=2.80,
        p_2x=0.48,
        p_5x=0.18,
        p_10x=0.06,
        conviction_score=76,
    )

    ranked = _rank_opportunity_details([near, farther])

    assert ranked[0]["contract_id"] == "farther"
    assert ranked[0]["money_objective_score"] > ranked[1]["money_objective_score"]


def test_opportunity_primary_selection_keeps_data_readiness_as_guardrail() -> None:
    broken_but_flashy = opportunity_detail(
        "broken",
        expiration="2028-01-21",
        dte=583,
        ev_multiple=4.0,
        p_2x=0.70,
        p_5x=0.35,
        p_10x=0.15,
        data_contract_status="repair_required",
        tier="Service Bug",
    )
    usable = opportunity_detail(
        "usable",
        expiration="2027-06-17",
        dte=365,
        ev_multiple=1.20,
        p_2x=0.18,
        p_5x=0.03,
        p_10x=0.0,
    )

    assert _rank_opportunity_details([broken_but_flashy, usable])[0]["contract_id"] == "usable"


def test_options_radar_skips_degraded_snapshot_instead_of_collapsing(tmp_path) -> None:
    """A pre-market/off-hours pull with zero premiums on ~all contracts must not
    overwrite a healthy multi-ticker radar with the 1-2 names that happened to be
    priced. The read model falls back to the last healthy snapshot."""

    db_path = tmp_path / "radar-degraded.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "TSLA", slope=0.12)
        seed_prices(con, "NVDA", slope=0.12)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        # Healthy snapshot: both tickers fully priced. The high-iv decoy contract
        # seeds iv history so the target 120-call doesn't rank at the top iv
        # percentile (which would reject it).
        for symbol in ("TSLA", "NVDA"):
            con.execute(
                f"INSERT INTO quotes_intraday VALUES ('{symbol}', '2026-06-02T20:00:00Z', 102, 1, 1, 'USD', 'tradingview', '{{}}')"
            )
            store_options_chain(
                con,
                symbol,
                "2026-06-02T20:00:00Z",
                [
                    option_row("2027-09-18", 120, "call", 4.3, 4.7, 0.25, 0.30, f"OPRA:{symbol}270918C120", volume=25, open_interest=250),
                    option_row("2027-09-18", 130, "call", 4.3, 4.7, 0.55, 0.30, f"OPRA:{symbol}270918C130", volume=25, open_interest=250),
                ],
            )
        refresh_options_radar(con)
        healthy = {row["ticker"] for row in query_rows(con, "SELECT ticker FROM option_radar_opportunity")}

        # Degraded later snapshot: TSLA has one priced contract (so a candidate
        # exists and it becomes the newest snapshot) buried among zero-premium
        # rows; NVDA comes back entirely unpriced.
        for symbol in ("TSLA", "NVDA"):
            con.execute(
                f"INSERT INTO quotes_intraday VALUES ('{symbol}', '2026-06-03T11:56:00Z', 102, 1, 1, 'USD', 'tradingview', '{{}}')"
            )
        degraded = [option_row("2027-09-18", strike, "call", 0.0, 0.0, 0.55, 0.30, f"OPRA:TSLA270918C{strike}", volume=0, open_interest=0) for strike in range(120, 220, 5)]
        degraded[0] = option_row("2027-09-18", 120, "call", 4.3, 4.7, 0.25, 0.30, "OPRA:TSLA270918C120", volume=25, open_interest=250)
        store_options_chain(con, "TSLA", "2026-06-03T11:56:00Z", degraded)
        store_options_chain(
            con,
            "NVDA",
            "2026-06-03T11:56:00Z",
            [option_row("2027-09-18", strike, "call", 0.0, 0.0, 0.55, 0.30, f"OPRA:NVDA270918C{strike}", volume=0, open_interest=0) for strike in range(120, 220, 5)],
        )
        refresh_options_radar(con)
        after = {row["ticker"] for row in query_rows(con, "SELECT ticker FROM option_radar_opportunity")}

    assert healthy == {"TSLA", "NVDA"}
    # The degraded snapshot must not collapse the radar to just the priced name.
    assert after == {"TSLA", "NVDA"}


def test_option_radar_opportunity_closes_thesis_loop_from_source_evidence(tmp_path) -> None:
    db_path = tmp_path / "radar-opportunity-source-backed.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_extreme_opportunity_candidates(con)
        refresh_options_radar(con, ["TSLA"])
        seed_source_signal(con, "sig-tsla-a", "source-tsla-a")
        seed_source_signal(con, "sig-tsla-b", "source-tsla-b")
        seed_source_signal(con, "sig-tsla-c", "source-tsla-c")
        seed_source_signal(con, "sig-tsla-d", "source-tsla-d")

        refresh_option_radar_opportunities(con, ["TSLA"])
        opportunity = query_rows(con, "SELECT * FROM option_radar_opportunity WHERE ticker = 'TSLA'")[0]

    blockers = json.loads(opportunity["blockers"]) if isinstance(opportunity["blockers"], str) else opportunity["blockers"]
    top_reasons = json.loads(opportunity["top_reasons"]) if isinstance(opportunity["top_reasons"], str) else opportunity["top_reasons"]
    assert opportunity["tier"] == "Exceptional"
    assert opportunity["data_contract_status"] == "ready"
    assert blockers == []
    assert "source_backed_thesis" in top_reasons


def test_option_radar_opportunity_treats_etf_contract_as_systematic_context(tmp_path) -> None:
    db_path = tmp_path / "radar-opportunity-etf.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        upsert_instrument(con, {"symbol": "QQQ", "name": "Nasdaq 100 ETF", "asset_class": "etf"})
        seed_prices(con, "QQQ", start_price=100, slope=0.20)
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('QQQ', '2026-06-02T20:00:00Z', 140, 1, 1, 'USD', 'yfinance', '{}')"
        )
        store_options_chain(
            con,
            "QQQ",
            "2026-06-02T20:00:00Z",
            [
                option_row("2027-09-18", 170, "call", 3.95, 4.05, 0.28, 0.20, "QQQ270918C00170000", volume=500, open_interest=1500),
                option_row("2027-09-18", 230, "call", 6.95, 7.05, 0.80, 0.50, "QQQ270918C00230000", volume=500, open_interest=1500),
            ],
            source="yfinance",
        )

        refresh_options_radar(con, ["QQQ"])
        opportunity = query_rows(con, "SELECT * FROM option_radar_opportunity WHERE ticker = 'QQQ'")[0]

    failures = json.loads(opportunity["data_contract_failures"]) if isinstance(opportunity["data_contract_failures"], str) else opportunity["data_contract_failures"]
    satisfied = json.loads(opportunity["data_contract_satisfied"]) if isinstance(opportunity["data_contract_satisfied"], str) else opportunity["data_contract_satisfied"]
    assert opportunity["data_contract_status"] == "ready"
    assert "source_evidence_sync_gap" not in failures
    assert "thesis_synthesis_sync_gap" not in failures
    assert "etf_macro_contract" in satisfied
    assert "etf_systematic_thesis" in satisfied


def test_option_radar_opportunity_requires_extreme_gates_for_exceptional_tier(tmp_path) -> None:
    db_path = tmp_path / "radar-opportunity-exceptional.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_extreme_opportunity_candidates(con)
        refresh_options_radar(con, ["TSLA"])
        event = query_rows(
            con,
            """
            SELECT event_id, snapshot_time
            FROM candidate_event
            WHERE contract_id = 'OPRA:TSLA270918C115' AND strategy_version = ?
            """,
            [DEFAULT_STRATEGY_VERSION],
        )[0]
        seed_source_signal(con, "sig-tsla-a", "source-tsla-a")
        seed_source_signal(con, "sig-tsla-b", "source-tsla-b")
        seed_validated_option_thesis(con, event["event_id"], event["snapshot_time"])

        refresh_option_radar_opportunities(con, ["TSLA"])
        opportunity = query_rows(con, "SELECT * FROM option_radar_opportunity WHERE ticker = 'TSLA'")[0]

    blockers = json.loads(opportunity["blockers"]) if isinstance(opportunity["blockers"], str) else opportunity["blockers"]
    top_reasons = json.loads(opportunity["top_reasons"]) if isinstance(opportunity["top_reasons"], str) else opportunity["top_reasons"]
    assert opportunity["tier"] == "Exceptional"
    assert opportunity["primary_event_id"] == event["event_id"]
    assert opportunity["conviction_score"] >= 78
    assert blockers == []
    assert "thesis_validated" in top_reasons
    assert "source_evidence_cluster" in top_reasons


def test_option_radar_opportunity_demotes_large_bank_without_validated_catalyst(tmp_path) -> None:
    db_path = tmp_path / "radar-opportunity-bank-plausibility.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        upsert_instrument(
            con,
            {
                "symbol": "JPM",
                "name": "JPMorgan Chase & Co.",
                "asset_class": "equity",
                "sector": "Financial Services",
                "industry": "Banks - Diversified",
            },
        )
        seed_prices(con, "JPM", start_price=290, slope=0.10)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('JPM', '2026-06-02T20:00:00Z', 312, 1, 1, 'USD', 'tradingview', '{}')"
        )
        store_yfinance_market_snapshot(
            con,
            "test-run",
            "JPM",
            "2026-06-02T19:50:00Z",
            {"marketCap": 802_000_000_000, "revenueGrowth": 0.127},
        )
        store_options_chain(
            con,
            "JPM",
            "2026-06-02T20:00:00Z",
            [
                option_row("2028-01-21", 420, "call", 12.5, 13.0, 0.20, 0.30, "OPRA:JPM280121C420", volume=25, open_interest=600),
                option_row("2028-01-21", 520, "call", 17.5, 18.0, 0.50, 0.50, "OPRA:JPM280121C520", volume=25, open_interest=600),
            ],
        )
        for index in range(4):
            seed_source_signal_for_symbol(con, "JPM", f"sig-jpm-{index}", f"source-jpm-{index}")

        refresh_options_radar(con, ["JPM"])
        refresh_option_radar_opportunities(con, ["JPM"])
        opportunity = query_rows(con, "SELECT * FROM option_radar_opportunity WHERE ticker = 'JPM'")[0]

    blockers = json.loads(opportunity["blockers"]) if isinstance(opportunity["blockers"], str) else opportunity["blockers"]
    assert opportunity["tier"] == "Research"
    assert "bank_move_implausible_without_validated_catalyst" in blockers


def test_options_radar_preserves_missing_liquidity_candidate_without_trade(tmp_path) -> None:
    db_path = tmp_path / "radar-watch.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "RBLX", start_price=75, slope=0.11)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('RBLX', '2026-06-02T19:00:00Z', 100, 1, 1, 'USD', 'tradingview', '{}')"
        )
        store_options_chain(
            con,
            "RBLX",
            "2026-06-02T19:00:00Z",
            [
                option_row("2027-09-18", 120, "call", 4.3, 4.7, 0.25, 0.30, "OPRA:RBLX270918C120"),
                option_row("2027-09-18", 180, "call", 7.5, 8.5, 0.50, 0.30, "OPRA:RBLX270918C180"),
            ],
        )

        result = refresh_options_radar(con, ["RBLX"])
        event = query_rows(con, "SELECT state, trigger_reason FROM candidate_event WHERE contract_id = 'OPRA:RBLX270918C120' AND strategy_version = ?", [DEFAULT_STRATEGY_VERSION])[0]
        trades = query_rows(con, "SELECT * FROM shadow_trade")
        default_candidates = query_rows(con, "SELECT contract_id FROM candidate_event WHERE strategy_version = ?", [DEFAULT_STRATEGY_VERSION])

    assert len(default_candidates) == 2
    assert event["state"] == "WATCH"
    assert "missing_open_interest" in event["trigger_reason"]
    assert "missing_volume" in event["trigger_reason"]
    assert trades == []


def test_options_radar_prioritizes_10x_watch_themes_without_bypassing_gates(tmp_path) -> None:
    db_path = tmp_path / "radar-theme-watch.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        upsert_instrument(
            con,
            {
                "symbol": "NVDA",
                "name": "NVIDIA Corporation",
                "asset_class": "equity",
                "sector": "Technology",
                "industry": "Semiconductors",
                "category": "AI accelerator infrastructure",
            },
        )
        upsert_instrument(
            con,
            {
                "symbol": "ACME",
                "name": "Acme Industrials",
                "asset_class": "equity",
                "sector": "Industrials",
                "industry": "Specialty Industrial Machinery",
            },
        )
        upsert_instrument(
            con,
            {
                "symbol": "BOTZ",
                "name": "Botz Robotics",
                "asset_class": "equity",
                "sector": "Industrials",
                "industry": "Robotics and Factory Automation",
                "category": "physical AI robotics",
            },
        )
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        for symbol in ["NVDA", "BOTZ", "ACME"]:
            seed_prices(con, symbol, start_price=100, slope=0.12)
            con.execute(
                "INSERT INTO quotes_intraday VALUES (?, '2026-06-02T19:00:00Z', 120, 1, 1, 'USD', 'tradingview', '{}')",
                [symbol],
            )
            store_options_chain(
                con,
                symbol,
                "2026-06-02T19:00:00Z",
                [
                    option_row("2027-09-18", 145, "call", 2.9, 3.0, 0.25, 0.30, f"OPRA:{symbol}270918C145", volume=25, open_interest=250),
                    option_row("2027-09-18", 210, "call", 7.5, 8.5, 0.50, 0.50, f"OPRA:{symbol}270918C210", volume=25, open_interest=250),
                ],
            )

        refresh_options_radar(con, ["NVDA", "BOTZ", "ACME"])
        rows = query_rows(con, "SELECT ticker, state, score, trigger_reason, raw FROM candidate_event WHERE contract_id LIKE '%C145' AND strategy_version = ? ORDER BY ticker", [DEFAULT_STRATEGY_VERSION])

    by_ticker = {row["ticker"]: row for row in rows}
    nvda_raw = json.loads(by_ticker["NVDA"]["raw"]) if isinstance(by_ticker["NVDA"]["raw"], str) else by_ticker["NVDA"]["raw"]
    acme_raw = json.loads(by_ticker["ACME"]["raw"]) if isinstance(by_ticker["ACME"]["raw"], str) else by_ticker["ACME"]["raw"]
    assert by_ticker["NVDA"]["state"] == by_ticker["ACME"]["state"] == "FIRE"
    assert by_ticker["BOTZ"]["state"] == "FIRE"
    assert "theme_ai_infrastructure" in by_ticker["NVDA"]["trigger_reason"]
    assert "theme_robotics_physical_ai" in by_ticker["BOTZ"]["trigger_reason"]
    assert "theme_ai_infrastructure" in nvda_raw["watch_themes"]
    botz_raw = json.loads(by_ticker["BOTZ"]["raw"]) if isinstance(by_ticker["BOTZ"]["raw"], str) else by_ticker["BOTZ"]["raw"]
    assert "theme_robotics_physical_ai" in botz_raw["watch_themes"]
    assert "theme_ai_infrastructure" not in acme_raw.get("watch_themes", [])
    assert "theme_robotics_physical_ai" not in acme_raw.get("watch_themes", [])
    assert by_ticker["NVDA"]["score"] > by_ticker["ACME"]["score"]
    assert by_ticker["BOTZ"]["score"] > by_ticker["ACME"]["score"]


def test_options_radar_uses_yfinance_liquidity_enrichment_for_fire_candidate(tmp_path) -> None:
    db_path = tmp_path / "radar-yfinance-liquidity.duckdb"
    init_db(db_path)
    chain_observed_at = "2026-06-02T19:00:00Z"
    with db(db_path) as con:
        seed_prices(con, "RBLX", start_price=75, slope=0.11)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute("INSERT INTO quotes_intraday VALUES ('RBLX', ?, 100, 1, 1, 'USD', 'tradingview', '{}')", [chain_observed_at])
        store_options_chain(
            con,
            "RBLX",
            chain_observed_at,
            [
                option_row("2027-09-18", 120, "call", 2.9, 3.0, 0.25, 0.30, "OPRA:RBLX270918C120"),
                option_row("2027-09-18", 180, "call", 7.5, 8.5, 0.50, 0.30, "OPRA:RBLX270918C180"),
            ],
        )
        updated = store_yfinance_options_liquidity(
            con,
            "RBLX",
            "2027-09-18",
            "2026-06-02T19:05:00Z",
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
        event = query_rows(con, "SELECT state, trigger_reason FROM candidate_event WHERE contract_id = 'OPRA:RBLX270918C120' AND strategy_version = ?", [DEFAULT_STRATEGY_VERSION])[0]
        trades = query_rows(con, "SELECT * FROM shadow_trade")

    assert updated == 2
    assert snapshot == {"volume": 25, "open_interest": 250}
    assert result["shadow_trades"] == 1
    assert event["state"] == "FIRE"
    assert "open_interest_supported" in event["trigger_reason"]
    assert "volume_seen" in event["trigger_reason"]
    assert len(trades) == 1


def test_options_radar_reads_yfinance_chain_source_by_default(tmp_path) -> None:
    db_path = tmp_path / "radar-yfinance-source.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "RBLX", start_price=75, slope=0.11)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute("INSERT INTO quotes_intraday VALUES ('RBLX', '2026-06-02T19:00:00Z', 100, 1, 1, 'USD', 'yfinance', '{}')")
        store_options_chain(
            con,
            "RBLX",
            "2026-06-02T19:00:00Z",
            [
                option_row("2027-09-18", 120, "call", 2.9, 3.0, 0.25, 0.30, "RBLX270918C00120000", volume=25, open_interest=250),
                option_row("2027-09-18", 180, "call", 7.5, 8.5, 0.50, 0.30, "RBLX270918C00180000", volume=25, open_interest=250),
            ],
            source="yfinance",
        )

        tradingview_only = refresh_options_radar(con, ["RBLX"], source="tradingview")
        result = refresh_options_radar(con, ["RBLX"])
        snapshot = query_rows(con, "SELECT data_source, contract_id FROM option_snapshot WHERE contract_id = 'RBLX270918C00120000'")[0]
        event = query_rows(con, "SELECT state, contract_id FROM candidate_event WHERE contract_id = 'RBLX270918C00120000' AND strategy_version = ?", [DEFAULT_STRATEGY_VERSION])[0]

    assert tradingview_only["option_snapshots"] == 0
    assert result["option_snapshots"] == 2
    assert snapshot == {"data_source": "yfinance", "contract_id": "RBLX270918C00120000"}
    assert event["state"] == "FIRE"
    assert event["contract_id"] == "RBLX270918C00120000"


def test_options_radar_models_missing_yfinance_greeks(tmp_path) -> None:
    db_path = tmp_path / "radar-yfinance-modeled-greeks.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "RBLX", start_price=75, slope=0.11)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute("INSERT INTO quotes_intraday VALUES ('RBLX', '2026-06-02T19:00:00Z', 100, 1, 1, 'USD', 'yfinance', '{}')")
        store_options_chain(
            con,
            "RBLX",
            "2026-06-02T19:00:00Z",
            [
                option_row(
                    "2027-09-18",
                    120,
                    "call",
                    2.9,
                    3.0,
                    0.30,
                    None,
                    "RBLX270918C00120000",
                    gamma=None,
                    theta=None,
                    vega=None,
                    volume=25,
                    open_interest=250,
                ),
                option_row(
                    "2027-09-18",
                    180,
                    "call",
                    7.5,
                    8.5,
                    0.50,
                    None,
                    "RBLX270918C00180000",
                    gamma=None,
                    theta=None,
                    vega=None,
                    volume=25,
                    open_interest=250,
                ),
            ],
            source="yfinance",
        )

        result = refresh_options_radar(con, ["RBLX"])
        snapshot = query_rows(
            con,
            """
            SELECT data_source, delta, gamma, theta, vega, raw
            FROM option_snapshot
            WHERE contract_id = 'RBLX270918C00120000'
            """,
        )[0]
        event = query_rows(con, "SELECT state, trigger_reason, quality_status, quality_flags FROM candidate_event WHERE contract_id = 'RBLX270918C00120000' AND strategy_version = ?", [DEFAULT_STRATEGY_VERSION])[0]

    raw = json.loads(snapshot["raw"]) if isinstance(snapshot["raw"], str) else snapshot["raw"]
    quality_flags = json.loads(event["quality_flags"]) if isinstance(event["quality_flags"], str) else event["quality_flags"]
    assert result["option_snapshots"] == 2
    assert snapshot["data_source"] == "yfinance"
    assert 0.20 <= snapshot["delta"] <= 0.45
    assert snapshot["gamma"] > 0
    assert snapshot["theta"] < 0
    assert snapshot["vega"] > 0
    assert raw["greeks_source"] == "black_scholes_model"
    assert "missing_delta" not in event["trigger_reason"]
    assert "delta_in_range" in event["trigger_reason"]
    assert event["quality_status"] == "caution"
    assert "modeled_greeks" in quality_flags


def test_options_radar_models_same_day_yfinance_greeks_with_floor_inputs(tmp_path) -> None:
    db_path = tmp_path / "radar-yfinance-same-day-greeks.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "AAPL", start_price=100, slope=0.01)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute("INSERT INTO quotes_intraday VALUES ('AAPL', '2026-06-05T20:00:00Z', 100, 1, 1, 'USD', 'yfinance', '{}')")
        store_options_chain(
            con,
            "AAPL",
            "2026-06-05T20:00:00Z",
            [
                option_row(
                    "2026-06-05",
                    100,
                    "call",
                    0.5,
                    0.7,
                    0.0,
                    None,
                    "AAPL260605C00100000",
                    dte=0,
                    gamma=None,
                    theta=None,
                    vega=None,
                    volume=25,
                    open_interest=250,
                ),
                option_row(
                    "2026-06-05",
                    1000,
                    "call",
                    0.01,
                    0.02,
                    0.0,
                    None,
                    "AAPL260605C01000000",
                    dte=0,
                    gamma=None,
                    theta=None,
                    vega=None,
                    volume=25,
                    open_interest=250,
                ),
            ],
            source="yfinance",
        )

        refresh_options_radar(con, ["AAPL"])
        snapshot = query_rows(
            con,
            """
            SELECT dte, delta, gamma, theta, vega, raw
            FROM option_snapshot
            WHERE contract_id = 'AAPL260605C00100000'
            """,
        )[0]
        zero_delta_event = query_rows(
            con,
            """
            SELECT trigger_reason
            FROM candidate_event
            WHERE contract_id = 'AAPL260605C01000000'
            """,
        )[0]

    raw = json.loads(snapshot["raw"]) if isinstance(snapshot["raw"], str) else snapshot["raw"]
    assert snapshot["dte"] == 0
    assert snapshot["delta"] is not None
    assert snapshot["gamma"] is not None
    assert snapshot["theta"] is not None
    assert snapshot["vega"] is not None
    assert raw["greeks_source"] == "black_scholes_model"
    assert raw["greeks_model"]["effective_dte"] == 1
    assert raw["greeks_model"]["effective_iv"] == 0.0001
    assert "missing_delta" not in zero_delta_event["trigger_reason"]
    assert "delta_outside_strategy_range" in zero_delta_event["trigger_reason"]


def test_options_radar_uses_tradingview_match_for_missing_yfinance_greeks(tmp_path) -> None:
    db_path = tmp_path / "radar-yfinance-matched-greeks.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "RBLX", start_price=75, slope=0.11)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute("INSERT INTO quotes_intraday VALUES ('RBLX', '2026-06-02T20:00:00Z', 100, 1, 1, 'USD', 'tradingview', '{}')")
        store_options_chain(
            con,
            "RBLX",
            "2026-06-02T20:00:00Z",
            [
                option_row(
                    "2027-09-18",
                    120,
                    "call",
                    4.3,
                    4.7,
                    0.30,
                    0.24,
                    "OPRA:RBLX270918C120",
                    gamma=0.011,
                    theta=-0.015,
                    vega=0.45,
                    volume=25,
                    open_interest=250,
                ),
            ],
            source="tradingview",
        )
        store_options_chain(
            con,
            "RBLX",
            "2026-06-02T20:00:00Z",
            [
                option_row(
                    "2027-09-18",
                    120,
                    "call",
                    4.4,
                    4.8,
                    0.31,
                    None,
                    "RBLX270918C00120000",
                    gamma=None,
                    theta=None,
                    vega=None,
                    volume=30,
                    open_interest=275,
                ),
            ],
            source="yfinance",
        )

        result = refresh_options_radar(con, ["RBLX"], source="yfinance")
        snapshot = query_rows(
            con,
            """
            SELECT delta, gamma, theta, vega, raw
            FROM option_snapshot
            WHERE data_source = 'yfinance' AND contract_id = 'RBLX270918C00120000'
            """,
        )[0]

    raw = json.loads(snapshot["raw"]) if isinstance(snapshot["raw"], str) else snapshot["raw"]
    assert result["option_snapshots"] == 1
    assert snapshot["delta"] == 0.24
    assert snapshot["gamma"] == 0.011
    assert snapshot["theta"] == -0.015
    assert snapshot["vega"] == 0.45
    assert raw["greeks_source"] == "tradingview_match"


def test_options_radar_does_not_use_future_tradingview_greeks_for_yfinance_backfill(tmp_path) -> None:
    db_path = tmp_path / "radar-no-future-greeks.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "RBLX", start_price=75, slope=0.11)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute("INSERT INTO quotes_intraday VALUES ('RBLX', '2026-06-02T20:00:00Z', 100, 1, 1, 'USD', 'yfinance', '{}')")
        store_options_chain(
            con,
            "RBLX",
            "2026-06-02T20:00:00Z",
            [
                option_row(
                    "2027-09-18",
                    120,
                    "call",
                    4.4,
                    4.8,
                    0.31,
                    None,
                    "RBLX270918C00120000",
                    gamma=None,
                    theta=None,
                    vega=None,
                    volume=30,
                    open_interest=275,
                ),
            ],
            source="yfinance",
        )
        store_options_chain(
            con,
            "RBLX",
            "2026-06-03T20:00:00Z",
            [
                option_row(
                    "2027-09-18",
                    120,
                    "call",
                    4.3,
                    4.7,
                    0.30,
                    0.24,
                    "OPRA:RBLX270918C120",
                    gamma=0.011,
                    theta=-0.015,
                    vega=0.45,
                    volume=25,
                    open_interest=250,
                ),
            ],
            source="tradingview",
        )

        refresh_options_radar(con, ["RBLX"], source="yfinance", snapshot_time="2026-06-02T20:00:00Z")
        snapshot = query_rows(
            con,
            """
            SELECT delta, raw
            FROM option_snapshot
            WHERE contract_id = 'RBLX270918C00120000'
            """,
        )[0]

    raw = json.loads(snapshot["raw"]) if isinstance(snapshot["raw"], str) else snapshot["raw"]
    assert raw["greeks_source"] == "black_scholes_model"
    assert snapshot["delta"] != 0.24


def test_options_radar_does_not_model_yfinance_greeks_from_future_underlying_quote(tmp_path) -> None:
    db_path = tmp_path / "radar-no-future-underlying.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "RBLX", start_price=75, slope=0.11)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute("INSERT INTO quotes_intraday VALUES ('RBLX', '2026-06-03T20:00:00Z', 100, 1, 1, 'USD', 'yfinance', '{}')")
        store_options_chain(
            con,
            "RBLX",
            "2026-06-02T20:00:00Z",
            [
                option_row(
                    "2027-09-18",
                    120,
                    "call",
                    4.4,
                    4.8,
                    0.31,
                    None,
                    "RBLX270918C00120000",
                    gamma=None,
                    theta=None,
                    vega=None,
                    volume=30,
                    open_interest=275,
                ),
            ],
            source="yfinance",
        )

        refresh_options_radar(con, ["RBLX"], source="yfinance", snapshot_time="2026-06-02T20:00:00Z")
        snapshot = query_rows(
            con,
            """
            SELECT underlying_price, delta, gamma, theta, vega, raw
            FROM option_snapshot
            WHERE contract_id = 'RBLX270918C00120000'
            """,
        )[0]
        event_count = query_rows(con, "SELECT count(*) AS count FROM candidate_event WHERE contract_id = 'RBLX270918C00120000'")[0]["count"]

    raw = json.loads(snapshot["raw"]) if isinstance(snapshot["raw"], str) else snapshot["raw"]
    assert snapshot["underlying_price"] is None
    assert snapshot["delta"] is None
    assert snapshot["gamma"] is None
    assert snapshot["theta"] is None
    assert snapshot["vega"] is None
    assert "greeks_source" not in raw
    assert event_count == 0


def test_options_radar_marks_source_disagreement_without_noisy_clean_rows(tmp_path) -> None:
    db_path = tmp_path / "radar-source-quality.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "RBLX", start_price=75, slope=0.11)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute("INSERT INTO quotes_intraday VALUES ('RBLX', '2026-06-02T20:00:00Z', 100, 1, 1, 'USD', 'tradingview', '{}')")
        store_options_chain(
            con,
            "RBLX",
            "2026-06-02T20:00:00Z",
            [
                option_row("2027-09-18", 120, "call", 4.3, 4.7, 0.30, 0.25, "OPRA:RBLX270918C120", volume=25, open_interest=250),
                option_row("2027-09-18", 180, "call", 7.5, 8.5, 0.50, 0.50, "OPRA:RBLX270918C180", volume=25, open_interest=250),
            ],
            source="tradingview",
        )
        store_options_chain(
            con,
            "RBLX",
            "2026-06-02T20:00:00Z",
            [
                option_row("2027-09-18", 120, "call", 7.0, 7.4, 0.45, 0.25, "RBLX270918C00120000", volume=25, open_interest=250),
            ],
            source="yfinance",
        )

        refresh_options_radar(con, ["RBLX"])
        tradingview_event = query_rows(
            con,
            """
            SELECT quality_status, quality_flags
            FROM candidate_event
            WHERE contract_id = 'OPRA:RBLX270918C120'
            """,
        )[0]
        yfinance_event = query_rows(
            con,
            """
            SELECT quality_status, quality_flags
            FROM candidate_event
            WHERE contract_id = 'RBLX270918C00120000'
            """,
        )[0]

    tradingview_flags = json.loads(tradingview_event["quality_flags"]) if isinstance(tradingview_event["quality_flags"], str) else tradingview_event["quality_flags"]
    yfinance_flags = json.loads(yfinance_event["quality_flags"]) if isinstance(yfinance_event["quality_flags"], str) else yfinance_event["quality_flags"]
    assert tradingview_event["quality_status"] == "bad"
    assert yfinance_event["quality_status"] == "bad"
    assert "source_mid_disagreement" in tradingview_flags
    assert "source_iv_disagreement" in yfinance_flags
    assert "modeled_greeks" not in tradingview_flags


def test_options_radar_quality_does_not_compare_future_peer_snapshots(tmp_path) -> None:
    db_path = tmp_path / "radar-no-future-peer-quality.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "RBLX", start_price=75, slope=0.11)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute("INSERT INTO quotes_intraday VALUES ('RBLX', '2026-06-02T20:00:00Z', 100, 1, 1, 'USD', 'yfinance', '{}')")
        store_options_chain(
            con,
            "RBLX",
            "2026-06-02T20:00:00Z",
            [
                option_row("2027-09-18", 120, "call", 4.3, 4.7, 0.30, 0.25, "RBLX270918C00120000", volume=25, open_interest=250),
                option_row("2027-09-18", 180, "call", 7.5, 8.5, 0.50, 0.50, "RBLX270918C00180000", volume=25, open_interest=250),
            ],
            source="yfinance",
        )
        store_options_chain(
            con,
            "RBLX",
            "2026-06-03T20:00:00Z",
            [
                option_row("2027-09-18", 120, "call", 7.0, 7.4, 0.45, 0.25, "OPRA:RBLX270918C120", volume=25, open_interest=250),
            ],
            source="tradingview",
        )

        refresh_options_radar(con, ["RBLX"], source="yfinance", snapshot_time="2026-06-02T20:00:00Z")
        event = query_rows(
            con,
            """
            SELECT quality_status, quality_flags
            FROM candidate_event
            WHERE contract_id = 'RBLX270918C00120000'
            """,
        )[0]

    quality_flags = json.loads(event["quality_flags"]) if isinstance(event["quality_flags"], str) else event["quality_flags"]
    assert event["quality_status"] == "ok"
    assert "source_mid_disagreement" not in quality_flags
    assert "source_iv_disagreement" not in quality_flags


def test_options_radar_quality_does_not_compare_stale_peer_snapshots(tmp_path) -> None:
    db_path = tmp_path / "radar-no-stale-peer-quality.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "RBLX", start_price=75, slope=0.11)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute("INSERT INTO quotes_intraday VALUES ('RBLX', '2026-06-03T20:00:00Z', 100, 1, 1, 'USD', 'yfinance', '{}')")
        store_options_chain(
            con,
            "RBLX",
            "2026-06-02T20:00:00Z",
            [
                option_row("2027-09-18", 120, "call", 7.0, 7.4, 0.45, 0.25, "OPRA:RBLX270918C120", volume=25, open_interest=250),
            ],
            source="tradingview",
        )
        store_options_chain(
            con,
            "RBLX",
            "2026-06-03T20:00:00Z",
            [
                option_row("2027-09-18", 120, "call", 4.3, 4.7, 0.30, 0.25, "RBLX270918C00120000", volume=25, open_interest=250),
            ],
            source="yfinance",
        )

        refresh_options_radar(con, ["RBLX"])
        event = query_rows(
            con,
            """
            SELECT quality_status, quality_flags, raw
            FROM candidate_event
            WHERE contract_id = 'RBLX270918C00120000'
            """,
        )[0]

    quality_flags = json.loads(event["quality_flags"]) if isinstance(event["quality_flags"], str) else event["quality_flags"]
    raw = json.loads(event["raw"]) if isinstance(event["raw"], str) else event["raw"]
    assert event["quality_status"] == "ok"
    assert "source_mid_disagreement" not in quality_flags
    assert raw["quality"]["peer"]["crosscheck_skipped"] == "stale_peer_snapshot"


def test_options_radar_tables_load_through_panel_contract(tmp_path) -> None:
    db_path = tmp_path / "panel-radar.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "TSLA", slope=0.12)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-02T19:00:00Z', 102, 1, 1, 'USD', 'tradingview', '{}')"
        )
        store_options_chain(
            con,
            "TSLA",
            "2026-06-02T19:00:00Z",
            [
                option_row("2027-09-18", 120, "call", 2.9, 3.0, 0.25, 0.30, "OPRA:TSLA270918C120", volume=25, open_interest=250),
                option_row("2027-09-18", 180, "call", 7.5, 8.5, 0.50, 0.30, "OPRA:TSLA270918C180", volume=25, open_interest=250),
            ],
        )
        refresh_options_radar(con, ["TSLA"])

    from app.data_access import load_panel_scope_data

    panel = load_panel_scope_data({"database": {"duckdb_path": str(db_path)}}, "options-radar")
    tables = panel.tables
    assert panel.rows("option_radar_summary")[0]["scanned_tickers_current"] == 1
    assert panel.rows("option_radar_summary")[0]["opportunity_tickers_current"] == 1
    assert "option_snapshot" not in tables
    assert "option_features" not in tables
    assert "stock_features" not in tables
    assert "radar_alert" in tables
    assert panel.rows("radar_alert")
    assert {row["state"] for row in panel.rows("candidate_event")} == {"FIRE"}
    assert panel.rows("candidate_event")[0]["strategy_version"] == DEFAULT_STRATEGY_VERSION
    assert panel.rows("candidate_event_mark")[0]["candidate_state"] in {"FIRE", "REJECT"}
    assert "candidate_event_attribution" in tables
    assert "shadow_trade" not in tables
    assert "radar_state_transition" not in tables


def test_options_radar_attributes_shadow_trade_return(tmp_path) -> None:
    db_path = tmp_path / "radar-attribution.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "TSLA", slope=0.12)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-02T19:00:00Z', 102, 1, 1, 'USD', 'tradingview', '{}')"
        )
        con.execute(
            "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-03T19:00:00Z', 112, 1, 1, 'USD', 'tradingview', '{}')"
        )
        store_options_chain(
            con,
            "TSLA",
            "2026-06-02T19:00:00Z",
            [option_row("2027-09-18", 120, "call", 2.9, 3.0, 0.25, 0.30, "OPRA:TSLA270918C120", volume=25, open_interest=250)],
        )
        store_options_chain(
            con,
            "TSLA",
            "2026-06-03T19:00:00Z",
            [option_row("2027-09-18", 120, "call", 9.0, 10.0, 0.28, 0.38, "OPRA:TSLA270918C120", volume=45, open_interest=275)],
        )

        result = refresh_options_radar(con, ["TSLA"])
        attribution = query_rows(con, "SELECT * FROM option_attribution")[0]
        candidate_attribution = query_rows(con, "SELECT * FROM candidate_event_attribution WHERE strategy_version = ?", [DEFAULT_STRATEGY_VERSION])[0]
        first_trade = query_rows(con, "SELECT * FROM shadow_trade ORDER BY entry_time LIMIT 1")[0]
        candidate_marks = query_rows(con, "SELECT * FROM candidate_event_mark WHERE strategy_version = ? ORDER BY mark_time", [DEFAULT_STRATEGY_VERSION])
        marks = query_rows(con, "SELECT * FROM shadow_trade_mark ORDER BY mark_time")
        transitions = query_rows(
            con,
            """
            SELECT state, previous_state, trigger_reason
            FROM radar_state_transition
            WHERE contract_id = 'OPRA:TSLA270918C120'
              AND strategy_version = ?
            ORDER BY snapshot_time
            """,
            [DEFAULT_STRATEGY_VERSION],
        )
        cohorts = query_rows(con, "SELECT * FROM strategy_cohort_result")
        setup_cohort = query_rows(con, "SELECT * FROM strategy_cohort_result WHERE cohort_type = 'setup_type'")[0]
        market_cohort = query_rows(con, "SELECT * FROM strategy_cohort_result WHERE cohort_value = 'qqq_above_200d'")[0]

    assert result["option_attributions"] == 1
    assert result["candidate_event_attributions"] >= 1
    assert result["candidate_event_marks"] >= 3
    assert result["shadow_trade_marks"] == 2
    assert result["radar_state_transitions"] >= 2
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
            "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-02T19:00:00Z', 102, 1, 1, 'USD', 'tradingview', '{}')"
        )
        store_options_chain(
            con,
            "TSLA",
            "2026-06-02T19:00:00Z",
            [
                option_row("2027-09-18", 120, "call", 2.9, 3.0, 0.25, 0.30, "OPRA:TSLA270918C120", volume=25, open_interest=250),
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
              AND strategy_version = ?
            ORDER BY snapshot_time
            """,
            [DEFAULT_STRATEGY_VERSION],
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
            "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-02T19:00:00Z', 102, 1, 1, 'USD', 'tradingview', '{}')"
        )
        store_options_chain(
            con,
            "TSLA",
            "2026-06-02T19:00:00Z",
            [
                option_row("2027-09-18", 120, "call", 1.95, 2.05, 0.25, 0.30, "OPRA:TSLA270918C120", volume=25, open_interest=250),
                option_row("2027-09-18", 130, "call", 1.95, 2.05, 0.25, 0.30, "OPRA:TSLA270918C130", volume=25, open_interest=250),
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
        missed = query_rows(con, "SELECT * FROM missed_winner_event WHERE strategy_version = ?", [DEFAULT_STRATEGY_VERSION])[0]
        missed_raw = json.loads(missed["raw"]) if isinstance(missed["raw"], str) else missed["raw"]
        proposal = query_rows(con, "SELECT * FROM strategy_mutation_proposal WHERE strategy_version = ?", [DEFAULT_STRATEGY_VERSION])[0]
        backtest = query_rows(con, "SELECT * FROM strategy_backtest_result WHERE strategy_version = ?", [DEFAULT_STRATEGY_VERSION])[0]
        forward = query_rows(con, "SELECT * FROM strategy_forward_test_result WHERE strategy_version = ?", [DEFAULT_STRATEGY_VERSION])[0]
        candidate_marks = query_rows(con, "SELECT * FROM candidate_event_mark WHERE strategy_version = ? ORDER BY mark_time", [DEFAULT_STRATEGY_VERSION])
        candidate_attribution = query_rows(con, "SELECT * FROM candidate_event_attribution WHERE strategy_version = ?", [DEFAULT_STRATEGY_VERSION])[0]
        trades = query_rows(
            con,
            """
            SELECT st.*
            FROM shadow_trade st
            JOIN candidate_event ce ON ce.event_id = st.event_id
            WHERE ce.strategy_version = ?
            """,
            [DEFAULT_STRATEGY_VERSION],
        )

    assert result["candidate_event_marks"] >= 3
    assert result["candidate_event_attributions"] >= 1
    assert result["missed_winners"] >= 1
    assert result["strategy_mutation_proposals"] >= 1
    assert result["strategy_backtests"] >= 1
    assert result["strategy_forward_tests"] >= 1
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
    assert missed_raw["outcome_basis"] == "trailing_stop_realized_exit"
    assert missed_raw["observed_window"]["snapshot_count"] == 2
    assert missed_raw["candidate_context"]["first_state"] == "REJECT"
    assert missed_raw["candidate_context"]["first_filter_reason"] == "delta_outside_strategy_range"
    assert missed_raw["entry_quality"]["volume"] == 25
    assert missed_raw["winner_quality"]["open_interest"] == 450
    assert missed_raw["return_path"][0]["return"] == 0
    assert missed_raw["return_path"][-1]["return"] >= 9.0
    # Honest validation: a single synthetic missed winner gives the walk-forward
    # backtest no historical candidate base, so the proposal correctly fails the
    # backtest gate rather than advancing. The gating metadata still records that a
    # backtest, a forward shadow test and human approval are all required, and the
    # forward shadow test still spins up to collect real out-of-sample data.
    assert proposal["status"] == "backtest_failed"
    assert proposal["requires_backtest"] is True
    assert proposal["requires_forward_test"] is True
    assert proposal["human_approval_status"] == "required"
    assert "lower-delta" in proposal["proposed_parameter_changes"]
    assert backtest["verdict"] == "fail"
    assert backtest["baseline_candidate_count"] == 0
    assert backtest["proposed_candidate_count"] == 0
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

        # Honest validation fails the backtest on this synthetic single-winner
        # fixture, so promotion is blocked at the backtest gate first.
        with pytest.raises(StrategyPromotionError, match="backtest"):
            promote_strategy_mutation(con, proposal_id, approved_by="joe")

        # Force the backtest to pass; the forward shadow test gate now blocks.
        con.execute(
            "UPDATE strategy_backtest_result SET verdict = 'pass' WHERE proposal_id = ?",
            [proposal_id],
        )
        with pytest.raises(StrategyPromotionError, match="forward shadow test"):
            promote_strategy_mutation(con, proposal_id, approved_by="joe")

        # Force the forward test to pass; human approval is the final gate.
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
        refresh_options_radar(con, ["RBLX"])
        strategy = query_rows(con, "SELECT strategy_version, status, supersedes FROM option_strategy_versions WHERE strategy_version = ?", [promoted])[0]
        proposal = query_rows(
            con,
            """
            SELECT status, human_approval_status, approved_by, approved_at
            FROM strategy_mutation_proposal
            WHERE proposal_id = ?
            """,
            [proposal_id],
        )[0]

    assert promoted == "leap_10x_momentum_lottery__delta_max_delta_min"
    assert strategy == {"strategy_version": promoted, "status": "promoted", "supersedes": DEFAULT_STRATEGY_VERSION}
    assert proposal["status"] == "promoted"
    assert proposal["human_approval_status"] == "approved"
    assert proposal["approved_by"] == "joe"
    assert proposal["approved_at"]


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


def seed_extreme_opportunity_candidates(con) -> None:
    seed_prices(con, "TSLA", slope=0.12)
    seed_prices(con, "QQQ", start_price=100, slope=0.02)
    con.execute(
        "INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-02T19:00:00Z', 102, 1, 1, 'USD', 'tradingview', '{}')"
    )
    store_options_chain(
        con,
        "TSLA",
        "2026-06-02T19:00:00Z",
        [
            option_row("2027-09-18", 115, "call", 0.95, 1.05, 0.20, 0.30, "OPRA:TSLA270918C115", volume=300, open_interest=1200),
            option_row("2027-09-18", 120, "call", 4.3, 4.7, 0.25, 0.30, "OPRA:TSLA270918C120", volume=120, open_interest=800),
            option_row("2027-09-18", 180, "call", 7.5, 8.5, 0.50, 0.50, "OPRA:TSLA270918C180", volume=120, open_interest=800),
        ],
    )


def seed_source_signal(con, signal_id: str, source_item_id: str) -> None:
    seed_source_signal_for_symbol(con, "TSLA", signal_id, source_item_id)


def seed_source_signal_for_symbol(con, symbol: str, signal_id: str, source_item_id: str) -> None:
    con.execute(
        """
        INSERT INTO ticker_source_signals
        (id, source_item_id, source_id, symbol, observed_at, signal_type,
         sentiment, direction, confidence, thesis, antithesis, catalysts,
         risks, invalidation, evidence_refs, needs_market_context, raw)
        VALUES (?, ?, 'test_research', ?, '2026-06-02T19:00:00Z',
         'catalyst', 'positive', 'bullish', 0.92,
         'delivery recovery and margin stabilization create a path to a sharp repricing',
         'pricing pressure remains the bear case',
         '[{"type":"earnings","what_to_watch":"margin recovery"}]',
         '["pricing pressure"]',
         'stock loses 50D reclaim and source thesis fails',
         '[{"type":"source_item","id":"source-tsla"}]',
         false,
         '{}')
        """,
        [signal_id, source_item_id, symbol],
    )


def seed_validated_option_thesis(con, event_id: str, snapshot_time: str) -> None:
    con.execute(
        """
        INSERT INTO agent_thesis_validation
        (validation_id, thesis_id, ticker, strategy_version, validation_date,
         candidate_event_id, candidate_snapshot_time, validated_at, state,
         reason, option_still_valid, stock_progress, iv_status, candidate_state,
         proof_status, catalyst_status, invalidation_status, evidence_status,
         red_team_status, red_team_flags, evidence_refs, raw)
        VALUES (
         'validation-tsla-exceptional', 'thesis-tsla-exceptional', 'TSLA', ?,
         '2026-06-02', ?, TRY_CAST(? AS TIMESTAMP), '2026-06-02T20:10:00Z',
         'validated', 'source-backed catalyst and option math remain aligned',
         true, 'progressing', 'acceptable', 'FIRE',
         'supported', 'scheduled', 'clear', 'source_backed',
         'clear', '[]', '[{"type":"source_item","id":"source-tsla"}]', '{}'
        )
        """,
        [DEFAULT_STRATEGY_VERSION, event_id, snapshot_time],
    )


def option_row(
    expiry: str,
    strike: float,
    option_type: str,
    bid: float,
    ask: float,
    iv: float,
    delta: float | None,
    symbol: str,
    *,
    dte: int = 473,
    gamma: float | None = 0.01,
    theta: float | None = -0.01,
    vega: float | None = 0.2,
    volume: int | None = None,
    open_interest: int | None = None,
) -> dict[str, object]:
    return {
        "expiry": expiry,
        "dte": dte,
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
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "symbol": symbol,
    }


def opportunity_detail(
    contract_id: str,
    *,
    expiration: str,
    dte: int,
    ev_multiple: float,
    p_2x: float,
    p_5x: float,
    p_10x: float,
    conviction_score: float = 75.0,
    data_contract_status: str = "ready",
    tier: str = "Research",
) -> dict[str, object]:
    return {
        "event_id": f"event-{contract_id}",
        "snapshot_time": "2026-06-17T17:50:06",
        "ticker": "TEST",
        "contract_id": contract_id,
        "state": "SETUP",
        "tier": tier,
        "conviction_score": conviction_score,
        "entry_quality_score": 78.0,
        "survivability_score": 78.0,
        "required_move_pct": 1.5,
        "premium_mid": 5.0,
        "buy_under": 8.0,
        "data_contract_status": data_contract_status,
        "raw": {
            "expiration": expiration,
            "dte": dte,
            "spread_pct": 0.08,
            "open_interest": 600,
            "volume": 20,
            "ev": {
                "ev_multiple": ev_multiple,
                "p_2x": p_2x,
                "p_5x": p_5x,
                "p_10x": p_10x,
            },
        },
    }


def test_candidate_event_marks_rebuild_incrementally(tmp_path) -> None:
    db_path = tmp_path / "radar-incremental-marks.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_prices(con, "TSLA", slope=0.12)
        seed_prices(con, "QQQ", start_price=100, slope=0.02)
        con.execute("INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-02T19:00:00Z', 102, 1, 1, 'USD', 'tradingview', '{}')")
        store_options_chain(
            con,
            "TSLA",
            "2026-06-02T19:00:00Z",
            [option_row("2027-09-18", 120, "call", 2.9, 3.0, 0.25, 0.30, "OPRA:TSLA270918C120", volume=25, open_interest=250)],
        )
        first = refresh_options_radar(con, ["TSLA"])
        marks_after_first = query_rows(con, "SELECT count(*) AS c FROM candidate_event_mark")[0]["c"]

        # No new option data -> the learning pass must not re-mark anything, but the
        # already-built marks stay in place (no DELETE-all churn).
        second = refresh_options_radar(con, ["TSLA"])
        marks_after_second = query_rows(con, "SELECT count(*) AS c FROM candidate_event_mark")[0]["c"]

        # A fresh snapshot lands -> only the contract with new data is re-marked.
        con.execute("INSERT INTO quotes_intraday VALUES ('TSLA', '2026-06-03T19:00:00Z', 112, 1, 1, 'USD', 'tradingview', '{}')")
        store_options_chain(
            con,
            "TSLA",
            "2026-06-03T19:00:00Z",
            [option_row("2027-09-18", 120, "call", 9.0, 10.0, 0.28, 0.38, "OPRA:TSLA270918C120", volume=45, open_interest=275)],
        )
        third = refresh_options_radar(con, ["TSLA"])

    assert first["candidate_event_marks"] >= 1
    assert second["candidate_event_marks"] == 0  # nothing new to mark
    assert marks_after_second == marks_after_first  # existing marks preserved
    assert third["candidate_event_marks"] >= 1  # the updated contract re-marked
