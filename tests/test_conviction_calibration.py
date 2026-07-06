"""Probability calibration of predicted P(2x) vs realized outcomes (Phase 2a)."""

from __future__ import annotations

import json

import pytest

from investment_panel.core.options_radar import (
    DEFAULT_STRATEGY_VERSION,
    build_conviction_calibration,
    calibrated_p2x,
    load_conviction_calibration,
    refresh_conviction_calibration,
)


def test_build_calibration_bins_and_monotone_map():
    # Predicted 0.1 rarely hits; predicted 0.8 usually hits -> monotone realized map.
    samples = []
    samples += [(0.1, 0, 0)] * 18 + [(0.1, 1, 0)] * 2   # ~10% realized
    samples += [(0.8, 1, 0)] * 16 + [(0.8, 0, 0)] * 4   # ~80% realized
    bins, mapping, calibrated = build_conviction_calibration(samples)
    assert calibrated is True  # 40 mature obs >= 30
    realized = [b["realized_p2x"] for b in bins]
    assert realized == sorted(realized)  # monotone non-decreasing across bins
    ys = [y for _x, y in mapping]
    assert ys == sorted(ys)
    # Each bin carries a Wilson interval.
    assert all(b["wilson_lo"] <= b["realized_p2x"] <= b["wilson_hi"] for b in bins)


def test_uncalibrated_until_min_mature():
    bins, mapping, calibrated = build_conviction_calibration([(0.5, 1, 0)] * 10)
    assert calibrated is False  # only 10 obs


def test_calibrated_p2x_is_identity_when_uncalibrated():
    assert calibrated_p2x(0.42, None) == 0.42
    assert calibrated_p2x(0.42, {"calibrated": False, "calibration_map": []}) == 0.42
    # When calibrated, maps through the table.
    cal = {"calibrated": True, "calibration_map": [(0.2, 0.1), (0.8, 0.9)]}
    assert calibrated_p2x(0.5, cal) == pytest.approx(0.5)  # midpoint of the linear map


def test_refresh_calibration_end_to_end_requires_maturity(tmp_path):
    from investment_panel.core.db import db, init_db

    init_db(tmp_path / "c.duckdb")
    with db(tmp_path / "c.duckdb") as con:
        # 35 mature events (snapshot 2026-01-01, mark 2026-05-01 -> ~120d observed).
        for i in range(35):
            eid = f"ev{i}"
            predicted = 0.1 if i % 2 == 0 else 0.7
            con.execute(
                "INSERT INTO candidate_event (event_id, snapshot_time, ticker, contract_id, strategy_version, state, raw) "
                "VALUES (?, '2026-01-01T14:00:00', 'NVDA', ?, ?, 'FIRE', ?)",
                [eid, f"c{i}", DEFAULT_STRATEGY_VERSION, json.dumps({"ev": {"p_2x": predicted}})],
            )
            hit = 1 if (i % 2 == 1) else 0  # high-predicted ones hit
            con.execute(
                "INSERT INTO candidate_event_mark (mark_id, event_id, strategy_version, mark_time, max_return_since_alert, time_to_2x) "
                "VALUES (?, ?, ?, '2026-05-01T14:00:00', ?, ?)",
                [f"m{i}", eid, DEFAULT_STRATEGY_VERSION, 1.5 if hit else 0.2, 10 if hit else None],
            )
        written = refresh_conviction_calibration(con, strategy_version=DEFAULT_STRATEGY_VERSION)
        loaded = load_conviction_calibration(con, DEFAULT_STRATEGY_VERSION)

    assert written >= 2  # at least the two predicted-probability bins
    assert loaded["calibrated"] is True
    assert loaded["calibration_map"]  # non-empty monotone map


def test_refresh_skips_immature_events(tmp_path):
    from investment_panel.core.db import db, init_db

    init_db(tmp_path / "c2.duckdb")
    with db(tmp_path / "c2.duckdb") as con:
        # Mark only 5 days after snapshot -> immature, excluded.
        con.execute(
            "INSERT INTO candidate_event (event_id, snapshot_time, ticker, contract_id, strategy_version, state, raw) "
            "VALUES ('e1', '2026-01-01T14:00:00', 'NVDA', 'c1', ?, 'FIRE', ?)",
            [DEFAULT_STRATEGY_VERSION, json.dumps({"ev": {"p_2x": 0.5}})],
        )
        con.execute(
            "INSERT INTO candidate_event_mark (mark_id, event_id, strategy_version, mark_time, max_return_since_alert) "
            "VALUES ('m1', 'e1', ?, '2026-01-06T14:00:00', 2.0)",
            [DEFAULT_STRATEGY_VERSION],
        )
        refresh_conviction_calibration(con, strategy_version=DEFAULT_STRATEGY_VERSION)
        loaded = load_conviction_calibration(con, DEFAULT_STRATEGY_VERSION)

    assert loaded["calibrated"] is False  # no mature observations


def test_short_dated_events_calibrate_after_five_days(tmp_path):
    from investment_panel.core.db import db, init_db

    init_db(tmp_path / "short-cal.duckdb")
    with db(tmp_path / "short-cal.duckdb") as con:
        for i in range(20):
            eid = f"short{i}"
            predicted = 0.2 if i % 2 == 0 else 0.8
            hit = 1 if i % 2 == 1 else 0
            con.execute(
                "INSERT INTO candidate_event (event_id, snapshot_time, ticker, contract_id, strategy_version, state, raw) "
                "VALUES (?, '2026-01-01T14:00:00', 'NVDA', ?, ?, 'FIRE', ?)",
                [
                    eid,
                    f"short-c{i}",
                    DEFAULT_STRATEGY_VERSION,
                    json.dumps({"strategy_family": "short_dated_lottery_call", "dte": 21, "ev": {"p_2x": predicted}}),
                ],
            )
            con.execute(
                "INSERT INTO candidate_event_mark (mark_id, event_id, strategy_version, mark_time, max_return_since_alert, raw) "
                "VALUES (?, ?, ?, '2026-01-06T14:00:00', ?, ?)",
                [f"short-m{i}", eid, DEFAULT_STRATEGY_VERSION, 1.2 if hit else 0.1, json.dumps({"realized_exit_return": 1.2 if hit else 0.1})],
            )

        written = refresh_conviction_calibration(con, strategy_version=DEFAULT_STRATEGY_VERSION)
        loaded = load_conviction_calibration(con, DEFAULT_STRATEGY_VERSION)

    assert written >= 2
    assert loaded["calibrated"] is True
