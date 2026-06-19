from __future__ import annotations

from datetime import datetime, timezone

from investment_panel.core.options_radar import (
    DEFAULT_STRATEGY_PARAMETERS,
    DEFAULT_STRATEGY_VERSION,
    EXCEPTIONAL_CONVICTION_BAR,
    acknowledge_radar_alert,
    build_candidate_event,
    display_snapshot_time,
    market_session,
    refresh_option_features,
    refresh_options_radar,
    refresh_radar_alerts,
    snapshot_is_rth,
    tier_rank,
)

# 2026-06-09 is a Tuesday. EDT = UTC-4, so RTH (09:30-16:00 ET) is 13:30-20:00 UTC.
RTH_UTC = "2026-06-09T15:00:00"  # 11:00 ET, regular hours
CLOSED_UTC = "2026-06-09T02:00:00"  # 22:00 ET Monday, market closed


def test_market_session_rth_vs_closed() -> None:
    assert market_session(datetime(2026, 6, 9, 15, 0, tzinfo=timezone.utc)) == "rth"
    assert market_session(datetime(2026, 6, 9, 2, 0, tzinfo=timezone.utc)) == "closed"
    # Saturday is always closed
    assert market_session(datetime(2026, 6, 13, 15, 0, tzinfo=timezone.utc)) == "closed"


def test_snapshot_is_rth_handles_naive_and_zulu() -> None:
    assert snapshot_is_rth(RTH_UTC) is True
    assert snapshot_is_rth("2026-06-09T15:00:00Z") is True
    assert snapshot_is_rth(CLOSED_UTC) is False
    assert snapshot_is_rth("") is False


def test_display_snapshot_freezes_on_last_rth_when_closed() -> None:
    times = [RTH_UTC, "2026-06-09T20:30:00"]  # an RTH snapshot then an after-hours one
    # Market closed now -> freeze on the last regular-hours snapshot.
    frozen = display_snapshot_time(times, now=datetime(2026, 6, 9, 21, 0, tzinfo=timezone.utc))
    assert frozen == RTH_UTC
    # During RTH -> show the newest snapshot.
    live = display_snapshot_time([RTH_UTC, "2026-06-09T15:30:00"], now=datetime(2026, 6, 9, 15, 45, tzinfo=timezone.utc))
    assert live == "2026-06-09T15:30:00"


def _candidate_row(snapshot_time: str, volume: float) -> dict:
    return {
        "snapshot_time": snapshot_time,
        "contract_id": "NVDA-2027-CALL",
        "ticker": "NVDA",
        "mid": 5.0,
        "underlying_price": 100.0,
        "strike": 150.0,
        "required_move_10x_pct": 2.0,
        "option_type": "call",
        "dte": 540,
        "delta": 0.30,
        "spread_pct": 0.10,
        "open_interest": 500.0,
        "volume": volume,
        "iv_percentile": 50.0,
        "price": 100.0,
        "ma_50": 90.0,
        "rs_vs_qqq_20d": 1.0,
        "required_10x_price": 150.0,
    }


def test_off_hours_candidate_uses_oi_not_volume_and_is_indicative() -> None:
    event = build_candidate_event(_candidate_row(CLOSED_UTC, volume=0.0), "v1", DEFAULT_STRATEGY_PARAMETERS)
    blockers = event["raw"]["blockers"]
    positives = event["raw"]["positives"]
    assert "off_hours_indicative" in blockers
    assert "off_hours_oi_liquidity" in positives  # OI (500) >= min_open_interest
    assert "volume_below_threshold" not in blockers  # volume gate not applied off-hours
    assert "missing_volume" not in blockers
    assert event["state"] != "FIRE"  # indicative, never trade-ready off-hours


def test_rth_candidate_still_enforces_volume_gate() -> None:
    event = build_candidate_event(_candidate_row(RTH_UTC, volume=0.0), "v1", DEFAULT_STRATEGY_PARAMETERS)
    blockers = event["raw"]["blockers"]
    assert "volume_below_threshold" in blockers  # RTH volume=0 still penalized
    assert "off_hours_indicative" not in blockers


def test_delayed_feed_rth_leans_on_oi_not_volume() -> None:
    # A delayed IBKR feed prints volume=0 during RTH; it must not be treated as a
    # real liquidity failure. Lean on OI and mark the candidate indicative instead.
    row = {**_candidate_row(RTH_UTC, volume=0.0), "raw": '{"market_data": "delayed"}'}
    event = build_candidate_event(row, "v1", DEFAULT_STRATEGY_PARAMETERS)
    blockers = event["raw"]["blockers"]
    positives = event["raw"]["positives"]
    assert "delayed_indicative" in blockers
    assert "delayed_oi_liquidity" in positives  # OI (500) >= min_open_interest
    assert "volume_below_threshold" not in blockers  # delayed volume gate not applied
    assert "missing_volume" not in blockers
    assert event["state"] != "FIRE"  # indicative until real-time volume confirms


def test_delayed_feed_with_printed_volume_keeps_volume_credit() -> None:
    # When the delayed feed does print real volume, it should still earn volume_seen
    # rather than being demoted to indicative.
    row = {**_candidate_row(RTH_UTC, volume=250.0), "raw": '{"market_data": "delayed"}'}
    event = build_candidate_event(row, "v1", DEFAULT_STRATEGY_PARAMETERS)
    blockers = event["raw"]["blockers"]
    positives = event["raw"]["positives"]
    assert "volume_seen" in positives
    assert "delayed_indicative" not in blockers


def test_service_bug_tier_ranks_after_clean_watch() -> None:
    assert tier_rank("Exceptional") < tier_rank("Research") < tier_rank("Watch") < tier_rank("Service Bug")


def test_radar_alerts_load_and_do_not_refire_after_ack(tmp_path) -> None:
    from investment_panel.core.db import db, init_db, query_rows

    db_path = tmp_path / "radar-alerts.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            """
            INSERT INTO option_radar_opportunity
                (opportunity_id, snapshot_time, ticker, strategy_version, tier,
                 primary_event_id, primary_contract_id, primary_state,
                 conviction_score, premium_mid, buy_under, data_contract_status)
            VALUES
                ('opp-1', TIMESTAMP '2026-06-09 15:00:00', 'NVDA', ?, 'Exceptional',
                 'event-1', 'OPRA:NVDA270918C150', 'FIRE',
                 ?, 4.5, 5.0, 'ready')
            """,
            [DEFAULT_STRATEGY_VERSION, EXCEPTIONAL_CONVICTION_BAR],
        )

        first_count = refresh_radar_alerts(con)
        alerts = query_rows(con, "SELECT alert_id, alert_type, acknowledged_at FROM radar_alert ORDER BY alert_type")
        assert first_count == 2
        assert {row["alert_type"] for row in alerts} == {"buy_under_hit", "exceptional_conviction"}

        assert acknowledge_radar_alert(con, alerts[0]["alert_id"]) is True
        second_count = refresh_radar_alerts(con)
        acknowledged = query_rows(con, "SELECT count(*) AS count FROM radar_alert WHERE acknowledged_at IS NOT NULL")[0]
        assert second_count == 0
        assert acknowledged["count"] == 1


def test_radar_alerts_auto_resolve_when_condition_clears(tmp_path) -> None:
    from investment_panel.core.db import db, init_db, query_rows

    db_path = tmp_path / "radar-alerts-resolve.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            """
            INSERT INTO option_radar_opportunity
                (opportunity_id, snapshot_time, ticker, strategy_version, tier,
                 primary_event_id, primary_contract_id, primary_state,
                 conviction_score, premium_mid, buy_under, data_contract_status)
            VALUES
                ('opp-1', TIMESTAMP '2026-06-09 15:00:00', 'NVDA', ?, 'Exceptional',
                 'event-1', 'OPRA:NVDA270918C150', 'FIRE',
                 ?, 4.5, 5.0, 'ready')
            """,
            [DEFAULT_STRATEGY_VERSION, EXCEPTIONAL_CONVICTION_BAR],
        )
        assert refresh_radar_alerts(con) == 2

        con.execute(
            """
            UPDATE option_radar_opportunity
            SET tier = 'Watch', primary_state = 'WATCH', conviction_score = 40, premium_mid = 6.0
            WHERE opportunity_id = 'opp-1'
            """
        )
        assert refresh_radar_alerts(con) == 0
        active = query_rows(con, "SELECT count(*) AS count FROM radar_alert WHERE acknowledged_at IS NULL")[0]
        assert active["count"] == 0

        con.execute(
            """
            UPDATE option_radar_opportunity
            SET tier = 'Exceptional', primary_state = 'FIRE', conviction_score = ?, premium_mid = 4.5
            WHERE opportunity_id = 'opp-1'
            """,
            [EXCEPTIONAL_CONVICTION_BAR],
        )
        assert refresh_radar_alerts(con) == 2
        reactivated = query_rows(con, "SELECT count(*) AS count FROM radar_alert WHERE acknowledged_at IS NULL")[0]
        assert reactivated["count"] == 2


def test_fast_feature_refresh_keeps_historical_iv_rank(tmp_path) -> None:
    from investment_panel.core.db import db, init_db, query_rows

    db_path = tmp_path / "radar-fast-iv.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        for snapshot_time, iv in [("2026-06-02T15:00:00Z", 0.1), ("2026-06-03T15:00:00Z", 0.3)]:
            con.execute(
                """
                INSERT INTO option_snapshot
                    (snapshot_time, ticker, underlying_price, expiration, strike, option_type,
                     mid, iv, dte, data_source, contract_id, raw)
                VALUES (TRY_CAST(? AS TIMESTAMP), 'NVDA', 100, '2027-09-18', 150, 'call',
                        5, ?, 540, 'ibkr', 'OPRA:NVDA270918C150', '{}')
                """,
                [snapshot_time, iv],
            )

        assert refresh_option_features(con, symbols=["NVDA"], source="ibkr", snapshot_time="2026-06-03T15:00:00Z") == 1
        feature = query_rows(con, "SELECT iv_percentile, iv_rank FROM option_features WHERE snapshot_time = TRY_CAST('2026-06-03T15:00:00Z' AS TIMESTAMP)")[0]
        assert feature["iv_percentile"] == 100.0
        assert feature["iv_rank"] == 100.0


def test_targeted_radar_alert_refresh_does_not_resolve_other_tickers(tmp_path) -> None:
    from investment_panel.core.db import db, init_db, query_rows

    db_path = tmp_path / "radar-alerts-scoped.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            """
            INSERT INTO option_radar_opportunity
                (opportunity_id, snapshot_time, ticker, strategy_version, tier,
                 primary_event_id, primary_contract_id, primary_state,
                 conviction_score, premium_mid, buy_under, data_contract_status)
            VALUES
                ('opp-nvda', TIMESTAMP '2026-06-09 15:00:00', 'NVDA', ?, 'Exceptional',
                 'event-nvda', 'OPRA:NVDA270918C150', 'FIRE', ?, 4.5, 5.0, 'ready'),
                ('opp-amd', TIMESTAMP '2026-06-09 15:00:00', 'AMD', ?, 'Exceptional',
                 'event-amd', 'OPRA:AMD270918C150', 'FIRE', ?, 4.5, 5.0, 'ready')
            """,
            [DEFAULT_STRATEGY_VERSION, EXCEPTIONAL_CONVICTION_BAR, DEFAULT_STRATEGY_VERSION, EXCEPTIONAL_CONVICTION_BAR],
        )
        assert refresh_radar_alerts(con) == 4

        con.execute(
            """
            UPDATE option_radar_opportunity
            SET tier = 'Watch', primary_state = 'WATCH', conviction_score = 40, premium_mid = 6.0
            WHERE ticker = 'NVDA'
            """
        )
        assert refresh_radar_alerts(con, symbols=["NVDA"], resolve_all=False) == 0
        active = query_rows(con, "SELECT ticker, count(*) AS count FROM radar_alert WHERE acknowledged_at IS NULL GROUP BY ticker ORDER BY ticker")
        assert active == [{"ticker": "AMD", "count": 2}]


def test_source_scoped_full_universe_refresh_resolves_stale_alerts(tmp_path) -> None:
    from investment_panel.core.db import db, init_db, query_rows

    db_path = tmp_path / "radar-alerts-source-scope.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            """
            INSERT INTO option_radar_opportunity
                (opportunity_id, snapshot_time, ticker, strategy_version, tier,
                 primary_event_id, primary_contract_id, primary_state,
                 conviction_score, premium_mid, buy_under, data_contract_status)
            VALUES
                ('opp-nvda', TIMESTAMP '2026-06-09 15:00:00', 'NVDA', ?, 'Exceptional',
                 'event-nvda', 'OPRA:NVDA270918C150', 'FIRE', ?, 4.5, 5.0, 'ready')
            """,
            [DEFAULT_STRATEGY_VERSION, EXCEPTIONAL_CONVICTION_BAR],
        )
        assert refresh_radar_alerts(con) == 2
        con.execute(
            """
            UPDATE option_radar_opportunity
            SET tier = 'Watch', primary_state = 'WATCH', conviction_score = 40, premium_mid = 6.0
            WHERE ticker = 'NVDA'
            """
        )
        assert refresh_options_radar(con, symbols=None, source="ibkr", include_agent_work=False, include_learning=False)["radar_alerts"] == 0
        active = query_rows(con, "SELECT count(*) AS count FROM radar_alert WHERE acknowledged_at IS NULL")[0]
        assert active["count"] == 0


def test_opportunity_refresh_preserves_last_good_when_latest_builds_none(tmp_path) -> None:
    """A bad latest snapshot (all-REJECT candidates) must not blank the radar."""

    from investment_panel.core.db import db, init_db, query_rows
    from investment_panel.core.options_radar import refresh_option_radar_opportunities

    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        # Seed an existing good opportunity (the last regular-hours result).
        con.execute(
            """
            INSERT INTO option_radar_opportunity
                (opportunity_id, snapshot_time, ticker, strategy_version, tier, primary_state)
            VALUES ('opp-1', TIMESTAMP '2026-06-05 15:00:00', 'NVDA', ?, 'Research', 'FIRE')
            """,
            [DEFAULT_STRATEGY_VERSION],
        )
        # Latest candidate snapshot is all REJECT -> the refresh builds nothing.
        con.execute(
            """
            INSERT INTO candidate_event
                (event_id, snapshot_time, ticker, contract_id, strategy_version, state)
            VALUES ('ev-1', TIMESTAMP '2026-06-09 13:00:00', 'NVDA', 'c1', ?, 'REJECT')
            """,
            [DEFAULT_STRATEGY_VERSION],
        )
        built = refresh_option_radar_opportunities(con, strategy_version=DEFAULT_STRATEGY_VERSION)
        survivors = query_rows(con, "SELECT opportunity_id FROM option_radar_opportunity")

    assert built == 0  # nothing fresh built
    assert any(r["opportunity_id"] == "opp-1" for r in survivors)  # last-good preserved, not wiped


def test_explicit_opportunity_snapshot_refresh_preserves_other_snapshots(tmp_path, monkeypatch) -> None:
    import importlib

    from investment_panel.core.db import db, init_db, query_rows
    from investment_panel.core.options_radar import refresh_option_radar_opportunities

    opportunities_module = importlib.import_module("investment_panel.core.options_radar.opportunities")

    def fake_build_opportunity(con, ticker, candidate_rows, strategy_version, *, cohort_priors=None):
        snapshot_time = candidate_rows[0]["snapshot_time"]
        snapshot_id = snapshot_time.isoformat() if hasattr(snapshot_time, "isoformat") else str(snapshot_time)
        return {
            "opportunity_id": f"rebuilt-{ticker}-{snapshot_id}",
            "snapshot_time": snapshot_id,
            "ticker": ticker,
            "strategy_version": strategy_version,
            "tier": "Research",
            "primary_event_id": candidate_rows[0]["event_id"],
            "primary_contract_id": candidate_rows[0]["contract_id"],
            "primary_state": "SETUP",
            "conviction_score": 80.0,
            "asymmetry_score": 75.0,
            "entry_quality_score": 70.0,
            "catalyst_score": 65.0,
            "evidence_score": 60.0,
            "regime_score": 55.0,
            "survivability_score": 50.0,
            "learning_score": 45.0,
            "required_move_pct": 0.5,
            "premium_mid": 1.0,
            "premium_fill_assumption": 1.05,
            "required_10x_price": 125.0,
            "buy_under": 1.2,
            "entry_zone": "test",
            "max_loss_assumption": 1.05,
            "position_sizing_band": "test",
            "data_contract_status": "ready",
            "data_contract_failures": [],
            "data_contract_satisfied": [],
            "service_repair_jobs": [],
            "service_repair_summary": "",
            "why_now": "test",
            "kill_switch": "test",
            "top_reasons": [],
            "blockers": [],
            "quality_status": "ok",
            "quality_flags": [],
            "evidence_refs": [],
            "alternative_contracts": [],
            "raw": {},
        }

    monkeypatch.setattr(opportunities_module, "load_cohort_priors", lambda con, strategy_version: {})
    monkeypatch.setattr(opportunities_module, "build_option_radar_opportunity", fake_build_opportunity)

    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        con.execute(
            """
            INSERT INTO option_radar_opportunity
                (opportunity_id, snapshot_time, ticker, strategy_version, tier, primary_state)
            VALUES
                ('old-display-nvda', TIMESTAMP '2026-06-09 15:00:00', 'NVDA', ?, 'Watch', 'WATCH'),
                ('newer-amd', TIMESTAMP '2026-06-10 15:00:00', 'AMD', ?, 'Research', 'SETUP')
            """,
            [DEFAULT_STRATEGY_VERSION, DEFAULT_STRATEGY_VERSION],
        )
        con.execute(
            """
            INSERT INTO candidate_event
                (event_id, snapshot_time, ticker, contract_id, strategy_version, state)
            VALUES ('ev-nvda', TIMESTAMP '2026-06-09 15:00:00', 'NVDA', 'OPRA:NVDA270918C150', ?, 'SETUP')
            """,
            [DEFAULT_STRATEGY_VERSION],
        )

        built = refresh_option_radar_opportunities(
            con,
            strategy_version=DEFAULT_STRATEGY_VERSION,
            read_snapshot="2026-06-09T15:00:00Z",
        )
        opportunities = query_rows(con, "SELECT opportunity_id, ticker FROM option_radar_opportunity ORDER BY ticker, opportunity_id")

    assert built == 1
    assert opportunities == [
        {"opportunity_id": "newer-amd", "ticker": "AMD"},
        {"opportunity_id": "rebuilt-NVDA-2026-06-09T15:00:00", "ticker": "NVDA"},
    ]
