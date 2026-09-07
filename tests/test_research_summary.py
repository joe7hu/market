from datetime import UTC, datetime

import pytest

from investment_panel.analysis.stock_alpha import TARGET_VERSION
from investment_panel.database.research_summary import evaluation_summary


def _evaluation(**metrics):
    return {
        "evaluation_type": "out_of_sample", "verdict": "incomplete",
        "evaluated_at": datetime(2026, 9, 1, tzinfo=UTC),
        "metrics": {"target_version": TARGET_VERSION, **metrics},
        "evidence": {"walk_forward": True, "purge_embargo": True},
    }


def test_research_reports_only_current_independent_stock_outcomes():
    row = _evaluation(
        effective_sample_size=22, lower_confidence_net_utility_after_costs=0.03,
        calibration_metrics={"brier_score": 0.18},
        validation={"gates": {"sample_size": {"passed": False}, "costs": {"passed": True}}},
    )
    summary = evaluation_summary(row)
    assert summary["independent_sample_count"] == 22
    assert summary["net_return_lower_bound"] == 0.03
    assert summary["brier_score"] == 0.18
    assert summary["failed_gates"] == ["sample_size"]
    assert summary["evidence_basis"] == "independent_stock_episodes"


@pytest.mark.parametrize("change,basis", [
    ({"target_version": "legacy-relative-option-return"}, "obsolete_stock_target"),
    ({"purge_embargo": False}, "independence_unconfirmed"),
])
def test_legacy_or_unconfirmed_samples_do_not_become_performance(change, basis):
    row = _evaluation(effective_sample_size=400, lower_confidence_net_utility_after_costs=0)
    if "target_version" in change:
        row["metrics"].update(change)
    else:
        row["evidence"].update(change)
    summary = evaluation_summary(row)
    assert summary["independent_sample_count"] is None
    assert summary["net_return_lower_bound"] is None
    assert summary["brier_score"] is None
    assert summary["evidence_basis"] == basis


@pytest.mark.parametrize("sample", [None, True, -1, 0.5, "NaN", float("inf")])
def test_invalid_sample_counts_stay_unknown(sample):
    summary = evaluation_summary(_evaluation(effective_sample_size=sample))
    assert summary["independent_sample_count"] is None
    assert summary["net_return_lower_bound"] is None


def test_zero_observations_do_not_show_zero_returns():
    summary = evaluation_summary(_evaluation(
        effective_sample_size=0, lower_confidence_net_utility_after_costs=0,
        calibration_metrics={"brier_score": 0},
    ))
    assert summary["independent_sample_count"] == 0
    assert summary["net_return_lower_bound"] is None
    assert summary["brier_score"] is None


@pytest.mark.parametrize("score", [-0.1, 1.1, True, "NaN"])
def test_invalid_probability_error_stays_unknown(score):
    summary = evaluation_summary(_evaluation(
        effective_sample_size=10, calibration_metrics={"brier_score": score},
    ))
    assert summary["brier_score"] is None


def _options_evaluation(stage="shadow", **metrics):
    return {
        "evaluation_type": stage, "verdict": "collecting_data",
        "metrics": {
            "proposed": {"sample_size": 12, "lower_95_expectancy": 0.9},
            "comparison_denominator": 20, "unmatched_episodes": 0,
            "comparison_window_complete": True, "comparison_lower_95": 0.03,
            "brier": 0.2, **metrics,
        },
        "evidence": {"options_comparison_version": "options-independent-comparison-v1"},
    }


@pytest.mark.parametrize("stage,basis", [
    ("walk_forward", "independent_options_replay"),
    ("shadow", "independent_options_shadow"),
    ("execution_grade_paper", "independent_options_paper"),
])
def test_versioned_options_stages_show_their_own_samples_and_comparison(stage, basis):
    summary = evaluation_summary(_options_evaluation(stage))
    assert summary["independent_sample_count"] == 12
    assert summary["net_return_lower_bound"] == 0.03
    assert summary["brier_score"] == 0.2
    assert summary["evidence_basis"] == basis
    assert summary["comparison_denominator"] == 20
    assert summary["unmatched_episodes"] == 0
    assert summary["comparison_window_complete"] is True


@pytest.mark.parametrize("metrics", [
    {"comparison_window_complete": False},
    {"comparison_window_complete": None},
    {"comparison_window_complete": "true"},
    {"unmatched_episodes": 2},
    {"unmatched_episodes": None},
    {"unmatched_episodes": False},
    {"comparison_denominator": 10},
    {"comparison_lower_95": None},
    {"comparison_lower_95": float("nan")},
])
def test_options_collection_keeps_real_counts_without_a_premature_lower_bound(metrics):
    summary = evaluation_summary(_options_evaluation(**metrics))
    assert summary["independent_sample_count"] == 12
    assert summary["net_return_lower_bound"] is None


@pytest.mark.parametrize("version", [None, "options-independent-comparison-v0"])
def test_unversioned_options_evidence_stays_unknown(version):
    row = _options_evaluation()
    row["evidence"] = {"options_comparison_version": version}
    summary = evaluation_summary(row)
    assert summary["evidence_basis"] == "independence_unconfirmed"
    assert summary["independent_sample_count"] is None
    assert summary["net_return_lower_bound"] is None
    assert summary["brier_score"] is None
    assert summary["comparison_denominator"] is None


@pytest.mark.parametrize("brier", [None, True, -0.1, 1.1, "NaN"])
def test_options_missing_or_invalid_brier_is_unknown(brier):
    summary = evaluation_summary(_options_evaluation(brier=brier))
    assert summary["independent_sample_count"] == 12
    assert summary["brier_score"] is None


@pytest.mark.parametrize("sample", [0, None, True])
def test_options_need_a_positive_measured_sample_for_returns(sample):
    summary = evaluation_summary(_options_evaluation(proposed={"sample_size": sample}))
    assert summary["independent_sample_count"] == (0 if sample == 0 and type(sample) is int else None)
    assert summary["net_return_lower_bound"] is None
    assert summary["brier_score"] is None
