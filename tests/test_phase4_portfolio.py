from datetime import UTC, datetime, timedelta

import pytest
from app.contracts import OptionsHistoryToggleInput
from app.routers.portfolio import delete_watchlist_symbol_endpoint, set_watchlist_options_history_endpoint
from investment_panel.database.options_paper_execution import OptionsPaperExecutionRepository
from investment_panel.database.portfolio import PortfolioLoopRepository
from investment_panel.core import portfolio as portfolio_core

from investment_panel.core.portfolio import (
    AuthoritativePortfolioBundle,
    PaperExecutionObservation,
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
    with pytest.raises(TypeError, match="AuthoritativePortfolioBundle"):
        allocate_portfolio([candidate("MAPPING").model_dump()], as_of=AS_OF)


def test_postgresql_bundle_hydration_rejects_caller_forged_authority() -> None:
    with pytest.raises(ValueError, match="issued by the repository"):
        AuthoritativePortfolioBundle._from_postgresql(
            source_payload={
                "input_cutoff": AS_OF, "authority_snapshot_id": "account:1",
                "source_rows": {"account": {}, "positions": [], "candidates": [], "tape": []},
                "candidate_provenance": [],
            },
            candidates=(), book=None, constraints=None, execution=None, scenario=None,
        )


def test_postgresql_authority_token_is_sealed_and_matches_persisted_digest() -> None:
    source = {
        "input_cutoff": AS_OF, "authority_snapshot_id": "account:1",
        "source_rows": {"account": {}, "positions": [], "candidates": [], "tape": []},
        "candidate_provenance": [],
    }
    with pytest.raises(ValueError, match="verified canonical source digest"):
        portfolio_core._PostgreSQLAuthorityToken._issue(AS_OF, "account:1", "a" * 64, object())
    token = portfolio_core._PostgreSQLAuthorityToken._issue(
        AS_OF, "account:1", portfolio_core.canonical_content_hash(source), portfolio_core._POSTGRESQL_AUTHORITY_SEAL,
    )
    assert token.cutoff == AS_OF
    assert token.snapshot_id == "account:1"
    with pytest.raises(ValueError, match="source digest"):
        AuthoritativePortfolioBundle._from_postgresql(
            source_payload=source, candidates=(), book=None, constraints=None, execution=None, scenario=None,
            authority=portfolio_core._PostgreSQLAuthorityToken._issue(AS_OF, "account:1", "b" * 64, portfolio_core._POSTGRESQL_AUTHORITY_SEAL),
        )


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
        "policy_result": {"trade_plan_id": "action:test", "entry_quote": {"bid": 99, "ask": 101}, "impact_bps": 3.0},
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
    assert seen[0].observed_at == order["submitted_at"]
    assert seen[0].available_at == order["filled_at"]
    assert seen[0].latency_ms == 60_000
    assert seen[0].spread_bps == pytest.approx(200)
    assert connection.calls == 2


def test_calibration_pending_authority_can_only_return_cash() -> None:
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
    allocation = portfolio_core._allocate_portfolio(bundle, as_of=AS_OF)
    assert allocation.status == "cash_only"
    assert allocation.metadata["safe_state_reason"] == "execution_calibration_pending"
    assert all(item.ticker == "CASH" or item.disposition != "selected" for item in allocation.items)


def test_risk_evidence_requires_all_six_persisted_inputs() -> None:
    with pytest.raises(ValueError):
        PortfolioImpactRiskEvidence.model_validate({
            "impact_id": "impact:test", "ticker": "ABC", "source_decision_id": "decision:test",
            "source_input_hash": "a" * 64, "source_decision_input_hash": "b" * 64,
            "input_cutoff": AS_OF, "expected_return": 0.1, "uncertainty": 0.01,
            "risk_budget": 0.1, "kelly_cap": 0.1, "drawdown_cap": 0.1, "capacity": 100,
            "covariance": {"ABC": 0.04},
        })


def test_allocator_rejects_duplicate_tickers_and_joint_constraints_fail_closed() -> None:
    with pytest.raises(ValueError, match="duplicate ticker"):
        allocate_portfolio_for_tests([candidate("A", ticker="ABC"), candidate("B", ticker="ABC")], as_of=AS_OF, cash_hurdle=0.01)
    constrained = AuthoritativePortfolioBundle.model_construct(
        input_cutoff=AS_OF, candidates=(
            candidate("A", factor_exposure={"market": 1.0}, sector="technology", asset_class="equity", greeks={"delta": 1.0}, liquidity={"score": 1.0}, venue="NYSE"),
            candidate("B", factor_exposure={"market": 1.0}, sector="technology", asset_class="equity", greeks={"delta": 1.0}, liquidity={"score": 1.0}, venue="NYSE"),
        ), complete=True, cash_hurdle=0.01,
        book=PortfolioBookEvidence.model_construct(net_liquidation=100_000, cash_available=100_000, cash_source_id="acct:test:cash", input_cutoff=AS_OF),
        constraints=PortfolioConstraintEvidence.model_construct(
            cash_hurdle=0.01, constraint_hash="constraints:test", risk_policy_hash="a" * 64,
            risk_policy_version="v1", position_limit=1, aggregate_loss_limit=1,
            factor_limits={"market": 0.01}, sector_limits={"technology": 0.01},
            asset_class_limits={"equity": 0.01}, greek_limits={"delta": 0.01},
            min_liquidity=0.5, allowed_venues=("NYSE",),
        ),
        execution=PortfolioExecutionEvidence.model_construct(snapshot_id="execution:ready", calibration_status="calibrated", sample_count=1, input_cutoff=AS_OF),
        scenario=PortfolioScenarioEvidence.model_construct(artifact_id="scenario:test", observations=(), input_cutoff=AS_OF),
        authority_snapshot_id="account:1", authority_content_hash="b" * 64, repository_authority=object(),
    )
    allocation = portfolio_core._allocate_portfolio(constrained, as_of=AS_OF)
    assert allocation.status == "cash_only"
    assert allocation.metadata["safe_state_reason"].startswith("joint_constraints_failed:")


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
