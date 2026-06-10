from __future__ import annotations

from datetime import datetime, timezone

from investment_panel.core.options_radar import (
    DEFAULT_STRATEGY_PARAMETERS,
    DEFAULT_STRATEGY_VERSION,
    build_candidate_event,
    display_snapshot_time,
    market_session,
    snapshot_is_rth,
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
