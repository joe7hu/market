from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.analysis.research_validation import (
    combinatorial_path_records,
    combinatorial_paths,
    cost_capacity_stress,
    future_information_trap,
    multiple_testing_metrics,
    negative_control,
    parameter_stability,
    purged_embargoed_splits,
    validate_trial,
)
from investment_panel.core.decision import build_strategy_forecast, strategy_forecast_id_for_payload
from investment_panel.database.research import ResearchRepository


def test_phase1_validation_is_deterministic_and_fail_closed() -> None:
    assert not negative_control([0.2])["passed"]
    assert negative_control([0.2], randomized=[0.0], white_noise=[-0.01])["passed"]
    assert not negative_control([0.2], randomized=[0.01], white_noise=[0.2])["passed"]
    assert future_information_trap(feature_available_at=[1, 2], cutoff=1)["passed"] is False
    observations = [{"id": str(index), "as_of": index} for index in range(8)]
    assert purged_embargoed_splits(observations, purge=1, embargo=1)
    assert len(combinatorial_paths(observations, folds=5, max_paths=3)) == 3
    metrics = multiple_testing_metrics([0.1, 0.2, 0.3], trials_tested=4, path_returns=[0.1], p_values=[0.2])
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


def test_multiple_testing_requires_real_paths_and_rejects_invalid_domains() -> None:
    assert multiple_testing_metrics([0.1, 0.2], trials_tested=2, path_returns=[0.1], p_values=[0.2])[
        "domain_valid"
    ]
    invalid = multiple_testing_metrics(
        [0.1, 0.2], trials_tested=2, path_returns=[0.1, float("nan")], p_values=[-0.1],
    )
    assert invalid["domain_valid"] is False
    assert invalid["paths_domain_valid"] is False
    assert invalid["p_values_domain_valid"] is False


def test_combinatorial_paths_are_not_contiguous_window_substitutes() -> None:
    paths = combinatorial_paths([0, 1, 2, 3], folds=4, max_paths=10, purge=0, embargo=0)
    assert (0, 2) in paths
    assert (1, 3) in paths
    records = combinatorial_path_records([0, 1, 2, 3, 4], folds=5, purge=1, embargo=1)
    assert records
    assert all(record["train_folds"] for record in records)
    assert all(set(record["test_folds"]).isdisjoint(record["train_folds"]) for record in records)


def test_validation_fails_closed_when_feature_availability_is_missing() -> None:
    report = validate_trial(
        mechanism_class="quality", falsification_rule="controls",
        observed_returns=[0.1, 0.1], randomized_returns=[0.0], white_noise_returns=[0.0],
        gross_return=0.2, base_cost=0.01, neutralized_returns=[0.02, 0.02],
        parameter_neighborhood=[{"return": 0.1}, {"return": 0.1}], trials_tested=1,
        cutoff=1, expected_members=["a"], observed_members=["a"],
        expected_attempts=["t"], completed_attempts=["t"], path_returns=[0.1],
    )
    assert report["gates"]["pit_integrity"]["passed"] is False


def test_strategy_forecast_identity_is_fail_closed_and_actual_availability_is_retained() -> None:
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
    future = build_strategy_forecast(
        ticker="ACME", opportunity_episode_id="episode:acme", strategy_revision_id=1,
        strategy_evaluation_id=None, target="return", horizon="1d", forecast_value=0.1,
        model_artifact_id="artifact", artifact_hash="a" * 64, input_hash="b" * 64,
        as_of=cutoff, generated_at=cutoff + timedelta(seconds=1), available_at=cutoff + timedelta(seconds=1),
    )
    assert future.generated_at > future.input_cutoff


def test_strategy_forecast_identity_normalizes_exact_second_utc_timestamps() -> None:
    cutoff = datetime(2026, 8, 22, 14, 0, 0, tzinfo=UTC)
    forecast = build_strategy_forecast(
        ticker="ACME", opportunity_episode_id="episode:acme", strategy_revision_id=1,
        strategy_evaluation_id=None, target="return", horizon="1d", forecast_value=0.1,
        model_artifact_id="artifact", artifact_hash="a" * 64, input_hash="b" * 64,
        as_of=cutoff, generated_at=cutoff - timedelta(minutes=1), available_at=cutoff - timedelta(minutes=1),
    )
    payload = forecast.model_dump(mode="json")
    payload["generated_at"] = "2026-08-22T13:59:00.000000+00:00"
    payload["available_at"] = "2026-08-22T13:59:00.000000+00:00"
    assert strategy_forecast_id_for_payload(payload) == forecast.strategy_forecast_id


def test_strategy_forecast_identity_normalizes_equivalent_numeric_forms() -> None:
    base = {
        "contract_version": "strategy-forecast.v1", "ticker": "ACME",
        "opportunity_episode_id": "episode:acme", "strategy_revision_id": 1,
        "strategy_evaluation_id": None, "target": "return", "horizon": "1d",
        "forecast_value": 1.0, "forecast_range": {"low": 0.0, "high": 1.0},
        "forecast_distribution": {"positive": 1.0}, "probability_semantics": "P(positive)",
        "model_artifact_id": "artifact", "artifact_hash": "a" * 64,
        "input_hash": "b" * 64, "as_of": "2026-08-22T14:00:00Z",
        "input_cutoff": "2026-08-22T14:00:00.000000+00:00",
        "generated_at": "2026-08-22T13:59:00Z", "available_at": "2026-08-22T13:59:00Z",
    }
    equivalent = {**base, "forecast_value": "1", "forecast_range": {"low": "0", "high": "1.000"}, "forecast_distribution": {"positive": "1.0000"}}
    assert strategy_forecast_id_for_payload(base) == strategy_forecast_id_for_payload(equivalent)
    negative_zero = {**base, "forecast_value": -0.0, "forecast_range": {"low": -0.0, "high": 1.0}, "forecast_distribution": {"positive": -0.0, "negative": 1.0}}
    zero_equivalent = {**base, "forecast_value": 0, "forecast_range": {"low": 0, "high": 1}, "forecast_distribution": {"positive": 0, "negative": 1}}
    assert strategy_forecast_id_for_payload(negative_zero) == strategy_forecast_id_for_payload(zero_equivalent)


def test_research_repository_rejects_caller_owned_gate_timestamps() -> None:
    with pytest.raises(ValueError, match="database-owned"):
        ResearchRepository(None).record_gate(  # type: ignore[arg-type]
            dossier_id="dossier", code="pit_integrity", verdict="fail",
            evaluated_at=datetime.now(UTC) - timedelta(days=1),
            available_at=datetime.now(UTC) - timedelta(days=1),
        )
