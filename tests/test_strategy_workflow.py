from datetime import UTC, datetime

import pytest

import investment_panel.domain.signals.catalog as signal_catalog
import investment_panel.domain.strategies.implementations as strategy_implementations
from investment_panel.domain.strategies.catalog import evaluate_strategy, resolve_builtin_strategy
from investment_panel.domain.strategies.implementations import factor_snapshot_for_inputs
from investment_panel.domain.factors import EvaluationContext
from investment_panel.workflows.strategies import StrategyWorkflow, _input_manifest


class MemoryStrategyRepository:
    def __init__(self) -> None:
        self.specs = {
            key: resolve_builtin_strategy(key)
            for key in ("daily_trend_underreaction_v2", "volatility_aware_momentum_v1")
        }
        self.saved: list[tuple[str, str]] = []

    def resolve(self, strategy_key: str):
        return self.specs[strategy_key]

    def record_signal_evaluation(self, strategy_key, revision, signal, *, scope, input_snapshot_identity, input_cutoff, mode):
        value = f"{strategy_key}:{revision}:{scope}:{input_snapshot_identity}:{input_cutoff.isoformat()}:{mode}:{signal.model_dump_json()}"
        self.saved.append((strategy_key, value))
        return value


def _inputs() -> dict[str, dict[str, object]]:
    return {
        "AAA": {
            "input_cutoff": "2026-10-01T13:00:00Z",
            "input_snapshot_identity": "instrument:1:cutoff:2026-10-01T13:00:00Z",
            "daily_bars": [
                {"status": "confirmed", "confirmed": True, "disabled": False,
                 "observed_at": f"2026-09-{day:02d}T12:00:00Z", "available_at": f"2026-09-{day:02d}T12:00:00Z",
                 "trading_date": f"2026-09-{day:02d}", "close": 100 + day}
                for day in range(1, 26)
            ],
        },
    }


def test_persisted_strategy_workflow_reuses_context_and_replay_keeps_new_generation_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryStrategyRepository()
    cutoff = datetime(2026, 10, 1, 13, tzinfo=UTC)
    workflow = StrategyWorkflow(repository, clock=lambda: datetime(2026, 10, 2, 13, tzinfo=UTC))
    calls: list[object] = []
    original_strategy_evaluate_factors = strategy_implementations.evaluate_factors
    original_signal_evaluate_factors = signal_catalog.evaluate_factors

    def counted_strategy_evaluate_factors(*args, **kwargs):
        result = original_strategy_evaluate_factors(*args, **kwargs)
        calls.append(kwargs["context"])
        return result

    def counted_signal_evaluate_factors(*args, **kwargs):
        result = original_signal_evaluate_factors(*args, **kwargs)
        calls.append(kwargs["context"])
        return result

    monkeypatch.setattr(strategy_implementations, "evaluate_factors", counted_strategy_evaluate_factors)
    monkeypatch.setattr(signal_catalog, "evaluate_factors", counted_signal_evaluate_factors)
    results = workflow.run(
        tuple(repository.specs), _inputs(), input_cutoff=cutoff, mode="research",
    )
    assert [result.signal.status for result in results] == ["available", "available"]
    assert len(repository.saved) == 2
    assert results[0].generated_at > results[0].input_cutoff
    assert len(calls) == 2
    assert calls[0] is calls[1]
    assert len(calls[1].trace) == 2
    assert sum(item["key"] == "price.momentum" for item in calls[1].trace) == 1

    replay = workflow.run(
        tuple(repository.specs), _inputs(), input_cutoff=cutoff, mode="replay",
    )
    assert [result.signal.model_dump() for result in replay] == [result.signal.model_dump() for result in results]
    assert all(result.mode == "replay" for result in replay)


def test_strategy_manifests_are_local_while_context_memo_is_shared() -> None:
    repository = MemoryStrategyRepository()
    inputs = _inputs()["AAA"]
    snapshot = factor_snapshot_for_inputs(inputs)
    context = EvaluationContext(snapshot)
    manifests = []
    for spec in repository.specs.values():
        context.begin_evaluation()
        result = evaluate_strategy(spec, inputs, factor_context=context)
        manifests.append(_input_manifest(spec, {**inputs, "scope": "AAA"}, snapshot, "research", context))
        assert result.status == "available"
    first_keys = {item["key"] for item in manifests[0]["factor_computation"]["manifest"]}
    second_keys = {item["key"] for item in manifests[1]["factor_computation"]["manifest"]}
    assert first_keys == {"price.momentum"}
    assert second_keys == {"price.momentum", "risk.realized_volatility"}
