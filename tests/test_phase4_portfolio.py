from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.core.portfolio import (
    PaperExecutionObservation,
    PortfolioCandidate,
    allocate_portfolio,
    allocate_portfolio_for_tests,
    apply_decay_guard,
    attribute_paper_pnl,
    build_execution_model_snapshot,
    build_scenario_artifact,
)


AS_OF = datetime(2026, 9, 2, 15, tzinfo=UTC)


def candidate(candidate_id: str, **overrides: object) -> PortfolioCandidate:
    values: dict[str, object] = {
        "candidate_id": candidate_id,
        "ticker": candidate_id,
        "strategy_forecast_id": f"forecast:{candidate_id}",
        "action_id": f"action:{candidate_id}",
        "rank_id": f"rank:{candidate_id}",
        "hypothesis_id": "hypothesis:test",
        "experiment_id": "experiment:test",
        "trial_id": "trial:test",
        "result_id": "result:test",
        "expression": {"kind": "stock", "ticker": candidate_id},
        "expected_return": 0.12,
        "uncertainty": 0.02,
        "volatility": 0.20,
        "risk_budget": 0.10,
        "kelly_cap": 0.20,
        "drawdown_cap": 0.20,
        "capacity": 0.20,
        "covariance": {candidate_id: 0.04},
        "cash_available": 1000,
        "cash_source_id": "acct:test:cash",
        "input_cutoff": AS_OF - timedelta(minutes=1),
        "available_at": AS_OF - timedelta(minutes=2),
    }
    values.update(overrides)
    values.setdefault("rank_position", 1)
    values.setdefault("rank_utility", float(values["expected_return"]) - float(values["uncertainty"]))
    return PortfolioCandidate.model_validate(values)


def test_allocator_is_pit_fail_closed_and_separates_rejection_reasons() -> None:
    allocation = allocate_portfolio_for_tests([
        candidate("OVERLAP", blockers=("overlap_conflict",)),
        candidate("CAPACITY", capacity=0),
        candidate("EXECUTION", blockers=("execution_unavailable",)),
        candidate("LATE", available_at=AS_OF),
    ], as_of=AS_OF, cash_hurdle=0.01)

    assert allocation.status == "cash_only"
    rejected = {item.ticker: set(item.blockers) for item in allocation.items}
    assert rejected["OVERLAP"] == {"overlap_conflict"}
    assert "capacity_unavailable" in rejected["CAPACITY"]
    assert rejected["EXECUTION"] == {"execution_unavailable"}
    assert "pit_lineage_conflict" in rejected["LATE"]
    assert [item.ticker for item in allocation.items if item.ticker == "CASH"] == ["CASH"]


def test_allocator_selects_positive_marginal_utility_and_keeps_cash_above_hurdle() -> None:
    allocation = allocate_portfolio_for_tests([
        candidate("GOOD"),
        candidate("BELOW", expected_return=0.03, uncertainty=0.03),
    ], as_of=AS_OF, cash_hurdle=0.02)

    good = next(item for item in allocation.items if item.ticker == "GOOD")
    below = next(item for item in allocation.items if item.ticker == "BELOW")
    cash = next(item for item in allocation.items if item.ticker == "CASH")
    assert good.disposition == "selected"
    assert good.marginal_book_utility > 0.02
    assert below.disposition == "ranked_out"
    assert cash.target_weight > 0
    assert good.funding_source == "CASH:acct:test:cash"
    assert {"uncertainty", "volatility", "risk_budget", "kelly_cap", "drawdown_cap", "capacity"} <= set(good.trace)


def test_allocator_rejects_funding_without_postgres_cash_or_position_identity() -> None:
    allocation = allocate_portfolio_for_tests([candidate("NO_FUNDING", cash_available=None, cash_source_id=None)], as_of=AS_OF, cash_hurdle=0.01)
    item = next(item for item in allocation.items if item.ticker == "NO_FUNDING")
    assert item.disposition == "rejected"
    assert "cash_funding_missing" in item.blockers


def test_allocator_rejects_free_form_mapping_authority() -> None:
    with pytest.raises(TypeError, match="AuthoritativePortfolioBundle"):
        allocate_portfolio([candidate("MAPPING").model_dump()], as_of=AS_OF)


def test_allocator_persists_covariance_marginal_risk_not_weight_times_volatility() -> None:
    left = candidate("LEFT", covariance={"LEFT": 0.04, "RIGHT": 0.02})
    right = candidate("RIGHT", covariance={"LEFT": 0.02, "RIGHT": 0.09}, cash_source_id="acct:test:cash:right")
    allocation = allocate_portfolio_for_tests([left, right], as_of=AS_OF, cash_hurdle=0.01)
    item = next(item for item in allocation.items if item.ticker == "RIGHT")
    assert item.trace["proposed_marginal_risk_contribution"] is not None
    assert item.trace["proposed_marginal_risk_contribution"] != item.target_weight * right.volatility
    assert "uncertainty_haircut" in item.trace


def test_scenario_artifact_is_bounded_and_contains_tail_and_unwind_evidence() -> None:
    allocation = allocate_portfolio_for_tests([candidate("GOOD")], as_of=AS_OF, cash_hurdle=0.01)
    artifact = build_scenario_artifact(
        allocation,
        [{"name": "base", "probability": 0.7, "returns": {"GOOD": 0.05}, "shocks": {"GOOD": 0.01}}, {"name": "tail", "probability": 0.3, "returns": {"GOOD": -0.2}, "shocks": {"GOOD": -0.3}}],
        model_version="scenario.v1",
        probability_semantics="normalized states",
        tail_dependence={"negative_return_co_exceedance": {"GOOD|GOOD": {"probability": 0.3}}},
        simultaneous_unwind={"trigger": "tail", "probability": 0.3, "observations": 2},
    )
    assert artifact.scenario_artifact_id.startswith("scenario:")
    assert artifact.tail_dependence["negative_return_co_exceedance"]
    assert artifact.simultaneous_unwind["observations"] == 2
    with pytest.raises(ValueError):
        build_scenario_artifact(allocation, [{"probability": 1, "returns": {}}] * 65, model_version="v", probability_semantics="p", tail_dependence={"x": 1}, simultaneous_unwind={"x": 1})


def test_scenario_artifact_rejects_mutated_content_and_empty_tail_or_unwind() -> None:
    allocation = allocate_portfolio_for_tests([candidate("GOOD")], as_of=AS_OF, cash_hurdle=0.01)
    kwargs = {
        "allocation": allocation,
        "scenarios": [{"probability": 1, "returns": {"GOOD": 0.1}, "shocks": {"GOOD": 0.2}}],
        "model_version": "scenario.v1", "probability_semantics": "observed",
        "tail_dependence": {"negative_return_co_exceedance": {"GOOD|GOOD": {"probability": 0}}},
        "simultaneous_unwind": {"probability": 0, "observations": 1},
    }
    artifact = build_scenario_artifact(**kwargs)
    with pytest.raises(ValueError):
        type(artifact).model_validate({**artifact.model_dump(), "scenarios": ({"probability": 1, "returns": {"GOOD": 0.2}, "shocks": {"GOOD": 0.2}},)})
    with pytest.raises(ValueError):
        build_scenario_artifact(**{**kwargs, "tail_dependence": {}})
    with pytest.raises(ValueError):
        build_scenario_artifact(**{**kwargs, "scenarios": [{"probability": 1, "returns": {"GOOD": 0.1}, "shocks": {"GOOD": 0.1}}]})


def test_decay_guard_reduces_before_the_rollback_threshold() -> None:
    allocation = allocate_portfolio_for_tests([candidate("GOOD")], as_of=AS_OF, cash_hurdle=0.01)
    item = next(item for item in allocation.items if item.ticker == "GOOD")
    decisions = apply_decay_guard(allocation, {item.allocation_item_id: 0.6}, rollback_threshold=1.0)
    assert decisions[0].action == "reduce"
    assert decisions[0].proposed_weight == item.target_weight / 2
    assert apply_decay_guard(allocation, {item.allocation_item_id: 1.0}, rollback_threshold=1.0)[0].action == "rollback"


def observation(status: str, filled: float = 0, *, exit_price: float | None = None) -> PaperExecutionObservation:
    return PaperExecutionObservation(
        paper_execution_observation_id=f"observation:{status}:{filled}", allocation_item_id="allocation-item:test", action_id="action:test", paper_order_id="00000000-0000-0000-0000-000000000001", status=status,
        requested_quantity=10, filled_quantity=filled, requested_price=100,
        fill_price=100.5 if filled else None, spread_bps=5 if filled else None,
        exit_price=exit_price, observed_at=AS_OF, available_at=AS_OF + timedelta(seconds=1),
    )


def test_execution_stays_calibration_pending_until_genuine_fill_and_attribution_closes() -> None:
    pending_observation = observation("submitted")
    pending_model = build_execution_model_snapshot("allocation:x", AS_OF, [pending_observation])
    assert pending_model.calibration_status == "calibration_pending"
    assert pending_model.sample_count == 0
    filled_observation = observation("filled", 10)
    calibrated_model = build_execution_model_snapshot("allocation:x", AS_OF, [filled_observation])
    assert calibrated_model.calibration_status == "calibrated"
    allocation = allocate_portfolio_for_tests([candidate("GOOD")], as_of=AS_OF, cash_hurdle=0.01)
    item = next(item for item in allocation.items if item.ticker == "GOOD")
    with pytest.raises(ValueError, match="genuine|does not belong"):
        attribute_paper_pnl(allocation, item, observation=pending_observation)
    realized = attribute_paper_pnl(
        allocation, item,
        observation=observation("exited", 10, exit_price=102).model_copy(
            update={"allocation_item_id": item.allocation_item_id}
        ),
    )
    assert realized.pnl_status == "realized"
    assert realized.realized_pnl == 15


def test_paper_observation_rejects_live_mode() -> None:
    with pytest.raises(ValueError):
        PaperExecutionObservation.model_validate({**observation("submitted").model_dump(), "execution_mode": "live"})
