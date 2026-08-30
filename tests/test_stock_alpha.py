from __future__ import annotations

from datetime import UTC, datetime, timedelta

from investment_panel.analysis.stock_alpha import (
    COST_MODEL_VERSION,
    FEATURE_VERSION,
    MODEL_VERSION,
    hierarchical_calibration,
    research_score,
    walk_forward,
)


def _row(index: int, *, cohort: str = "large-liquid") -> dict[str, object]:
    as_of = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
    return {
        "ticker": f"T{index:02d}",
        "horizon": "TACTICAL",
        "cohort_id": cohort,
        "as_of": as_of,
        "outcome_available_at": as_of + timedelta(hours=1),
        "outcome": float(index % 2 == 0),
        "realized_return": 0.04 if index % 2 == 0 else -0.02,
        "modeled_cost": 0.002,
        "features": {
            "feature_version": FEATURE_VERSION,
            "momentum_5d": 0.02,
            "momentum_20d": 0.04,
            "relative_strength_20d": 0.03,
            "relative_strength_60d": 0.06,
            "kaufman_er_20d": 0.5,
        },
    }


def test_research_score_has_no_liquidity_fill_or_utility_inputs() -> None:
    features = {
        "feature_version": FEATURE_VERSION,
        "momentum_5d": 0.02,
        "momentum_20d": 0.04,
        "relative_strength_20d": 0.03,
        "relative_strength_60d": 0.06,
        "kaufman_er_20d": 0.5,
    }
    baseline = research_score(features)
    assert baseline is not None
    assert research_score({**features, "liquidity": -999, "fill": -999, "utility": -999}) == baseline
    assert research_score({key: value for key, value in features.items() if key != "kaufman_er_20d"}) is None
    assert research_score({**features, "feature_version": "other"}) is None


def test_hierarchical_calibration_uses_exact_then_parent_and_never_synthesises() -> None:
    rows = [_row(index) for index in range(4)] + [_row(index + 4, cohort="small") for index in range(4)]
    calibrated, path, parent, sample = hierarchical_calibration(
        0.6, rows, horizon="TACTICAL", cohort_id="large-liquid", min_cohort=4,
    )
    assert calibrated is not None
    assert path == ["cohort:large-liquid"]
    assert parent == "horizon:TACTICAL"
    assert sample == 4

    fallback, fallback_path, fallback_parent, fallback_sample = hierarchical_calibration(
        0.6, rows, horizon="TACTICAL", cohort_id="missing", min_cohort=4,
    )
    assert fallback is not None
    assert fallback_path == ["cohort:missing", "horizon:TACTICAL"]
    assert fallback_parent == "global"
    assert fallback_sample == 8

    assert hierarchical_calibration(0.6, rows[:2], horizon="OTHER", cohort_id="missing", min_cohort=4)[0] is None


def test_walk_forward_is_deterministic_pit_and_versioned() -> None:
    rows = [_row(index) for index in range(14)]
    future = _row(14)
    future["outcome_available_at"] = datetime(2027, 1, 1, tzinfo=UTC)
    cutoff = datetime(2026, 2, 1, tzinfo=UTC)

    first = walk_forward(rows + [future], cutoff=cutoff, min_train=4, fold_size=2, min_cohort=4)
    second = walk_forward(reversed(rows + [future]), cutoff=cutoff, min_train=4, fold_size=2, min_cohort=4)

    assert first == second
    assert first["model_version"] == MODEL_VERSION
    assert first["feature_version"] == FEATURE_VERSION
    assert first["cost_model_version"] == COST_MODEL_VERSION
    assert first["calibration_metrics"]["effective_sample_size"] > 0
    assert first["oos_period_start"] < first["oos_period_end"]
    assert all(row["ticker"] != "T14" for row in first["predictions"])
    assert len(first["artifact_hash"]) == 64
