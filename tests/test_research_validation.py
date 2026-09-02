from investment_panel.analysis.research_validation import (
    combinatorial_paths,
    cost_capacity_stress,
    future_information_trap,
    multiple_testing_metrics,
    negative_control,
    parameter_stability,
    purged_embargoed_splits,
)


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
