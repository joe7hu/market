from datetime import UTC, datetime, timedelta

import pytest
from app.contracts import OptionsHistoryToggleInput
from app.routers.portfolio import delete_watchlist_symbol_endpoint, set_watchlist_options_history_endpoint
from investment_panel.database.options_paper_execution import OptionsPaperExecutionRepository
from investment_panel.database.portfolio import PortfolioLoopRepository
from investment_panel.database.ticker_execution import TickerPaperExecutionRepository
from investment_panel.core import portfolio as portfolio_core

from investment_panel.core.portfolio import (
    AuthoritativePortfolioBundle,
    PaperExecutionObservation,
    PortfolioAllocationSnapshot,
    PortfolioCandidate,
    PortfolioBookEvidence,
    PortfolioConstraintEvidence,
    PortfolioExecutionEvidence,
    PortfolioImpactRiskEvidence,
    PortfolioScenarioEvidence,
    allocate_portfolio,
    allocate_portfolio_for_tests,
    apply_decay_guard,
    apply_decay_to_allocation,
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
    with pytest.raises(TypeError, match="repository and PostgreSQL bound"):
        allocate_portfolio([candidate("MAPPING").model_dump()], as_of=AS_OF)


def test_postgresql_authority_has_no_importable_issuer_or_caller_hydrator() -> None:
    assert not hasattr(portfolio_core, "_POSTGRESQL_AUTHORITY_SEAL")
    assert not hasattr(portfolio_core, "_PostgreSQLAuthorityToken")
    assert not hasattr(AuthoritativePortfolioBundle, "_from_postgresql")


def test_production_allocator_rejects_a_caller_constructed_bundle() -> None:
    bundle = AuthoritativePortfolioBundle.model_construct(
        input_cutoff=AS_OF, candidates=(), complete=False,
        book=PortfolioBookEvidence.model_construct(input_cutoff=AS_OF),
        constraints=PortfolioConstraintEvidence.model_construct(constraint_hash="x"),
        execution=PortfolioExecutionEvidence.model_construct(input_cutoff=AS_OF),
        scenario=PortfolioScenarioEvidence.model_construct(input_cutoff=AS_OF),
        authority_snapshot_id="account:1", authority_content_hash="a" * 64,
    )
    with pytest.raises(TypeError, match="repository and PostgreSQL bound"):
        allocate_portfolio(bundle, as_of=AS_OF)


@pytest.mark.parametrize("field", ["factor_exposure", "greeks"])
def test_candidate_risk_dimensions_reject_non_finite_values(field: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        candidate("BAD", **{field: {"market": float("nan")}})


def test_constraint_dimensions_reject_invalid_limits() -> None:
    with pytest.raises(ValueError, match="positive finite"):
        PortfolioConstraintEvidence.model_validate({"constraint_hash": "x", "factor_limits": {"market": 0}})
    with pytest.raises(ValueError, match="non-negative"):
        PortfolioConstraintEvidence.model_validate({"constraint_hash": "x", "min_liquidity": -1})


def test_existing_paper_fill_uses_persisted_quote_and_distinct_execution_clocks(monkeypatch) -> None:
    order = {
        "id": "00000000-0000-0000-0000-000000000001", "quantity": 10, "filled_quantity": 10,
        "limit_price": 100, "intended_limit_price": 99.5, "actual_fill_price": 100.5,
        "submitted_at": AS_OF - timedelta(minutes=2), "filled_at": AS_OF - timedelta(minutes=1),
        "exit_at": None, "exit_price": None, "fees": 1.25, "entry_slippage": 1.0,
        "exit_slippage": None, "side": "buy", "status": "entered",
        "fill_evidence_at": AS_OF - timedelta(seconds=30),
        "execution_quote": {"bid": 99, "ask": 101}, "contract_multiplier": 100,
        "policy_result": {"trade_plan_id": "action:test"},
    }

    class Result:
        def __init__(self, row): self.row = row
        def fetchone(self): return self.row

    class Connection:
        def __init__(self): self.calls = 0
        def execute(self, *_args):
            self.calls += 1
            return Result(order if self.calls == 1 else {"allocation_item_id": "allocation-item:test", "action_id": "action:test"})

    repository = PortfolioLoopRepository.__new__(PortfolioLoopRepository)
    seen = []
    monkeypatch.setattr(repository, "record_paper_execution", lambda observation, **_kwargs: (seen.append(observation), observation.paper_execution_observation_id)[1])
    connection = Connection()
    observation_id = repository.record_existing_paper_order_fill(
        connection, paper_order_id=order["id"], observed_at=AS_OF, status="filled",
    )
    assert observation_id == "paper-observation:00000000-0000-0000-0000-000000000001:10:"
    assert seen[0].observed_at == order["filled_at"]
    assert seen[0].available_at == order["fill_evidence_at"]
    assert seen[0].latency_ms == 60_000
    assert seen[0].spread_bps == pytest.approx(200)
    assert connection.calls == 2


def test_existing_paper_fill_never_uses_policy_metadata_for_calibration(monkeypatch) -> None:
    order = {
        "id": "00000000-0000-0000-0000-000000000001", "quantity": 1, "filled_quantity": 1,
        "limit_price": 100, "intended_limit_price": 100, "actual_fill_price": 100,
        "submitted_at": AS_OF - timedelta(minutes=2), "filled_at": AS_OF - timedelta(minutes=1),
        "fill_evidence_at": AS_OF - timedelta(seconds=30), "execution_quote": None,
        "contract_multiplier": None, "fees": 0, "entry_slippage": 0, "exit_slippage": None,
        "exit_at": None, "exit_price": None, "side": "buy", "status": "entered",
        "policy_result": {"trade_plan_id": "action:test", "entry_quote": {"bid": 1, "ask": 2}, "assignment": {"multiplier": 100}},
    }
    class Result:
        def fetchone(self): return order
    class Connection:
        def execute(self, *_args): return Result()
    repository = PortfolioLoopRepository.__new__(PortfolioLoopRepository)
    monkeypatch.setattr(repository, "record_paper_execution", lambda *_args, **_kwargs: pytest.fail("caller policy must not calibrate"))
    assert repository.record_existing_paper_order_fill(Connection(), paper_order_id=order["id"], observed_at=AS_OF, status="filled") is None


def test_record_paper_execution_rebuilds_observation_from_persisted_fill(monkeypatch) -> None:
    order_id = "00000000-0000-0000-0000-000000000001"
    submitted = AS_OF - timedelta(minutes=2)
    filled = AS_OF - timedelta(minutes=1)
    evidence = AS_OF - timedelta(seconds=30)
    order = {
        "id": order_id, "created_at": AS_OF - timedelta(minutes=3), "status": "entered", "side": "buy",
        "quantity": 10, "filled_quantity": 10, "actual_fill_price": 100.5, "exit_price": None,
        "limit_price": 100, "intended_limit_price": 99.5, "submitted_at": submitted, "filled_at": filled,
        "fill_evidence_at": evidence, "execution_quote": {"bid": 99, "ask": 101}, "fees": 1.25,
        "entry_slippage": 1.0, "exit_slippage": None, "contract_multiplier": 100, "exit_at": None,
        "policy_result": {"trade_plan_id": "action:test"},
    }
    observation = PaperExecutionObservation(
        paper_execution_observation_id="paper-observation:test", allocation_item_id="allocation-item:test",
        action_id="action:test", paper_order_id=order_id, status="filled", requested_quantity=10,
        filled_quantity=10, requested_price=99.5, fill_price=100.5, spread_bps=999, latency_ms=999,
        impact_bps=999, observed_at=filled, available_at=evidence,
        metadata={"fees": 1.25, "paper_order_id": order_id, "contract_multiplier": 100, "quote": {"bid": 99, "ask": 101}, "submitted_at": submitted, "filled_at": filled},
    )

    class Result:
        def __init__(self, one=None, many=None): self.one, self.many = one, many
        def fetchone(self): return self.one
        def fetchall(self): return self.many or []

    class Connection:
        def __init__(self): self.calls = 0
        def execute(self, statement, _parameters=None):
            self.calls += 1
            if "SELECT id, created_at" in statement:
                return Result(order)
            if "SELECT allocation_id, action_id" in statement:
                return Result({"allocation_id": "allocation:test", "action_id": "action:test"})
            if "phase4_telemetry_authorization_payload" in statement:
                return Result({"payload": "test-payload"})
            if "paper_execution_observation_id" in statement:
                return Result(many=[observation.model_copy(update={"spread_bps": 200, "latency_ms": 60_000, "impact_bps": 100}).model_dump()])
            if "SELECT allocation_id, input_cutoff" in statement:
                return Result({"allocation_id": "allocation:test", "input_cutoff": AS_OF})
            return Result()

    repository = PortfolioLoopRepository.__new__(PortfolioLoopRepository)
    stored = []
    monkeypatch.setattr(repository, "store_execution_model", lambda _connection, model: stored.append(model))
    connection = Connection()
    result = repository.record_paper_execution(observation, connection=connection)
    assert result == observation.paper_execution_observation_id
    assert stored and stored[0].latency_ms == 60_000
    assert stored[0].spread_bps == pytest.approx(200)
    assert stored[0].input_cutoff == evidence


def test_imported_allocator_cannot_consume_a_caller_bundle() -> None:
    bundle = AuthoritativePortfolioBundle.model_construct(
        input_cutoff=AS_OF, candidates=(candidate("GOOD"),), complete=True,
        cash_hurdle=0.01,
        book=PortfolioBookEvidence.model_construct(
            net_liquidation=100_000, cash_available=100_000, cash_source_id="acct:test:cash", input_cutoff=AS_OF,
        ),
        constraints=PortfolioConstraintEvidence.model_construct(
            cash_hurdle=0.01, constraint_hash="constraints:test", risk_policy_hash="a" * 64,
            risk_policy_version="v1", position_limit=1, aggregate_loss_limit=1,
        ),
        execution=PortfolioExecutionEvidence.model_construct(
            snapshot_id="execution:pending", calibration_status="calibration_pending", sample_count=0, input_cutoff=AS_OF,
        ),
        scenario=PortfolioScenarioEvidence.model_construct(artifact_id="scenario:test", observations=(), input_cutoff=AS_OF),
        authority_snapshot_id="account:1", authority_content_hash="b" * 64, repository_authority=object(),
    )
    with pytest.raises(TypeError, match="PostgreSQL repository"):
        portfolio_core._allocate_portfolio(bundle, as_of=AS_OF)


def test_risk_evidence_requires_all_six_persisted_inputs() -> None:
    with pytest.raises(ValueError):
        PortfolioImpactRiskEvidence.model_validate({
            "impact_id": "impact:test", "ticker": "ABC", "source_decision_id": "decision:test",
            "source_input_hash": "a" * 64, "source_decision_input_hash": "b" * 64,
            "input_cutoff": AS_OF, "expected_return": 0.1, "uncertainty": 0.01,
            "risk_budget": 0.1, "kelly_cap": 0.1, "drawdown_cap": 0.1, "capacity": 100,
            "covariance": {"ABC": 0.04},
        })


def test_allocator_resolves_distinct_candidates_with_one_ticker_and_joint_constraints() -> None:
    duplicate_ticker = allocate_portfolio_for_tests([candidate("A", ticker="ABC"), candidate("B", ticker="ABC")], as_of=AS_OF, cash_hurdle=0.01)
    assert {item.candidate_id for item in duplicate_ticker.items if item.ticker == "ABC"} == {"A", "B"}
    constrained_candidates = [
            candidate("A", factor_exposure={"market": 1.0}, sector="technology", asset_class="equity", greeks={"delta": 1.0}, liquidity={"score": 1.0}, venue="NYSE"),
            candidate("B", factor_exposure={"market": 1.0}, sector="technology", asset_class="equity", greeks={"delta": 1.0}, liquidity={"score": 1.0}, venue="NYSE"),
        ]
    constraint_evidence = PortfolioConstraintEvidence.model_construct(
            cash_hurdle=0.01, constraint_hash="constraints:test", risk_policy_hash="a" * 64,
            risk_policy_version="v1", position_limit=1, aggregate_loss_limit=1,
            factor_limits={"market": 0.01}, sector_limits={"technology": 0.01},
            asset_class_limits={"equity": 0.01}, greek_limits={"delta": 0.01},
            min_liquidity=0.5, allowed_venues=("NYSE",),
    )
    allocation = allocate_portfolio_for_tests(
        constrained_candidates, as_of=AS_OF, cash_hurdle=0.01,
        book=PortfolioBookEvidence.model_construct(net_liquidation=100_000, cash_available=100_000, cash_source_id="acct:test:cash", input_cutoff=AS_OF),
        constraints=constraint_evidence,
        execution=PortfolioExecutionEvidence.model_construct(snapshot_id="execution:ready", calibration_status="calibrated", sample_count=1, input_cutoff=AS_OF),
    )
    selected = [item for item in allocation.items if item.disposition == "selected" and item.ticker != "CASH"]
    assert allocation.status == "available"
    assert sum(item.target_weight for item in selected) <= 0.01 + 1e-9
    assert all(item.trace["optimizer"] == "SLSQP" for item in selected)


def test_joint_optimizer_handles_required_trim_and_existing_holding_without_self_funding() -> None:
    held = candidate("HELD", current_weight=.40, trim_position_id="broker-position:held", trim_available=.40, capacity=.01)
    fresh = candidate("FRESH", cash_available=20_000, cash_source_id="cash:1")
    allocation = allocate_portfolio_for_tests(
        [held, fresh], as_of=AS_OF, cash_hurdle=.01,
        book=PortfolioBookEvidence.model_construct(net_liquidation=100_000, cash_available=20_000, cash_source_id="cash:1", positions={"HELD": "broker-position:held", "LEGACY": "broker-position:legacy"}, position_weights={"HELD": .4, "LEGACY": .4}, input_cutoff=AS_OF),
        constraints=PortfolioConstraintEvidence.model_construct(cash_hurdle=.01, constraint_hash="constraints:test", risk_policy_hash="a" * 64, risk_policy_version="v1", position_limit=1, aggregate_loss_limit=1),
        execution=PortfolioExecutionEvidence.model_construct(snapshot_id="execution:ready", calibration_status="calibrated", sample_count=1, input_cutoff=AS_OF),
    )
    held_item = next(item for item in allocation.items if item.ticker == "HELD")
    assert held_item.target_weight <= .01 + 1e-9
    assert held_item.funding_source is None
    assert sum(item.target_weight for item in allocation.items if item.ticker == "CASH") <= .2 + 1e-9


def test_joint_optimizer_conserves_multiple_trim_sources_for_one_increase() -> None:
    evidence = {"factor_exposure": {"market": 0}, "sector": "technology", "asset_class": "equity",
                "greeks": {"delta": 0}, "liquidity": {"score": 1}, "venue": "NYSE"}
    trim_a = candidate("TRIM_A", current_weight=.1, expected_return=.01, uncertainty=.01,
                       trim_position_id="broker-position:a", trim_available=.1, **evidence)
    trim_b = candidate("TRIM_B", current_weight=.1, expected_return=.01, uncertainty=.01,
                       trim_position_id="broker-position:b", trim_available=.1, **evidence)
    fresh = candidate("FRESH", capacity=.15, risk_budget=.15, kelly_cap=.15, drawdown_cap=.15,
                      cash_available=.001, cash_source_id="cash:1", **evidence)
    allocation = allocate_portfolio_for_tests(
        [trim_a, trim_b, fresh], as_of=AS_OF, cash_hurdle=.01,
        book=PortfolioBookEvidence.model_construct(
            net_liquidation=1, cash_available=.001, cash_source_id="cash:1",
            positions={"TRIM_A": "broker-position:a", "TRIM_B": "broker-position:b"},
            position_weights={"TRIM_A": .1, "TRIM_B": .1}, input_cutoff=AS_OF,
        ),
        constraints=PortfolioConstraintEvidence.model_construct(
            cash_hurdle=.01, constraint_hash="constraints:test", risk_policy_hash="a" * 64,
            risk_policy_version="v1", position_limit=1, aggregate_loss_limit=1,
        ),
        execution=PortfolioExecutionEvidence.model_construct(snapshot_id="execution:ready", calibration_status="calibrated", sample_count=1, input_cutoff=AS_OF),
    )
    funded = next(item for item in allocation.items if item.ticker == "FRESH")
    assert funded.disposition == "selected"
    assert sum(funded.trace["funding_sources"].values()) == pytest.approx(funded.funding_amount)
    assert funded.funding_sources == funded.trace["funding_sources"]
    assert funded.funding_source == "MULTI_SOURCE"
    assert set(funded.funding_sources) == {
        "CASH:cash:1", "TRIM:broker-position:a", "TRIM:broker-position:b",
    }


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


def test_decay_persists_rollback_evidence_and_releases_weight_to_cash() -> None:
    allocation = allocate_portfolio_for_tests([candidate("GOOD")], as_of=AS_OF, cash_hurdle=0.01)
    # Exercise the canonical reconstruction path with a valid but non-canonical
    # row order.  The immutable allocation identity is order-independent;
    # decay must use the same canonical ordering when issuing its replacement.
    allocation = PortfolioAllocationSnapshot.model_validate({
        **allocation.model_dump(), "items": tuple(reversed(allocation.items)),
    })
    item = next(item for item in allocation.items if item.ticker == "GOOD")
    adjusted, decisions = apply_decay_to_allocation(
        allocation, {item.allocation_item_id: 1.0}, rollback_threshold=1.0,
    )
    rollback = next(row for row in adjusted.items if row.ticker == "GOOD")
    cash = next(row for row in adjusted.items if row.ticker == "CASH")
    assert rollback.disposition == "rollback"
    assert rollback.target_weight == 0
    assert rollback.trace["rollback_evidence"]["released_weight"] == item.target_weight
    assert cash.target_weight == 1
    assert decisions[0].allocation_id == adjusted.allocation_id
    assert decisions[0].allocation_item_id == rollback.allocation_item_id


def observation(status: str, filled: float = 0, *, exit_price: float | None = None) -> PaperExecutionObservation:
    return PaperExecutionObservation(
        paper_execution_observation_id=f"observation:{status}:{filled}", allocation_item_id="allocation-item:test", action_id="action:test", paper_order_id="00000000-0000-0000-0000-000000000001", status=status,
        requested_quantity=10, filled_quantity=filled, requested_price=100,
        fill_price=100.5 if filled else None, spread_bps=5 if filled else None,
        exit_price=exit_price, observed_at=AS_OF, available_at=AS_OF + timedelta(seconds=1),
        metadata={"paper_order_id": "00000000-0000-0000-0000-000000000001", "submitted_at": AS_OF - timedelta(seconds=1), "filled_at": AS_OF, "contract_multiplier": 1, "fees": 0},
    )


def test_execution_stays_calibration_pending_until_genuine_fill_and_attribution_closes() -> None:
    pending_observation = observation("submitted")
    pending_model = build_execution_model_snapshot("allocation:x", AS_OF, [pending_observation])
    assert pending_model.calibration_status == "calibration_pending"
    assert pending_model.sample_count == 0
    filled_observation = observation("filled", 10)
    calibrated_model = build_execution_model_snapshot("allocation:x", AS_OF + timedelta(seconds=2), [filled_observation])
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


def test_assignment_attribution_uses_persisted_multiplier_and_total_fees() -> None:
    allocation = allocate_portfolio_for_tests([candidate("GOOD")], as_of=AS_OF, cash_hurdle=0.01)
    item = next(item for item in allocation.items if item.ticker == "GOOD")
    assigned = observation("exited", 2, exit_price=102).model_copy(update={
        "allocation_item_id": item.allocation_item_id,
        "metadata": {"paper_order_id": "00000000-0000-0000-0000-000000000001", "contract_multiplier": 100, "fees": 2.60},
    })
    realized = attribute_paper_pnl(allocation, item, observation=assigned)
    assert realized.attribution["pnl"]["gross"] == 300
    assert realized.realized_pnl == 297.4


def test_attribution_aggregates_each_persisted_partial_exit_and_fee() -> None:
    allocation = allocate_portfolio_for_tests([candidate("GOOD")], as_of=AS_OF, cash_hurdle=0.01)
    item = next(item for item in allocation.items if item.ticker == "GOOD")
    partial = observation("partial_exited", 1, exit_price=101).model_copy(update={
        "allocation_item_id": item.allocation_item_id,
        "metadata": {"paper_order_id": "00000000-0000-0000-0000-000000000001", "contract_multiplier": 100, "fees": 1.25},
    })
    final = observation("exited", 1, exit_price=103).model_copy(update={
        "allocation_item_id": item.allocation_item_id,
        "paper_execution_observation_id": "observation:final",
        "metadata": {"paper_order_id": "00000000-0000-0000-0000-000000000001", "contract_multiplier": 100, "fees": 1.75},
    })
    realized = attribute_paper_pnl(allocation, item, observation=final, observations=[partial, final])
    assert realized.attribution["pnl"]["gross"] == 300
    assert realized.realized_pnl == 297
    assert realized.attribution["pnl"]["quantity"] == 2


def test_csp_assignment_charges_each_contract_and_persists_multiplier(monkeypatch) -> None:
    class Result:
        def __init__(self, row): self.row = row
        def fetchone(self): return self.row

    class Connection:
        def __init__(self): self.params = None
        def execute(self, _query, params):
            self.params = params
            return Result({"price": 90})

    repository = TickerPaperExecutionRepository.__new__(TickerPaperExecutionRepository)
    repository.runtime = object()
    monkeypatch.setattr(repository, "_stored_option_legs", lambda *_args: [{"strike": 100, "multiplier": 100, "expiration": AS_OF.date()}])
    seen = []
    monkeypatch.setattr(PortfolioLoopRepository, "record_existing_paper_order_fill", lambda *_args, **kwargs: seen.append(kwargs))
    order = {
        "id": "00000000-0000-0000-0000-000000000001", "instrument_id": 1,
        "expression_kind": "CASH_SECURED_PUT", "structure": "cash_secured_put", "quantity": 2,
        "filled_quantity": 2, "exited_quantity": 0, "policy_result": {}, "side": "sell",
        "expires_at": AS_OF.date(),
    }
    result = repository._manage_option_open(Connection(), order, AS_OF, 2)
    assert result["reason"] == "assignment"
    assert result["assigned_strike"] == 100
    assert seen == [{"paper_order_id": order["id"], "observed_at": AS_OF, "status": "exited"}]


def test_execution_snapshot_persistence_rechecks_canonical_digest() -> None:
    model = build_execution_model_snapshot("allocation:test", AS_OF, [])

    class Result:
        def fetchone(self):
            return {"content_hash": portfolio_core.canonical_content_hash(model)}

    class Connection:
        def __init__(self): self.calls = 0
        def execute(self, statement, *_args):
            self.calls += 1
            if "phase4_telemetry_authorization_payload" in statement:
                class Payload:
                    def fetchone(self): return {"payload": "test-payload"}
                return Payload()
            return Result()

    connection = Connection()
    PortfolioLoopRepository.store_execution_model(connection, model)
    assert connection.calls == 3


def test_paper_observation_rejects_live_mode() -> None:
    with pytest.raises(ValueError):
        PaperExecutionObservation.model_validate({**observation("submitted").model_dump(), "execution_mode": "live"})


def test_legacy_mutation_routes_keep_their_error_contract() -> None:
    class FailingActions:
        @staticmethod
        def delete_watchlist_symbol(_symbol: str) -> None:
            raise ValueError("missing symbol")

    class FailingOptions:
        @staticmethod
        def set_history_requested_state(_symbol: str, _payload: dict[str, object]) -> None:
            raise RuntimeError("database unavailable")

        @staticmethod
        def is_policy_conflict(_exc: Exception) -> bool:
            return False

    with pytest.raises(Exception) as delete_error:
        delete_watchlist_symbol_endpoint("MISSING", FailingActions())
    assert getattr(delete_error.value, "status_code", None) == 400
    with pytest.raises(Exception) as options_error:
        set_watchlist_options_history_endpoint(
            "MISSING", OptionsHistoryToggleInput(requested_state="on", lock_version=1), FailingOptions(),
        )
    assert getattr(options_error.value, "status_code", None) == 400


def test_option_fill_bridge_is_active_only_for_a_real_runtime(monkeypatch) -> None:
    repository = OptionsPaperExecutionRepository.__new__(OptionsPaperExecutionRepository)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        PortfolioLoopRepository,
        "record_existing_paper_order_fill",
        lambda _self, _connection, **kwargs: calls.append(kwargs),
    )
    repository._record_phase4_fill(None, paper_order_id="paper:1", observed_at=AS_OF, status="filled")
    assert calls == []
    repository.runtime = object()
    repository._record_phase4_fill(None, paper_order_id="paper:1", observed_at=AS_OF, status="filled")
    assert calls == [{"paper_order_id": "paper:1", "observed_at": AS_OF, "status": "filled"}]
