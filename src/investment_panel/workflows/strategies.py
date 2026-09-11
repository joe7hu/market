"""Persisted strategy evaluation workflow for research and replay."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
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
    run_id: str | None = None
    evaluation_id: str | None = None
    evaluated_at: datetime | None = None


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
        run_record = None
        start_run = getattr(self.repository, "start_strategy_run", None)
        if callable(start_run):
            run_record = start_run(
                strategy_keys=keys,
                strategy_revisions=tuple((spec.strategy_key, spec.revision) for spec in specs),
                scopes=tuple(str(scope) for scope in inputs_by_scope),
                input_cutoff=_utc(input_cutoff), mode=mode,
            )
        results: list[StrategyRunResult] = []
        run_manifest_entries: list[dict[str, Any]] = []
        try:
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
                    if context is not None:
                        context.begin_evaluation()
                    signal = evaluate_strategy(
                        spec,
                        inputs,
                        account_actionability=account_actionability,
                        factor_context=context,
                    )
                    manifest = _input_manifest(spec, {**inputs, "scope": str(scope)}, snapshot, mode, context)
                    record_method = getattr(self.repository, "record_signal_evaluation_record", None)
                    if callable(record_method) and run_record is not None:
                        record = record_method(
                            spec.strategy_key, spec.revision, signal,
                            run_id=run_record["run_id"], scope=str(scope),
                            input_snapshot_identity=snapshot.content_identity if snapshot is not None else None,
                            input_cutoff=_utc(input_cutoff), mode=mode, input_manifest=manifest,
                        )
                        input_hash = str(record["input_hash"])
                        evaluation_id = str(record["evaluation_id"])
                        evaluated_at = _utc(record["evaluated_at"])
                    else:
                        input_hash = self.repository.record_signal_evaluation(
                            spec.strategy_key, spec.revision, signal,
                            scope=str(scope),
                            input_snapshot_identity=snapshot.content_identity if snapshot is not None else None,
                            input_cutoff=_utc(input_cutoff), mode=mode,
                        )
                        evaluation_id = None
                        evaluated_at = None
                    run_manifest_entries.append({
                        "scope": str(scope), "strategy_key": spec.strategy_key, "revision": spec.revision,
                        "input_hash": input_hash, "manifest": manifest,
                    })
                    results.append(StrategyRunResult(
                        scope=str(scope), strategy_key=spec.strategy_key, revision=spec.revision,
                        mode=mode, input_cutoff=_utc(input_cutoff), generated_at=evaluated_at or generated_at,
                        signal=signal, input_hash=input_hash, run_id=run_record["run_id"] if run_record else None,
                        evaluation_id=evaluation_id, evaluated_at=evaluated_at,
                    ))
                if snapshot is not None:
                    contexts.pop(f"{scope}:{snapshot.content_identity}", None)
            if run_record is not None:
                self.repository.finish_strategy_run(
                    run_record["run_id"], status="succeeded",
                    summary={"result_count": len(results), "available_count": sum(item.signal.status == "available" for item in results)},
                    input_manifest=_resolved_run_manifest(run_manifest_entries),
                )
        except BaseException as exc:
            if run_record is not None:
                self.repository.finish_strategy_run(
                    run_record["run_id"], status="failed", summary={"error": type(exc).__name__, "result_count": len(results)},
                    input_manifest=_resolved_run_manifest(run_manifest_entries),
                )
            raise
        return tuple(results)


def _input_manifest(spec: Any, inputs: Mapping[str, Any], snapshot: Any, mode: str, context: EvaluationContext | None) -> dict[str, Any]:
    return _jsonable({
        "mode": mode,
        "scope": str(inputs.get("scope") or inputs.get("symbol") or ""),
        "strategy": {
            "key": spec.strategy_key, "revision": spec.revision,
            "implementation_id": spec.implementation_id, "implementation_version": spec.implementation_version,
            "parameters": spec.parameters, "definition_blockers": spec.blockers,
        },
        "snapshot": {
            "identity": snapshot.identity if snapshot is not None else None,
            "content_identity": snapshot.content_identity if snapshot is not None else None,
            "cutoff": inputs.get("input_cutoff"),
            "required_trading_dates": inputs.get("required_trading_dates", ()),
            "calendar_policy": inputs.get("calendar_policy"),
            "evidence_refs": inputs.get("evidence_refs", ()),
            "source_versions": inputs.get("source_versions", {}),
            "consumed_values": {key: value for key, value in inputs.items() if key in {
                "event", "benchmark_symbol", "benchmark_evidence_refs", "full_chain_state",
                "oi_volume_state", "dividend_state", "quote_quality", "fill_model_proven",
            }},
            "daily_open_values": [
                row.get("open") for row in inputs.get("daily_bars", ())
                if isinstance(row, Mapping)
            ],
            "daily_open_dates": [
                row.get("trading_date") or row.get("date") for row in inputs.get("daily_bars", ())
                if isinstance(row, Mapping)
            ],
        },
        "factor_computation": {
            "catalog_identity": context.catalog_identity if context is not None else None,
            "trace": context.evaluation_trace if context is not None else (),
            "manifest": context.evaluation_manifest if context is not None else (),
        },
    })


def _resolved_run_manifest(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "evaluations": sorted(
            (_jsonable(dict(entry)) for entry in entries),
            key=lambda item: (str(item.get("scope", "")), str(item.get("strategy_key", "")), int(item.get("revision", 0))),
        ),
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


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
