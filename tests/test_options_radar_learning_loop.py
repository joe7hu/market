"""Self-improvement loop: realizable outcomes, shrinkage calibration, cohort-aware
scoring, and epsilon-exploration of near-miss setups."""

from __future__ import annotations

from investment_panel.core.db import db, init_db
from investment_panel.core.options_radar import (
    build_conviction_calibration,
    realized_exit_return,
)
from investment_panel.core.options_radar.opportunity_scoring import _learning_score
from investment_panel.core.options_radar.shadow import _exploration_sampled
from investment_panel.core.options_radar.strategy_outcomes import _realized_series
from investment_panel.core.options_radar.strategy_common import _proposed_family
from investment_panel.core.options_radar.strategy_proposals import build_strategy_mutation_proposal
from investment_panel.core.panel.read_learning import exploration_gate_report


def _series(values: list[float]) -> list[tuple[str, float]]:
    return [(f"2026-06-{i + 1:02d}", v) for i, v in enumerate(values)]


def test_realized_exit_trails_a_collapsing_spike() -> None:
    # Spikes to 5x on one mark, then collapses to worthless. The peak basis would credit
    # a 5x win; the realizable basis trails out near the breach, well under 2x.
    _time, realized = realized_exit_return(_series([0.0, 4.0, -0.9, -0.95]))
    assert realized < 1.0


def test_realized_exit_rides_a_sustained_winner() -> None:
    # A monotonic climb is never stopped out: realized == the last mark (a true 10x).
    _time, realized = realized_exit_return(_series([0.0, 1.0, 4.0, 9.5]))
    assert realized == 9.5


def test_realized_series_is_point_in_time_and_locks_after_stop() -> None:
    path = _realized_series(_series([0.0, 4.0, -0.9, 0.5]))
    # Open mark-to-market until the stop fires, then the locked exit value forever.
    assert path[0] == 0.0
    assert path[1] == 4.0
    assert path[2] == path[3]  # stopped at mark 2; later recovery does not un-lock


def test_calibration_shrinks_small_lucky_bins_toward_prediction() -> None:
    # One bin: predicted 0.20, but a tiny 3/3 lucky sample. Raw realized is 1.0; the
    # shrunk map point must sit far below that, near the 0.20 prior.
    bins, mapping, _calibrated = build_conviction_calibration([(0.2, 1, 0)] * 3)
    assert bins[0]["realized_p2x"] == 1.0
    assert bins[0]["shrunk_p2x"] < 0.5
    assert mapping[0][1] < 0.5


def test_learning_score_uses_significant_cohort_edges() -> None:
    row = {
        "raw": {"positives": [], "blockers": []},
        "price": 110.0,
        "ma_50": 100.0,
        "breakout_level": 108.0,
        "rs_vs_qqq_20d": 0.08,
        "iv_percentile": 40.0,
        "spread_pct": 0.10,
    }
    # No priors -> legacy neutral.
    assert _learning_score(row, cohort_priors=None, qqq_above_200d=True) == 50.0
    # A significant cohort the candidate belongs to (relative_strength_leader) pulls the
    # score toward that cohort's realized 2x hit rate.
    priors = {("setup_type", "relative_strength_leader"): {"hit_rate_2x": 0.8, "hit_rate_5x": 0.3, "n": 25}}
    assert _learning_score(row, cohort_priors=priors, qqq_above_200d=True) == 80.0


def test_exploration_sampling_is_deterministic_and_bounded() -> None:
    ids = [f"event-{i}" for i in range(2000)]
    sampled = [e for e in ids if _exploration_sampled(e, 0.12)]
    # Deterministic across calls and roughly the configured rate (not 0, not everything).
    assert sampled == [e for e in ids if _exploration_sampled(e, 0.12)]
    assert 0.08 < len(sampled) / len(ids) < 0.16
    assert all(not _exploration_sampled(e, 0.0) for e in ids[:50])


def _add_trade(con, trade_id: str, authority: str, realized: float) -> None:
    con.execute(
        "INSERT INTO shadow_trade (trade_id, event_id, entry_time, status, raw) VALUES (?, ?, '2026-01-01', 'open', ?)",
        [trade_id, f"e-{trade_id}", f'{{"authority": "{authority}"}}'],
    )
    con.execute(
        "INSERT INTO shadow_trade_mark (mark_id, trade_id, mark_time, max_return_since_alert, raw) VALUES (?, ?, '2026-03-01', ?, ?)",
        [f"m-{trade_id}", trade_id, realized, f'{{"realized_exit_return": {realized}}}'],
    )


def test_exploration_gate_report_quantifies_gate_value(tmp_path) -> None:
    init_db(tmp_path / "gate.duckdb")
    with db(tmp_path / "gate.duckdb") as con:
        _add_trade(con, "f1", "shadow_only", 1.5)   # FIRE win (>=2x realizable)
        _add_trade(con, "f2", "shadow_only", 0.1)   # FIRE dud
        _add_trade(con, "x1", "shadow_exploration", 0.2)  # rejected setup, dud
        report = {row["bucket"]: row for row in exploration_gate_report(con)}
    assert report["fire"]["n"] == 2
    assert report["fire"]["hit_rate_2x"] == 0.5
    assert report["exploration"]["n"] == 1
    assert report["exploration"]["hit_rate_2x"] == 0.0
    # Gates select winners the rejected region didn't produce -> positive edge.
    assert report["fire"]["gate_edge_2x"] == 0.5


def test_same_family_proposals_do_not_collide_on_version() -> None:
    # open_interest, volume and spread missed-winners all map to the liquidity_watch
    # family with *different* loosenings; each must get its own promotable version so a
    # later promotion can't overwrite an earlier one's parameters.
    versions = {
        reason: build_strategy_mutation_proposal(
            {"filter_reason": reason, "proposed_strategy_family": "leap_10x_liquidity_watch", "missed_count": 1, "best_return": 5.0, "missed_ids": []},
            "leap_10x_reversal_v1",
        )["proposed_strategy_version"]
        for reason in ("open_interest_below_threshold", "volume_below_threshold", "spread_above_fire_threshold")
    }
    assert len(set(versions.values())) == 3
    assert all(v.startswith("leap_10x_liquidity_watch__") for v in versions.values())


def test_dte_missed_winners_propose_short_dated_lottery_sleeve() -> None:
    proposal = build_strategy_mutation_proposal(
        {
            "filter_reason": "dte_outside_strategy_range",
            "proposed_strategy_family": _proposed_family("dte_outside_strategy_range"),
            "missed_count": 12,
            "best_return": 8.0,
            "missed_ids": ["missed-1"],
        },
        "leap_10x_reversal_v1",
    )

    assert proposal is not None
    assert proposal["proposed_strategy_version"].startswith("short_dated_lottery_call__")
    assert proposal["proposed_parameter_changes"] == {
        "dte_min": 2,
        "dte_max": 45,
        "delta_min": 0.01,
        "delta_max": 0.20,
        "max_required_move_pct": 5.0,
        "candidate_note": "test short-dated low-delta lottery sleeve separately with strict liquidity gates",
    }
    assert proposal["requires_backtest"] is True
    assert proposal["requires_forward_test"] is True
