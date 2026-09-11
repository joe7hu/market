from datetime import UTC, datetime

import pytest

from investment_panel.domain.factors import (
    EvaluationContext,
    FactorDefinition,
    FactorDependency,
    FactorResult,
    FactorRequest,
    InputSnapshot,
    MomentumParams,
    RELATIVE_STRENGTH,
    evaluate_factors,
)
from investment_panel.domain.factors.catalog import EmptyFactorParameters


def test_momentum_factor_consumes_typed_parameters_and_preserves_pit_evidence() -> None:
    snapshot = InputSnapshot(
        "equity:AAA:2026-09-05:USD:split-adjusted",
        datetime(2026, 9, 5, 13, tzinfo=UTC),
        {"daily_closes": [100, 102, 108, 104]},
        evidence_refs=("price-fact:1",),
    )

    result = evaluate_factors(snapshot, {"price.momentum": MomentumParams(lookback_days=3)})["price.momentum"]

    assert result.available is True
    assert result.value == pytest.approx(0.04)
    assert result.evidence_refs == ("price-fact:1",)


def test_factor_engine_rejects_unknown_parameters_and_caches_shared_dependencies() -> None:
    calls = 0

    def shared(_snapshot, _params, _dependencies):
        nonlocal calls
        calls += 1
        return FactorResult(key="shared", implementation_version="1", available=True, value=2)

    def derived(_snapshot, _params, dependencies):
        return FactorResult(
            key="derived", implementation_version="1", available=True,
            value=dependencies["shared"].value,
        )

    definitions = {
        "shared": FactorDefinition("shared", "1", EmptyFactorParameters, (), (), shared),
        "derived": FactorDefinition("derived", "1", EmptyFactorParameters, (), ("shared",), derived),
        "derived-again": FactorDefinition("derived-again", "1", EmptyFactorParameters, (), ("shared",), lambda _s, _p, d: FactorResult(
            key="derived-again", implementation_version="1", available=True, value=d["shared"].value,
        )),
    }

    results = evaluate_factors(InputSnapshot("test", datetime(2026, 9, 5, 13, tzinfo=UTC), {}), ["derived", "derived-again"], definitions=definitions)

    assert results["derived"].value == 2
    assert results["derived-again"].value == 2
    assert calls == 1
    with pytest.raises(ValueError, match="parameters invalid"):
        evaluate_factors(
            InputSnapshot("test", datetime(2026, 9, 5, 13, tzinfo=UTC), {"daily_closes": [100, 101]}),
            {"price.momentum": {"unused": 1}},
        )


def test_factor_catalog_rejects_cycles_and_naive_cutoffs() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        InputSnapshot("test", datetime(2026, 9, 5, 13), {})

    def evaluator(_snapshot, _params, _dependencies):
        return FactorResult(key="a", implementation_version="1", available=True)

    cyclic = {
        "a": FactorDefinition("a", "1", EmptyFactorParameters, (), ("b",), evaluator),
        "b": FactorDefinition("b", "1", EmptyFactorParameters, (), ("a",), evaluator),
    }
    with pytest.raises(ValueError, match="cycle"):
        evaluate_factors(InputSnapshot("test", datetime(2026, 9, 5, 13, tzinfo=UTC), {}), ["a"], definitions=cyclic)


def test_momentum_preserves_required_window_and_ignores_invalid_history_outside_it() -> None:
    cutoff = datetime(2026, 9, 5, 13, tzinfo=UTC)
    missing = evaluate_factors(
        InputSnapshot("missing-window", cutoff, {"daily_closes": [100, None, 120, 130]}),
        {"price.momentum": {"lookback_days": 2}},
    )["price.momentum"]
    assert not missing.available
    assert "daily_closes_missing_window" in missing.blockers

    complete = InputSnapshot("complete-window", cutoff, {"daily_closes": [100, 102, 108, 104]})
    results = evaluate_factors(
        complete,
        [FactorRequest("price.momentum", {"lookback_days": 1}), FactorRequest("price.momentum", {"lookback_days": 3})],
    )
    assert results[FactorRequest("price.momentum", {"lookback_days": 1})].value == pytest.approx(104 / 108 - 1)
    assert results[FactorRequest("price.momentum", {"lookback_days": 3})].value == pytest.approx(0.04)

    outside = evaluate_factors(
        InputSnapshot("outside-window", cutoff, {"daily_closes": [float("nan"), 100, 110, 120]}),
        {"price.momentum": {"lookback_days": 2}},
    )["price.momentum"]
    inside = evaluate_factors(
        InputSnapshot("inside-window", cutoff, {"daily_closes": [100, float("nan"), 110, 120]}),
        {"price.momentum": {"lookback_days": 2}},
    )["price.momentum"]
    assert outside.available and outside.value == pytest.approx(0.2)
    assert not inside.available


@pytest.mark.parametrize("value", [True, float("nan"), float("inf")])
def test_momentum_rejects_boolean_and_non_finite_required_prices(value: object) -> None:
    result = evaluate_factors(
        InputSnapshot("invalid-price", datetime(2026, 9, 5, 13, tzinfo=UTC), {"daily_closes": [100, value, 120]}),
        {"price.momentum": {"lookback_days": 2}},
    )["price.momentum"]
    assert not result.available
    assert "invalid" in result.blockers[0]


def test_parameterized_dependencies_and_context_reuse_have_distinct_identities() -> None:
    calls: list[int] = []

    def base(_snapshot, params, _dependencies):
        calls.append(params.lookback_days)
        return FactorResult(key="base", implementation_version="1", available=True, value=params.lookback_days)

    def parent(_snapshot, _params, dependencies):
        return FactorResult(key="parent", implementation_version="1", available=True, value=dependencies["short"].value)

    definitions = {
        "base": FactorDefinition("base", "1", MomentumParams, (), (), base),
        "parent": FactorDefinition(
            "parent", "1", EmptyFactorParameters, (),
            (FactorDependency("short", FactorRequest("base", {"lookback_days": 20})),), parent,
        ),
    }
    snapshot = InputSnapshot("shared-context", datetime(2026, 9, 5, 13, tzinfo=UTC), {})
    context = EvaluationContext(snapshot)
    first = evaluate_factors(
        snapshot,
        [FactorRequest("base", {"lookback_days": 20}), FactorRequest("parent")],
        definitions=definitions,
        context=context,
    )
    second = evaluate_factors(snapshot, [FactorRequest("base", {"lookback_days": 20})], definitions=definitions, context=context)
    assert first[FactorRequest("parent")].value == 20
    assert second[FactorRequest("base", {"lookback_days": 20})].value == 20
    assert calls == [20]


def test_snapshot_freezes_nested_values_after_identity_is_established() -> None:
    bars = [100, 101]
    snapshot = InputSnapshot(
        "nested", datetime(2026, 9, 5, 13, tzinfo=UTC), {"payload": {"bars": bars}},
    )
    bars.append(102)
    assert snapshot.values["payload"]["bars"] == (100, 101)
    with pytest.raises(TypeError):
        snapshot.values["payload"]["bars"] += (103,)


def test_default_factor_requests_are_retrievable_by_original_and_explicit_identity() -> None:
    snapshot = InputSnapshot("defaults", datetime(2026, 9, 5, 13, tzinfo=UTC), {"daily_closes": [100 + item for item in range(25)]})
    results = evaluate_factors(snapshot, [FactorRequest("price.momentum"), FactorRequest("price.momentum", {"lookback_days": 20}, "2")])
    assert results.by_request(FactorRequest("price.momentum")).value == results.by_request(FactorRequest("price.momentum", MomentumParams(), "2")).value
    assert len(results) == 1


def test_factor_budget_counts_dependency_expansion_and_catalog_identity() -> None:
    def leaf(_snapshot, _params, _dependencies):
        return FactorResult(key="leaf", implementation_version="1", available=True, value=1)

    def parent(_snapshot, _params, dependencies):
        return FactorResult(key="parent", implementation_version="1", available=True, value=dependencies["leaf"].value)

    definitions = {
        "leaf": FactorDefinition("leaf", "1", EmptyFactorParameters, (), (), leaf),
        "parent": FactorDefinition("parent", "1", EmptyFactorParameters, (), (FactorDependency("leaf", FactorRequest("leaf")),), parent),
    }
    snapshot = InputSnapshot("budget", datetime(2026, 9, 5, 13, tzinfo=UTC), {})
    with pytest.raises(ValueError, match="computation bound"):
        evaluate_factors(snapshot, "parent", definitions=definitions, context=EvaluationContext(snapshot, max_computations=1))
    context = EvaluationContext(snapshot)
    evaluate_factors(snapshot, "parent", definitions=definitions, context=context)
    changed = {**definitions, "leaf": FactorDefinition("leaf", "2", EmptyFactorParameters, (), (), leaf)}
    with pytest.raises(ValueError, match="catalog"):
        evaluate_factors(snapshot, "parent", definitions=changed, context=context)


def test_factor_outputs_revalidate_constructed_models_and_reject_unsupported_inputs() -> None:
    snapshot = InputSnapshot("mutable", datetime(2026, 9, 5, 13, tzinfo=UTC), {})
    with pytest.raises(TypeError, match="unsupported mutable"):
        InputSnapshot("unsupported", snapshot.input_cutoff, {"object": object()})

    def malformed(_snapshot, _params, _dependencies):
        return FactorResult.model_construct(key="malformed", implementation_version="1", available=False, value=1)

    definitions = {"malformed": FactorDefinition("malformed", "1", EmptyFactorParameters, (), (), malformed)}
    with pytest.raises(ValueError, match="evaluator rejected"):
        evaluate_factors(snapshot, "malformed", definitions=definitions)


def test_relative_strength_requires_aligned_dated_windows() -> None:
    snapshot = InputSnapshot(
        "relative", datetime(2026, 9, 5, 13, tzinfo=UTC),
        {
            "daily_closes": [100, 110, 120], "daily_close_dates": ["2026-09-01", "2026-09-02", "2026-09-03"],
            "benchmark_closes": [100, 105, 110], "benchmark_close_dates": ["2026-09-01", "2026-09-03", "2026-09-04"],
        },
    )
    result = evaluate_factors(snapshot, {RELATIVE_STRENGTH.key: {"period": 2}})[RELATIVE_STRENGTH.key]
    assert not result.available
    assert any("misaligned" in blocker for blocker in result.blockers)
