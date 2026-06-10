"""Honest validation: walk-forward, significance, regime buckets (Phase 2b)."""

from __future__ import annotations

from investment_panel.core.options_radar import (
    _backtest_verdict,
    _market_regime,
    _strategy_arm_significance,
    _walk_forward_folds,
)


def _outcomes(n, hr2=0.0, hr5=0.0, hr10=0.0, fp=0.0):
    return {"candidate_count": n, "hit_rate_2x": hr2, "hit_rate_5x": hr5, "hit_rate_10x": hr10, "false_positive_rate": fp}


def test_significance_flags_insufficient_and_significant():
    small = _strategy_arm_significance(_outcomes(10, hr2=0.5), _outcomes(10, hr2=0.5))
    assert small["insufficient_sample"] is True
    big = _strategy_arm_significance(_outcomes(100, hr2=0.3), _outcomes(100, hr2=0.7))
    assert big["insufficient_sample"] is False and big["significant"] is True
    null = _strategy_arm_significance(_outcomes(100, hr2=0.5), _outcomes(100, hr2=0.5))
    assert null["significant"] is False


def test_verdict_blocks_on_insufficient_sample():
    base, prop = _outcomes(5, hr5=0.1), _outcomes(5, hr5=0.9)
    sig = _strategy_arm_significance(base, prop, key="5x")
    assert _backtest_verdict(base, prop, significance=sig) == "insufficient_sample"


def test_verdict_requires_significance_and_walk_forward():
    base = _outcomes(100, hr2=0.30, hr5=0.30, hr10=0.05)
    prop = _outcomes(100, hr2=0.55, hr5=0.55, hr10=0.10)
    sig = _strategy_arm_significance(base, prop, key="2x")  # different rates, n=100 -> significant
    wf_pass = {"evaluable": True, "pass": True}
    wf_fail = {"evaluable": True, "pass": False}
    assert _backtest_verdict(base, prop, significance=sig, walk_forward=wf_pass) == "pass"
    # Same improvement but fails out-of-sample-in-time -> not a pass.
    assert _backtest_verdict(base, prop, significance=sig, walk_forward=wf_fail) == "fail"


def test_walk_forward_majority_of_folds():
    rows = [{"snapshot_time": f"2026-01-{d:02d}T00:00:00"} for d in range(1, 31)]
    # Proposed beats baseline in every fold.
    wf = _walk_forward_folds(
        rows,
        lambda r: _outcomes(len(r), hr5=0.2, hr10=0.0),
        lambda r: _outcomes(len(r), hr5=0.5, hr10=0.1),
    )
    assert wf["evaluable"] and wf["folds_improved"] == 3 and wf["pass"] is True
    # Proposed never beats baseline.
    wf2 = _walk_forward_folds(
        rows,
        lambda r: _outcomes(len(r), hr5=0.5),
        lambda r: _outcomes(len(r), hr5=0.2),
    )
    assert wf2["pass"] is False


def test_market_regime_two_dimensional(tmp_path):
    from investment_panel.core.db import db, init_db

    init_db(tmp_path / "r.duckdb")
    with db(tmp_path / "r.duckdb") as con:
        # 260 rising QQQ closes -> price well above 200d MA -> risk_on.
        for i in range(260):
            con.execute(
                "INSERT INTO prices_daily (symbol, date, close) VALUES ('QQQ', TRY_CAST(? AS DATE), ?)",
                [f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}", 100.0 + i],
            )
        regime = _market_regime(con, "2026-06-10", {})

    assert regime["risk"] == "risk_on"
    assert regime["vol"] in {"vol_low", "vol_high"}
    assert "/" in regime["regime"]
