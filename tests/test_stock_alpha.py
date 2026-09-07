from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.analysis.stock_alpha import (
    CONTROL_STATISTIC_VERSION,
    COST_MODEL_VERSION,
    FEATURE_VERSION,
    MODEL_VERSION,
    TARGET_HORIZON_SESSIONS,
    TARGET_VERSION,
    build_control_results,
    content_hash,
    hierarchical_calibration,
    independent_observations,
    research_score,
    walk_forward,
)
from investment_panel.analysis.research_validation import negative_control
from investment_panel.core.decision import MARKET_TZ, is_us_market_day, market_session_bounds


def _window_end(as_of: datetime) -> datetime:
    day = as_of.astimezone(MARKET_TZ).date()
    for _ in range(TARGET_HORIZON_SESSIONS):
        day += timedelta(days=1)
        while not is_us_market_day(day):
            day += timedelta(days=1)
    return market_session_bounds(day)[1].astimezone(UTC)


def _row(index: int, *, cohort: str = "large-liquid") -> dict[str, object]:
    as_of = datetime(2024, 1, 2, 22, tzinfo=UTC) + timedelta(days=index * 40)
    while not is_us_market_day(as_of.astimezone(MARKET_TZ).date()):
        as_of += timedelta(days=1)
    measured_through = _window_end(as_of)
    return {
        "ticker": f"T{index:02d}",
        "opportunity_episode_id": f"stock-episode-{index}",
        "horizon": "TACTICAL",
        "horizon_sessions": TARGET_HORIZON_SESSIONS,
        "target_version": TARGET_VERSION,
        "cohort_id": cohort,
        "as_of": as_of,
        "outcome_measured_through": measured_through,
        "outcome_available_at": measured_through + timedelta(hours=1),
        "feature_available_at": as_of - timedelta(minutes=30),
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
    assert first["target_version"] == TARGET_VERSION
    assert first["calibration_metrics"]["effective_sample_size"] > 0
    assert first["oos_period_start"] < first["oos_period_end"]
    assert all(row["ticker"] != "T14" for row in first["predictions"])
    assert first["forecasts"] and first["forecasts"][0]["forecast_distribution"]
    assert all(row["neutralized_return"] != row["net_utility_after_costs"] for row in first["predictions"])
    assert first["validation_paths"]
    assert len(first["artifact_hash"]) == 64


def test_walk_forward_excludes_feature_runs_completed_after_cutoff() -> None:
    rows = [_row(index) for index in range(14)]
    rows[0]["feature_available_at"] = datetime(2027, 1, 1, tzinfo=UTC)
    result = walk_forward(rows, cutoff=datetime(2026, 2, 1, tzinfo=UTC), min_train=4, fold_size=2, min_cohort=4)
    assert all(row["ticker"] != "T00" for row in result["predictions"])


def test_walk_forward_rejects_feature_available_after_row_decision_and_fold_boundary() -> None:
    rows = [_row(index) for index in range(14)]
    rows[5]["feature_available_at"] = rows[5]["as_of"] + timedelta(minutes=1)
    result = walk_forward(rows, cutoff=datetime(2026, 2, 1, tzinfo=UTC), min_train=4, fold_size=2, min_cohort=4)
    assert all(row["ticker"] != "T05" for row in result["predictions"])
    assert any(item["ticker"] == "T05" and item["reason"] == "feature_not_available_at_decision" for item in result["pit_rejections"])

    rows = [_row(index) for index in range(14)]
    rows[5]["feature_available_at"] = rows[5]["as_of"]
    result = walk_forward(rows, cutoff=datetime(2026, 2, 1, tzinfo=UTC), min_train=4, fold_size=2, min_cohort=4)
    assert all(row["ticker"] != "T05" for row in result["predictions"])
    assert any(item["ticker"] == "T05" and item["reason"] == "feature_not_available_at_fold_boundary" for item in result["pit_rejections"])


def test_production_controls_are_repeated_nonempty_and_deterministic() -> None:
    cutoff = datetime(2026, 2, 1, tzinfo=UTC)
    rows = [_row(index) for index in range(12)]
    first = build_control_results(rows, cutoff=cutoff)
    second = build_control_results(rows, cutoff=cutoff)
    assert first == second
    assert first["randomized_label_returns"]
    assert first["white_noise_market_returns"]
    assert first["control_metadata"]["repeats"] == 8
    assert first["control_metadata"]["source_sample_count"] == 12
    assert first["control_metadata"]["randomized_label"]["runs"] == 8
    assert first["control_metadata"]["white_noise_market"]["runs"] == 8
    source_hash = content_hash(rows)
    assert all(value != source_hash for value in first["control_metadata"]["randomized_label"]["input_hashes"])
    assert all(value != source_hash for value in first["control_metadata"]["white_noise_market"]["input_hashes"])
    assert any(value > 0 for value in first["white_noise_market_returns"])
    assert any(value < 0 for value in first["white_noise_market_returns"])


def test_prediction_controls_do_not_reuse_profitable_underlying_returns() -> None:
    rows = [dict(_row(index), outcome=1.0, realized_return=.05) for index in range(14)]
    cutoff = datetime(2026, 2, 1, tzinfo=UTC)
    primary = walk_forward(rows, cutoff=cutoff, min_train=4, fold_size=2, min_cohort=4)
    controls = build_control_results(rows, cutoff=cutoff, min_train=4, fold_size=2, min_cohort=4)
    assert all(row["net_utility_after_costs"] == pytest.approx(.048) for row in primary["predictions"])
    assert all(value < 0 for value in controls["randomized_label_returns"])
    assert controls["control_metadata"]["statistic_version"] == CONTROL_STATISTIC_VERSION
    assert controls["control_metadata"]["units"] == "brier_loss_improvement"
    assert controls["control_metadata"]["null_reference"]["scope"] == "falsification_controls_only"
    assert all("calibration_prior_probability" not in row for row in primary["predictions"])


def test_control_null_reference_excludes_outcomes_unavailable_at_cutoff() -> None:
    rows = [_row(index) for index in range(14)]
    cutoff = datetime(2026, 2, 1, tzinfo=UTC)
    future = dict(_row(14), realized_return=999.0, outcome_available_at=datetime(2027, 1, 1, tzinfo=UTC))
    assert build_control_results(rows + [future], cutoff=cutoff) == build_control_results(rows, cutoff=cutoff)


@pytest.mark.parametrize("leaked_probability", ["oracle", "invalid"])
def test_production_control_gate_rejects_skill_or_invalid_forecasts_after_perturbation(
    monkeypatch: pytest.MonkeyPatch, leaked_probability: str,
) -> None:
    from investment_panel.analysis import stock_alpha as producer

    real_walk_forward = producer.walk_forward

    def contaminated_forecasts(*args, **kwargs):
        artifact = real_walk_forward(*args, **kwargs)
        for prediction in artifact["predictions"]:
            prediction["calibrated_probability"] = (
                prediction["outcome"] if leaked_probability == "oracle" else float("nan")
            )
        return artifact

    monkeypatch.setattr(producer, "walk_forward", contaminated_forecasts)
    controls = producer.build_control_results(
        [_row(index) for index in range(14)], cutoff=datetime(2026, 2, 1, tzinfo=UTC),
    )
    check = negative_control(
        [.048], randomized=controls["randomized_label_returns"],
        white_noise=controls["white_noise_market_returns"],
    )
    assert check["passed"] is False
    assert check["tolerance"] == 0.0
    assert check["reason"] == (
        "negative_control_positive_edge" if leaked_probability == "oracle"
        else "negative_controls_domain_invalid"
    )


def test_later_episode_windows_count_but_overlapping_publications_do_not() -> None:
    rows = [dict(_row(index), ticker="HELD", opportunity_episode_id="held-thesis") for index in range(14)]
    repeated = [dict(row, as_of=row["as_of"] + timedelta(hours=2)) for row in rows]
    cutoff = datetime(2026, 2, 1, tzinfo=UTC)
    result = walk_forward(rows, cutoff=cutoff, min_train=4, fold_size=2, min_cohort=4)
    assert walk_forward(reversed(rows + repeated), cutoff=cutoff, min_train=4, fold_size=2, min_cohort=4) == result
    assert len(result["sample_windows"]) == 14
    assert all(window["opportunity_episode_id"] == "held-thesis" for window in result["sample_windows"])
    assert result["calibration_metrics"]["oos_sample_size"] > 0
    for prediction in result["predictions"]:
        assert prediction["sample_window_start"] == prediction["as_of"]
        assert prediction["sample_window_end"] == prediction["outcome_measured_through"]
    controls = build_control_results(rows + repeated, cutoff=cutoff)
    assert controls["control_metadata"]["source_sample_count"] == 14
    assert controls == build_control_results(rows, cutoff=cutoff)
    assert build_control_results(rows, cutoff=cutoff + timedelta(days=1)) == build_control_results(rows, cutoff=cutoff)


def test_episode_window_boundary_uses_maturity_before_late_publication() -> None:
    first = _row(0)
    first["outcome_available_at"] = first["outcome_measured_through"] + timedelta(days=7)
    second_start = first["outcome_measured_through"]
    second = dict(
        first, as_of=second_start, feature_available_at=second_start - timedelta(minutes=30),
        outcome_measured_through=_window_end(second_start),
        outcome_available_at=_window_end(second_start) + timedelta(hours=1),
    )
    overlapping = dict(
        first, as_of=second_start - timedelta(days=1),
        outcome_measured_through=_window_end(second_start - timedelta(days=1)),
        outcome_available_at=_window_end(second_start - timedelta(days=1)) + timedelta(hours=1),
    )
    cutoff = second["outcome_available_at"]
    retained = independent_observations([second, overlapping, first, dict(first)], cutoff=cutoff)
    assert [row["sample_window_start"] for row in retained] == [first["as_of"], second_start]
    assert retained[1]["sample_window_start"] < retained[0]["outcome_available_at"]
    assert retained[0]["sample_window_end"] == retained[1]["sample_window_start"]
    assert independent_observations(retained, cutoff=cutoff) == retained

    # An old row without a measurement clock uses availability conservatively.
    fallback = dict(first, outcome_measured_through=None)
    assert len(independent_observations([fallback, overlapping, second], cutoff=cutoff)) == 1


@pytest.mark.parametrize("measurement", ["invalid", "2024-01-02T22:00:00+00:00", "2027-01-01T00:00:00+00:00"])
def test_invalid_stock_window_clocks_do_not_create_samples(measurement: str) -> None:
    row = dict(_row(0), outcome_measured_through=measurement)
    assert independent_observations([row], cutoff=datetime(2026, 2, 1, tzinfo=UTC)) == []


@pytest.mark.parametrize("change", [
    {"target_version": "selected-expression-v1"},
    {"horizon": "FUNDAMENTAL"},
    {"horizon_sessions": 5},
])
def test_incompatible_stock_target_is_rejected(change: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="stock-alpha"):
        walk_forward([{**_row(0), **change}], cutoff=datetime(2026, 2, 1, tzinfo=UTC))


def test_conflicting_duplicate_episode_is_rejected() -> None:
    row = _row(0)
    with pytest.raises(ValueError, match="conflicting observations"):
        walk_forward([row, dict(row, realized_return=0.9)], cutoff=datetime(2026, 2, 1, tzinfo=UTC))


def test_stock_baselines_use_paired_returns_and_keep_missing_history_null() -> None:
    rows = [dict(_row(index), baseline_returns={"cash": 0.0, "market": 0.02, "trend": 0.01}) for index in range(14)]
    result = walk_forward(rows, cutoff=datetime(2026, 2, 1, tzinfo=UTC), min_train=4, fold_size=2, min_cohort=4)
    comparisons = result["baseline_comparisons"]
    assert comparisons["cash"]["sample_count"] == len(result["predictions"])
    assert comparisons["cash"]["mean_return"] == 0.0
    assert comparisons["market"]["mean_stock_net_excess"] == pytest.approx(comparisons["cash"]["mean_stock_net_excess"] - 0.02)
    assert comparisons["sector"]["sample_count"] == 0
    assert comparisons["sector"]["mean_stock_net_excess"] is None


@pytest.mark.parametrize("enabled,qualified,promotion_calls", [(False, True, 0), (True, False, 0), (True, True, 1)])
def test_scheduled_stock_promotion_uses_configured_paper_boundary(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, qualified: bool, promotion_calls: int,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from investment_panel.jobs import stock_alpha_walk_forward as job

    runtime = MagicMock()
    runtime.read.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = None
    config = SimpleNamespace(analysis=SimpleNamespace(options_decision_system=SimpleNamespace(strategy_auto_promotion_enabled=enabled)))
    monkeypatch.setattr(job, "load_config", lambda _path: config)
    monkeypatch.setattr(job, "runtime_for_config", lambda _config: runtime)
    monkeypatch.setattr(job, "load_observations", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(job, "load_universe_members", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(job, "build_control_results", lambda *_args, **_kwargs: {"randomized_label_returns": [0.0], "white_noise_market_returns": [0.0]})
    calls = []

    def evaluate(*_args, **kwargs):
        calls.append(kwargs)
        return {"complete": qualified}

    monkeypatch.setattr(job, "run", evaluate)
    job.scheduled()
    assert sum(bool(call.get("promote")) for call in calls) == promotion_calls
    if promotion_calls:
        assert calls[1]["cutoff"] == calls[0]["cutoff"]
        assert calls[1]["authorization_mode"] == "PAPER"
        assert calls[1]["promotion_cutoff"] >= calls[1]["cutoff"]
