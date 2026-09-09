from datetime import UTC, datetime

import pytest

from investment_panel.domain.factors import (
    FactorDefinition,
    FactorResult,
    InputSnapshot,
    MomentumParams,
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
