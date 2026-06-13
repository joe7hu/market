"""Strategy archetype families: family-aware gating + registration (Phase 3)."""

from __future__ import annotations

import json

from investment_panel.core.options_radar import (
    DEFAULT_STRATEGY_VERSION,
    STRATEGY_FAMILY_PRESETS,
    build_candidate_event,
    candidate_strategy_versions,
    register_strategy_families,
)

BREAKDOWN = STRATEGY_FAMILY_PRESETS["breakdown_put_v1"]
CATALYST = STRATEGY_FAMILY_PRESETS["catalyst_call_v1"]


def _put_row(**overrides) -> dict:
    row = {
        "snapshot_time": "2026-06-10T14:00:00",
        "contract_id": "NVDA_P1",
        "ticker": "NVDA",
        "option_type": "put",
        "underlying_price": 80.0,
        "strike": 70.0,
        "mid": 4.0,
        "dte": 200,
        "delta": -0.30,
        "iv": 0.55,
        "spread_pct": 0.08,
        "open_interest": 5000,
        "volume": 200,
        "iv_percentile": 40.0,
        "required_move_10x_pct": 0.6,
        "price": 80.0,
        "ma_50": 100.0,            # price below 50d
        "rs_vs_qqq_20d": -0.05,    # deteriorating
        "stock_features_raw": json.dumps({"rv_60d": 0.50}),
    }
    row.update(overrides)
    return row


def _catalyst_row(**overrides) -> dict:
    row = {
        "snapshot_time": "2026-06-10T14:00:00",
        "contract_id": "NVDA_C1",
        "ticker": "NVDA",
        "option_type": "call",
        "underlying_price": 100.0,
        "strike": 120.0,
        "mid": 5.0,
        "dte": 120,
        "delta": 0.35,
        "iv": 0.55,
        "spread_pct": 0.08,
        "open_interest": 5000,
        "volume": 200,
        "iv_percentile": 40.0,
        "required_move_10x_pct": 1.0,
        "price": 100.0,
        "ma_50": 90.0,
        "next_earnings_date": "2026-07-20",  # ~40d out, inside 120 DTE
        "stock_features_raw": json.dumps({"rv_60d": 0.50}),
    }
    row.update(overrides)
    return row


def test_breakdown_put_accepts_put_and_mirrors_stock_gates():
    event = build_candidate_event(_put_row(), "breakdown_put_v1", BREAKDOWN)
    assert event is not None
    assert "strategy_only_tracks_puts" not in event["trigger_reason"]
    assert "stock_below_50d" in event["trigger_reason"]
    assert "rs_vs_qqq_deteriorating" in event["trigger_reason"]
    assert event["raw"]["strategy_family"] == "breakdown_put"


def test_breakdown_put_rejects_calls():
    event = build_candidate_event(_put_row(option_type="call"), "breakdown_put_v1", BREAKDOWN)
    assert event is not None
    assert "strategy_only_tracks_puts" in event["trigger_reason"]
    assert event["state"] == "REJECT"


def test_catalyst_call_requires_catalyst_in_window():
    with_catalyst = build_candidate_event(_catalyst_row(), "catalyst_call_v1", CATALYST)
    assert "catalyst_within_dte" in with_catalyst["trigger_reason"]
    assert "no_catalyst_in_window" not in with_catalyst["trigger_reason"]

    without = build_candidate_event(_catalyst_row(next_earnings_date=None), "catalyst_call_v1", CATALYST)
    assert "no_catalyst_in_window" in without["trigger_reason"]


def test_catalyst_call_iv_crush_guard():
    rich = build_candidate_event(_catalyst_row(iv_rv_ratio=2.0), "catalyst_call_v1", CATALYST)
    assert "iv_rich_vs_rv" in rich["trigger_reason"]
    cheap = build_candidate_event(_catalyst_row(iv_rv_ratio=1.1), "catalyst_call_v1", CATALYST)
    assert "iv_rich_vs_rv" not in cheap["trigger_reason"]


def test_register_families_and_version_list(tmp_path):
    from investment_panel.core.db import db, init_db, query_rows

    init_db(tmp_path / "f.duckdb")
    with db(tmp_path / "f.duckdb") as con:
        from investment_panel.core.options_radar import register_default_strategy

        register_default_strategy(con, DEFAULT_STRATEGY_VERSION)
        written = register_strategy_families(con)
        versions = candidate_strategy_versions(con, DEFAULT_STRATEGY_VERSION)
        rows = query_rows(con, "SELECT strategy_version, status FROM option_strategy_versions WHERE strategy_version = 'breakdown_put_v1'")

    assert written == len(STRATEGY_FAMILY_PRESETS)
    assert versions[0] == DEFAULT_STRATEGY_VERSION  # primary first
    assert "catalyst_call_v1" in versions and "breakdown_put_v1" in versions
    assert rows[0]["status"] == "forward_test"
