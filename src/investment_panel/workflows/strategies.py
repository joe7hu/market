"""Persisted strategy evaluation workflow for research and replay."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from investment_panel.domain.factors import EvaluationContext
from investment_panel.domain.strategies.catalog import (
    StrategySignal,
    evaluate_strategy,
    factor_snapshot_for_inputs,
)
from investment_panel.infrastructure.postgres.strategy_factory import StrategyFactoryRepository


StrategyRunMode = Literal["research", "replay"]


@dataclass(frozen=True, slots=True)
class StrategyRunResult:
    scope: str
    strategy_key: str
    revision: int
    mode: StrategyRunMode
    input_cutoff: datetime
    generated_at: datetime
    signal: StrategySignal
    input_hash: str


class StrategyWorkflow:
    """Resolve exact revisions, evaluate compatible scopes, and publish evidence."""

    def __init__(
        self,
        repository: StrategyFactoryRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

    def run(
        self,
        strategy_keys: Sequence[str],
        inputs_by_scope: Mapping[str, Mapping[str, Any]],
        *,
        input_cutoff: datetime,
        mode: StrategyRunMode = "research",
        account_actionability: str | None = None,
    ) -> tuple[StrategyRunResult, ...]:
        if input_cutoff.tzinfo is None:
            raise ValueError("strategy workflow input cutoff must be timezone-aware")
        if mode not in {"research", "replay"}:
            raise ValueError("strategy workflow mode is invalid")
        keys = tuple(dict.fromkeys(str(key) for key in strategy_keys if str(key).strip()))
        if not keys or len(keys) > 256:
            raise ValueError("strategy workflow requires a bounded strategy set")
        if len(inputs_by_scope) > 10_000:
            raise ValueError("strategy workflow scope set exceeds bound")
        specs = tuple(self.repository.resolve(key) for key in keys)
        contexts: dict[str, EvaluationContext] = {}
        generated_at = _utc(self.clock())
        results: list[StrategyRunResult] = []
        for scope, raw_inputs in inputs_by_scope.items():
            inputs = dict(raw_inputs)
            inputs.setdefault("input_cutoff", input_cutoff.isoformat())
            if _cutoff(inputs.get("input_cutoff")) != _utc(input_cutoff):
                raise ValueError(f"strategy scope cutoff does not match requested cutoff: {scope}")
            snapshot = factor_snapshot_for_inputs(inputs)
            context = None
            if snapshot is not None:
                context_key = f"{scope}:{snapshot.content_identity}"
                context = contexts.get(context_key)
                if context is None:
                    context = EvaluationContext(snapshot)
                    contexts[context_key] = context
            for spec in specs:
                signal = evaluate_strategy(
                    spec,
                    inputs,
                    account_actionability=account_actionability,
                    factor_context=context,
                )
                input_hash = self.repository.record_signal_evaluation(
                    spec.strategy_key,
                    spec.revision,
                    signal,
                    scope=str(scope),
                    input_snapshot_identity=snapshot.content_identity if snapshot is not None else None,
                    input_cutoff=_utc(input_cutoff),
                    mode=mode,
                )
                results.append(StrategyRunResult(
                    scope=str(scope), strategy_key=spec.strategy_key, revision=spec.revision,
                    mode=mode, input_cutoff=_utc(input_cutoff), generated_at=generated_at,
                    signal=signal, input_hash=input_hash,
                ))
        return tuple(results)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("strategy workflow clock must be timezone-aware")
    return value.astimezone(UTC)


def _cutoff(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


__all__ = ["StrategyRunResult", "StrategyWorkflow"]
