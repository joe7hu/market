from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.analysis.research_validation import (
    combinatorial_paths,
    cost_capacity_stress,
    future_information_trap,
    multiple_testing_metrics,
    negative_control,
    parameter_stability,
    purged_embargoed_splits,
    validate_trial,
)
from investment_panel.core.decision import build_strategy_forecast


def test_phase1_validation_is_deterministic_and_fail_closed() -> None:
    assert negative_control([0.2], randomized=[0.0], white_noise=[-0.01])["passed"]
    assert not negative_control([0.2], randomized=[0.01], white_noise=[0.2])["passed"]
    assert future_information_trap(feature_available_at=[1, 2], cutoff=1)["passed"] is False
    observations = [{"id": str(index), "as_of": index} for index in range(8)]
    assert purged_embargoed_splits(observations, purge=1, embargo=1)
    assert len(combinatorial_paths(observations, folds=5, max_paths=3)) == 3
    metrics = multiple_testing_metrics([0.1, 0.2, 0.3], trials_tested=4)
    assert set(("psr", "dsr", "pbo", "fdr_q_value")) <= metrics.keys()
    assert parameter_stability([{"return": 0.09}, {"return": 0.1}, {"return": 0.11}])["passed"]
    assert cost_capacity_stress(gross_return=0.2, base_cost=0.01)["explicit_3x"]["net_return"] > 0
    assert cost_capacity_stress(gross_return=0.02, base_cost=0.01)["passed"] is False


def test_multiple_testing_uses_real_domains_and_validation_binds_all_gates() -> None:
    metrics = multiple_testing_metrics(
        [0.1, 0.2, 0.3], trials_tested=4, path_returns=[0.1, -0.1, 0.2], p_values=[0.01, 0.2, 0.4],
    )
    assert metrics["pbo"] == pytest.approx(1 / 3)
    assert metrics["data_snooping_probability"] != metrics["pbo"]
    with pytest.raises(ValueError, match="trials_tested"):
        multiple_testing_metrics([0.1], trials_tested=0)
    report = validate_trial(
        mechanism_class="quality", falsification_rule="controls",
        observed_returns=[0.1, 0.1], randomized_returns=[0.0], white_noise_returns=[0.0],
        gross_return=0.2, base_cost=0.01, neutralized_returns=[0.02],
        parameter_neighborhood=[{"return": 0.1}, {"return": 0.1}], trials_tested=1,
        feature_available_at=[1], cutoff=1, expected_members=["a"], observed_members=["a"],
        expected_attempts=["t"], completed_attempts=["t"], path_returns=[0.1, 0.2],
    )
    assert set(report["gates"]) == {"pit_integrity", "denominator_completeness", "oos_predictive_validity", "falsification_and_robustness", "economic_promotability"}


def test_strategy_forecast_identity_and_generation_cutoff_are_fail_closed() -> None:
    cutoff = datetime(2026, 8, 22, 14, tzinfo=UTC)
    forecast = build_strategy_forecast(
        ticker="ACME", opportunity_episode_id="episode:acme", strategy_revision_id=1,
        strategy_evaluation_id=None, target="return", horizon="1d", forecast_value=0.1,
        model_artifact_id="artifact", artifact_hash="a" * 64, input_hash="b" * 64,
        as_of=cutoff, generated_at=cutoff - timedelta(minutes=1), available_at=cutoff - timedelta(minutes=1),
    )
    tampered = forecast.model_dump(mode="json")
    tampered["forecast_value"] = 0.2
    with pytest.raises(ValueError, match="identity"):
        type(forecast).model_validate(tampered)
    with pytest.raises(ValueError, match="generation"):
        build_strategy_forecast(
            ticker="ACME", opportunity_episode_id="episode:acme", strategy_revision_id=1,
            strategy_evaluation_id=None, target="return", horizon="1d", forecast_value=0.1,
            model_artifact_id="artifact", artifact_hash="a" * 64, input_hash="b" * 64,
            as_of=cutoff, generated_at=cutoff + timedelta(seconds=1), available_at=cutoff,
        )
