"""Small typed factor catalog for point-in-time evaluation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


@dataclass(frozen=True, slots=True)
class InputSnapshot:
    """Immutable identity and values for one cutoff/scope evaluation."""

    identity: str
    input_cutoff: datetime
    values: Mapping[str, Any]
    evidence_refs: tuple[str, ...] = ()
    source_versions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.identity.strip():
            raise ValueError("factor input snapshot identity is required")
        if self.input_cutoff.tzinfo is None or self.input_cutoff.utcoffset() is None:
            raise ValueError("factor input cutoff must be timezone-aware")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "source_versions", MappingProxyType(dict(self.source_versions)))


class FactorParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MomentumParams(FactorParameters):
    lookback_days: int = Field(default=20, ge=1, le=252)


class EmptyFactorParameters(FactorParameters):
    pass


class FactorResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    key: str = Field(min_length=1)
    implementation_version: str = Field(min_length=1)
    available: bool
    value: float | None = None
    units: str | None = None
    evidence_refs: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


FactorEvaluator = Callable[[InputSnapshot, FactorParameters, Mapping[str, FactorResult]], FactorResult]


@dataclass(frozen=True)
class FactorDefinition:
    key: str
    implementation_version: str
    parameters_type: type[FactorParameters]
    requires: tuple[str, ...]
    dependencies: tuple[str, ...]
    evaluate: FactorEvaluator


def _unavailable(definition: FactorDefinition, *blockers: str) -> FactorResult:
    return FactorResult(
        key=definition.key,
        implementation_version=definition.implementation_version,
        available=False,
        blockers=tuple(dict.fromkeys(blockers)),
    )


def _price_momentum(
    snapshot: InputSnapshot,
    params: MomentumParams,
    _dependencies: Mapping[str, FactorResult],
) -> FactorResult:
    raw_values = snapshot.values.get("daily_closes")
    if not isinstance(raw_values, Sequence) or isinstance(raw_values, (str, bytes)):
        return _unavailable(PRICE_MOMENTUM, "daily_closes_missing")
    try:
        values = [float(value) for value in raw_values]
    except (TypeError, ValueError):
        return _unavailable(PRICE_MOMENTUM, "daily_closes_invalid")
    if any(not isfinite(value) or value <= 0 for value in values):
        return _unavailable(PRICE_MOMENTUM, "daily_closes_invalid")
    if len(values) <= params.lookback_days:
        return _unavailable(PRICE_MOMENTUM, "daily_closes_insufficient")
    start = values[-params.lookback_days - 1]
    return FactorResult(
        key=PRICE_MOMENTUM.key,
        implementation_version=PRICE_MOMENTUM.implementation_version,
        available=True,
        value=values[-1] / start - 1,
        units="return",
        evidence_refs=snapshot.evidence_refs,
    )


PRICE_MOMENTUM = FactorDefinition(
    key="price.momentum",
    implementation_version="1",
    parameters_type=MomentumParams,
    requires=("daily_closes",),
    dependencies=(),
    evaluate=_price_momentum,
)


FACTOR_CATALOG: Mapping[str, FactorDefinition] = MappingProxyType({PRICE_MOMENTUM.key: PRICE_MOMENTUM})


def validate_factor_catalog(definitions: Mapping[str, FactorDefinition]) -> None:
    """Reject malformed or cyclic static factor registrations."""

    for key, definition in definitions.items():
        if key != definition.key or not definition.implementation_version:
            raise ValueError("factor definition identity is inconsistent")
        if len(set(definition.requires)) != len(definition.requires):
            raise ValueError(f"factor {key} repeats an input dependency")
        missing = [dependency for dependency in definition.dependencies if dependency not in definitions]
        if missing:
            raise ValueError(f"factor {key} has missing dependencies: {', '.join(missing)}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise ValueError("factor dependency cycle detected")
        if key in visited:
            return
        visiting.add(key)
        for dependency in definitions[key].dependencies:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in definitions:
        visit(key)


def evaluate_factors(
    snapshot: InputSnapshot,
    requested: Mapping[str, Mapping[str, Any] | FactorParameters] | Sequence[str],
    *,
    definitions: Mapping[str, FactorDefinition] = FACTOR_CATALOG,
) -> dict[str, FactorResult]:
    """Evaluate requested factors once for this snapshot and parameter identity."""

    validate_factor_catalog(definitions)
    requests = requested.items() if isinstance(requested, Mapping) else ((key, {}) for key in requested)
    memo: dict[tuple[str, str, str], FactorResult] = {}

    def evaluate(key: str, raw_parameters: Mapping[str, Any] | FactorParameters) -> FactorResult:
        definition = definitions.get(key)
        if definition is None:
            raise ValueError(f"unknown factor: {key}")
        try:
            normalized_parameters = raw_parameters.model_dump() if isinstance(raw_parameters, BaseModel) else raw_parameters
            params = definition.parameters_type.model_validate(normalized_parameters)
        except ValidationError as exc:
            raise ValueError(f"factor parameters invalid for {key}") from exc
        parameter_identity = params.model_dump_json()
        memo_key = (snapshot.identity, f"{definition.key}:{definition.implementation_version}", parameter_identity)
        if memo_key in memo:
            return memo[memo_key]
        dependencies = {
            dependency: evaluate(dependency, {})
            for dependency in definition.dependencies
        }
        missing_inputs = tuple(
            f"{input_name}_missing" for input_name in definition.requires
            if input_name not in snapshot.values
        )
        unavailable_dependencies = tuple(
            f"dependency_{dependency}_unavailable"
            for dependency, result in dependencies.items()
            if not result.available
        )
        if missing_inputs or unavailable_dependencies:
            result = _unavailable(definition, *missing_inputs, *unavailable_dependencies)
        else:
            result = definition.evaluate(snapshot, params, dependencies)
            if result.key != definition.key or result.implementation_version != definition.implementation_version:
                raise ValueError(f"factor evaluator identity mismatch for {key}")
        memo[memo_key] = result
        return result

    return {key: evaluate(key, parameters) for key, parameters in requests}


__all__ = [
    "FACTOR_CATALOG", "EmptyFactorParameters", "FactorDefinition", "FactorParameters", "FactorResult",
    "InputSnapshot", "MomentumParams", "PRICE_MOMENTUM", "evaluate_factors", "validate_factor_catalog",
]
