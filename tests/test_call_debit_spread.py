"""call_debit_spread_v1: synthetic SPREAD contracts through the radar pipeline (Phase 3).

The vertical is threaded as a single synthetic option_snapshot row priced at the net debit
(long_mid - short_mid) with netted greeks and option_type 'call_spread'. The feature/EV math
treats it as a long call at the long strike; the mark pipeline re-prices it from its legs
because persist_spread_snapshots re-writes the same deterministic contract_id every refresh.
"""

from __future__ import annotations

from investment_panel.core.options_radar import (
    STRATEGY_FAMILY_PRESETS,
    build_option_feature,
    build_spread_snapshot_row,
    generate_candidate_events,
    persist_spread_snapshots,
    refresh_option_features,
    register_default_strategy,
    register_strategy_families,
)

SPREAD = STRATEGY_FAMILY_PRESETS["call_debit_spread_v1"]

T0 = "2026-06-10T14:00:00"
T1 = "2026-06-11T14:00:00"


def _call(strike: float, mid: float, *, snapshot_time: str = T0, delta: float = 0.30,
          bid: float | None = None, ask: float | None = None, **overrides) -> dict:
    leg = {
        "snapshot_time": snapshot_time,
        "ticker": "NVDA",
        "underlying_price": 100.0,
        "expiration": "2027-01-15",
        "strike": strike,
        "option_type": "call",
        "bid": bid if bid is not None else mid - 0.1,
        "ask": ask if ask is not None else mid + 0.1,
        "mid": mid,
        "last": mid,
        "volume": 200.0,
        "open_interest": 5000.0,
        "iv": 0.50,
        "delta": delta,
        "gamma": 0.01,
        "theta": -0.02,
        "vega": 0.10,
        "dte": 200,
        "spread_pct": 0.04,
        "data_source": "ibkr",
        "contract_id": f"NVDA:2027-01-15:{strike:g}:call",
    }
    leg.update(overrides)
    return leg


# --- build_spread_snapshot_row -------------------------------------------------------


def test_build_spread_snapshot_row_nets_legs():
    spread = build_spread_snapshot_row(
        _call(100.0, 5.0, delta=0.40, bid=4.9, ask=5.1),
        _call(105.0, 3.0, delta=0.20, bid=2.9, ask=3.1),
    )
    assert spread is not None
    assert spread["option_type"] == "call_spread"
    assert spread["mid"] == 2.0          # net debit = 5.0 - 3.0
    assert spread["strike"] == 100.0     # long (lower) strike
    assert spread["delta"] == 0.40 - 0.20
    assert spread["bid"] == 4.9 - 3.1    # long bid - short ask (conservative)
    assert spread["ask"] == 5.1 - 2.9
    assert spread["contract_id"] == "NVDA:2027-01-15:100-105:call_spread"
    assert spread["raw"]["structure"] == "call_debit_spread"
    assert spread["raw"]["width"] == 5.0


def test_build_spread_snapshot_row_is_deterministic():
    a = build_spread_snapshot_row(_call(100.0, 5.0), _call(105.0, 3.0))
    b = build_spread_snapshot_row(_call(100.0, 5.0), _call(105.0, 3.0))
    assert a["contract_id"] == b["contract_id"]


def test_build_spread_snapshot_row_skips_non_debit():
    # short mid >= long mid is not a debit spread -> skipped.
    assert build_spread_snapshot_row(_call(100.0, 3.0), _call(105.0, 3.0)) is None
    assert build_spread_snapshot_row(_call(100.0, 2.0), _call(105.0, 3.0)) is None


def test_build_spread_snapshot_row_requires_higher_short_strike():
    assert build_spread_snapshot_row(_call(105.0, 5.0), _call(100.0, 3.0)) is None


# --- build_option_feature accepts the synthetic type ---------------------------------


def test_build_option_feature_accepts_call_spread():
    spread = build_spread_snapshot_row(_call(100.0, 5.0), _call(105.0, 3.0))
    feature = build_option_feature(spread, [])
    assert feature is not None
    # required_10x is computed off the net debit (2.0) at the long strike: 100 + 2*10.
    assert feature["required_10x_price"] == 100.0 + 2.0 * 10
    assert feature["breakeven"] == 100.0 + 2.0


# --- persist + end-to-end through the candidate/mark pipeline -------------------------


def _insert_call(con, leg: dict) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO option_snapshot
        (snapshot_time, ticker, underlying_price, expiration, strike, option_type, bid, ask, mid,
         last, volume, open_interest, iv, delta, gamma, theta, vega, dte, spread_pct,
         data_source, contract_id, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            leg["snapshot_time"], leg["ticker"], leg["underlying_price"], leg["expiration"],
            leg["strike"], leg["option_type"], leg["bid"], leg["ask"], leg["mid"], leg["last"],
            leg["volume"], leg["open_interest"], leg["iv"], leg["delta"], leg["gamma"],
            leg["theta"], leg["vega"], leg["dte"], leg["spread_pct"], leg["data_source"],
            leg["contract_id"], "{}",
        ],
    )


def test_persist_spread_snapshots_pairs_adjacent_calls(tmp_path):
    from investment_panel.core.db import db, init_db, query_rows

    init_db(tmp_path / "s.duckdb")
    with db(tmp_path / "s.duckdb") as con:
        _insert_call(con, _call(100.0, 5.0))
        _insert_call(con, _call(105.0, 3.0))
        written = persist_spread_snapshots(con)
        rows = query_rows(con, "SELECT * FROM option_snapshot WHERE option_type = 'call_spread'")

    assert written == 1
    assert len(rows) == 1
    assert rows[0]["contract_id"] == "NVDA:2027-01-15:100-105:call_spread"
    assert rows[0]["mid"] == 2.0


def test_spread_candidate_and_remark_through_pipeline(tmp_path):
    from investment_panel.core.db import db, init_db, query_rows

    init_db(tmp_path / "e.duckdb")
    with db(tmp_path / "e.duckdb") as con:
        # Same two-leg vertical at t0 (net debit 2.0) and t1 (net debit 3.0 -> +50%).
        _insert_call(con, _call(100.0, 5.0, snapshot_time=T0, delta=0.40, bid=4.9, ask=5.1))
        _insert_call(con, _call(105.0, 3.0, snapshot_time=T0, delta=0.20, bid=2.9, ask=3.1))
        _insert_call(con, _call(100.0, 6.0, snapshot_time=T1, delta=0.45, bid=5.9, ask=6.1,
                                underlying_price=102.0, dte=199))
        _insert_call(con, _call(105.0, 3.0, snapshot_time=T1, delta=0.22, bid=2.9, ask=3.1,
                                underlying_price=102.0, dte=199))
        register_default_strategy(con)
        register_strategy_families(con)
        persist_spread_snapshots(con)
        refresh_option_features(con)
        generate_candidate_events(con, strategy_version="call_debit_spread_v1")

        from investment_panel.core.options_radar import refresh_candidate_event_marks

        refresh_candidate_event_marks(con, strategy_version="call_debit_spread_v1")

        candidates = query_rows(
            con,
            """
            SELECT * FROM candidate_event
            WHERE strategy_version = 'call_debit_spread_v1'
              AND contract_id = 'NVDA:2027-01-15:100-105:call_spread'
            ORDER BY snapshot_time
            """,
        )
        marks = query_rows(
            con,
            """
            SELECT * FROM candidate_event_mark
            WHERE strategy_version = 'call_debit_spread_v1'
              AND contract_id = 'NVDA:2027-01-15:100-105:call_spread'
            ORDER BY mark_time
            """,
        )

    # A candidate exists for the synthetic spread, priced at the net debit.
    assert candidates, "expected a call_debit_spread candidate for the synthetic contract"
    assert candidates[0]["premium_mid"] == 2.0
    assert candidates[0]["raw"] and '"option_type": "call_spread"' in candidates[0]["raw"]
    # The t0 candidate (alert_time == t0) re-marks at t1 from the leg-derived net debit
    # (2.0 -> 3.0): proves the synthetic spread threads through the mark pipeline.
    t0_marks = [m for m in marks if str(m["alert_time"]).startswith("2026-06-10")]
    assert t0_marks
    final = sorted(t0_marks, key=lambda m: m["mark_time"])[-1]
    assert str(final["mark_time"]).startswith("2026-06-11")
    entry = 2.0 * (1 + float(SPREAD["fill_slippage_pct"]))  # premium_fill_assumption
    assert abs(final["current_return"] - (3.0 / entry - 1)) < 1e-6
