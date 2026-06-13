"""build_candidate_event now prices each contract with the EV engine (Phase 1)."""

from __future__ import annotations

import json

from investment_panel.core.options_radar import (
    DEFAULT_STRATEGY_PARAMETERS,
    DEFAULT_STRATEGY_VERSION,
    _setup_score,
    build_candidate_event,
)


def _fire_row(**overrides) -> dict:
    row = {
        "snapshot_time": "2026-06-10T14:00:00",
        "contract_id": "NVDA280119C00200000",
        "ticker": "NVDA",
        "option_type": "call",
        "underlying_price": 130.0,
        "strike": 200.0,
        "mid": 6.0,
        "dte": 580,
        "delta": 0.30,
        "iv": 0.55,
        "spread_pct": 0.08,
        "open_interest": 5000,
        "volume": 250,
        "iv_percentile": 45.0,
        "required_move_10x_pct": 2.0,
        "required_10x_price": 0.0,
        "price": 130.0,
        "ma_50": 120.0,
        "rs_vs_qqq_20d": 0.05,
        "expiration": "2028-01-19",
        "stock_features_raw": json.dumps({"rv_60d": 0.50}),
    }
    row.update(overrides)
    return row


def test_candidate_event_attaches_ev_and_uses_ev_buy_under():
    event = build_candidate_event(_fire_row(), DEFAULT_STRATEGY_VERSION, DEFAULT_STRATEGY_PARAMETERS)
    assert event is not None
    ev = event["raw"]["ev"]
    assert ev is not None
    # All EV outputs present and ordered.
    assert ev["ev_multiple"] > 0
    assert 0.0 <= ev["p_10x"] <= ev["p_5x"] <= ev["p_2x"] <= 1.0
    assert "conviction_ev" in ev
    assert ev["scenario_curve"]  # for the Phase 4 payoff diagram
    # buy_under is the EV-inverse limit price, not the old linear cap.
    assert event["buy_under"] is not None and event["buy_under"] > 0


def test_ev_asymmetry_raises_score_for_cheaper_premium():
    cheap = build_candidate_event(_fire_row(mid=4.0), DEFAULT_STRATEGY_VERSION, DEFAULT_STRATEGY_PARAMETERS)
    rich = build_candidate_event(_fire_row(mid=9.0), DEFAULT_STRATEGY_VERSION, DEFAULT_STRATEGY_PARAMETERS)
    assert cheap is not None and rich is not None
    # A cheaper premium is a more asymmetric bet -> higher EV multiple -> higher score.
    assert cheap["raw"]["ev"]["ev_multiple"] > rich["raw"]["ev"]["ev_multiple"]
    assert cheap["score"] >= rich["score"]


def test_missing_iv_skips_ev_but_still_builds_event():
    event = build_candidate_event(_fire_row(iv=None), DEFAULT_STRATEGY_VERSION, DEFAULT_STRATEGY_PARAMETERS)
    assert event is not None
    # Falls back gracefully: no EV block, linear buy_under still set.
    assert event["raw"]["ev"] is None
    assert event["buy_under"] is not None


def test_catalyst_within_dte_sets_days_to_earnings_and_positive():
    # Earnings 40 days out, contract has 580 DTE -> catalyst is inside the window.
    event = build_candidate_event(
        _fire_row(next_earnings_date="2026-07-20"), DEFAULT_STRATEGY_VERSION, DEFAULT_STRATEGY_PARAMETERS
    )
    assert event is not None
    assert event["raw"]["days_to_earnings"] == 40
    assert "catalyst_within_dte" in event["trigger_reason"]


def test_no_earnings_date_leaves_days_to_earnings_none():
    event = build_candidate_event(_fire_row(), DEFAULT_STRATEGY_VERSION, DEFAULT_STRATEGY_PARAMETERS)
    assert event is not None
    assert event["raw"]["days_to_earnings"] is None
    assert "catalyst_within_dte" not in event["trigger_reason"]


def test_setup_score_is_continuous_and_ranks_setups():
    strong = {"price": 99.0, "breakout_level": 100.0, "base_length_days": 100.0,
              "volume_ratio": 0.7, "rs_vs_qqq_20d": 0.08, "rs_vs_qqq_60d": 0.06, "atr_pct": 0.02}
    weak = {"price": 80.0, "breakout_level": 100.0, "base_length_days": 5.0,
            "volume_ratio": 1.3, "rs_vs_qqq_20d": -0.05, "rs_vs_qqq_60d": -0.04, "atr_pct": 0.08}
    s_strong, s_weak = _setup_score(strong), _setup_score(weak)
    assert s_strong > s_weak
    assert 0.0 <= s_weak < s_strong <= 100.0
    # Not the old binary 100/45: a near-breakout setup lands between.
    assert s_strong not in (45.0, 100.0)


def test_setup_score_falls_back_to_binary_without_features():
    assert _setup_score({"price": 100.0, "ma_50": 90.0}) == 100.0  # above MA50
    assert _setup_score({"price": 80.0, "ma_50": 90.0}) == 45.0    # below MA50
