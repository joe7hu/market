"""Vol-surface features: term structure, 25d skew, IV/RV, IV percentile (Phase 1c)."""

from __future__ import annotations

import json

from investment_panel.core.options_radar import (
    _expiry_atm_iv_and_skew,
    _iv_percentile_252d,
    build_vol_surface_feature,
    refresh_vol_surface_features,
)


def test_expiry_atm_iv_and_skew_reuses_intelligence_helpers():
    chain = [
        {"strike": 100.0, "option_type": "call", "iv": 0.40, "delta": 0.50},
        {"strike": 120.0, "option_type": "call", "iv": 0.35, "delta": 0.25},
        {"strike": 80.0, "option_type": "put", "iv": 0.50, "delta": -0.25},
    ]
    atm_iv, skew = _expiry_atm_iv_and_skew(chain, spot=100.0)
    assert atm_iv == 0.40  # ATM strike 100
    assert skew == round(0.50 - 0.35, 6)  # put_25 iv - call_25 iv


def test_build_vol_surface_term_slope_skew_and_iv_rv():
    per_expiry = [
        (30, 0.60, 0.04),   # front IV high -> inverted term
        (90, 0.45, 0.02),
        (540, 0.40, -0.05),  # leap: call skew (negative)
    ]
    feat = build_vol_surface_feature(
        "NVDA", "2026-06-10T14:00:00", per_expiry,
        rv_20d=0.35, rv_60d=0.50, iv_leap_history=[], skew_5d_ago=0.01,
    )
    assert feat is not None
    assert feat["atm_iv_30d"] == 0.60 and feat["atm_iv_leap"] == 0.40
    assert feat["term_slope"] == round(0.40 - 0.60, 6)  # negative -> inverted
    assert feat["put_call_skew_25d"] == -0.05  # leap skew (longest dte)
    assert feat["skew_change_5d"] == round(-0.05 - 0.01, 6)
    assert feat["iv_rv_ratio"] == round(0.40 / 0.50, 4)


def test_iv_percentile_falls_back_until_history_accrues():
    pct, basis = _iv_percentile_252d(0.40, [0.30, 0.35])  # <20 obs
    assert pct is None and basis == "insufficient_history"
    history = [0.20 + i * 0.01 for i in range(40)]  # 40 obs spanning 0.20..0.59
    pct, basis = _iv_percentile_252d(0.58, history)  # near the top of the range
    assert basis == "matched_tenor_252d" and pct is not None and pct >= 90.0


def test_refresh_vol_surface_end_to_end(tmp_path):
    from investment_panel.core.db import db, init_db, query_rows

    init_db(tmp_path / "v.duckdb")
    with db(tmp_path / "v.duckdb") as con:
        # Two expiries (front 30d, leap 540d), call+put each, with IV/delta.
        contracts = [
            ("2026-07-10", 30, "call", 100.0, 0.60, 0.50),
            ("2026-07-10", 30, "call", 120.0, 0.55, 0.25),
            ("2026-07-10", 30, "put", 80.0, 0.70, -0.25),
            ("2027-12-17", 540, "call", 100.0, 0.42, 0.50),
            ("2027-12-17", 540, "call", 130.0, 0.38, 0.25),
            ("2027-12-17", 540, "put", 75.0, 0.40, -0.25),
        ]
        for exp, dte, opt, strike, iv, delta in contracts:
            con.execute(
                "INSERT INTO option_snapshot (snapshot_time, ticker, contract_id, expiration, strike, option_type, iv, delta, dte, underlying_price, data_source) "
                "VALUES ('2026-06-10T14:00:00', 'NVDA', ?, ?, ?, ?, ?, ?, ?, 100.0, 'ibkr')",
                [f"NVDA{exp}{opt}{strike}", exp, strike, opt, iv, delta, dte],
            )
        con.execute(
            "INSERT INTO stock_features (snapshot_time, ticker, raw) VALUES ('2026-06-10T14:00:00', 'NVDA', ?)",
            [json.dumps({"rv_20d": 0.35, "rv_60d": 0.50})],
        )
        written = refresh_vol_surface_features(con)
        rows = query_rows(con, "SELECT * FROM vol_surface_features WHERE ticker = 'NVDA'")

    assert written == 1
    row = rows[0]
    assert row["atm_iv_30d"] == 0.60 and row["atm_iv_leap"] == 0.42
    assert row["term_slope"] < 0  # inverted front
    assert row["iv_rv_ratio"] is not None
    assert row["iv_percentile_basis"] == "insufficient_history"  # no prior history
