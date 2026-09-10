"""Small typed factor catalog for point-in-time evaluation.

Factors are pure functions over an immutable input snapshot. The evaluation
context is deliberately short-lived: it shares work within one application
run without creating a process-global financial cache.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from hashlib import sha256
import json
from math import isfinite, log, sqrt
from statistics import pstdev
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


def _freeze(value: Any) -> Any:
    """Recursively freeze the supported snapshot value types."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list, frozenset, set)):
        items = [_jsonable(item) for item in value]
        return sorted(items, key=str) if isinstance(value, (frozenset, set)) else items
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


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
        object.__setattr__(self, "input_cutoff", self.input_cutoff.astimezone(UTC))
        object.__setattr__(self, "values", _freeze(dict(self.values)))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "source_versions", _freeze(dict(self.source_versions)))

    @property
    def content_identity(self) -> str:
        return _digest({
            "identity": self.identity,
            "input_cutoff": self.input_cutoff,
            "values": self.values,
            "evidence_refs": self.evidence_refs,
            "source_versions": self.source_versions,
        })


class FactorParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MomentumParams(FactorParameters):
    lookback_days: int = Field(default=20, ge=1, le=252)


class VolatilityParams(FactorParameters):
    period: int = Field(default=20, ge=2, le=252)


class RelativeStrengthParams(FactorParameters):
    period: int = Field(default=20, ge=1, le=252)


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
    request_id: str | None = None
    parameter_identity: str = "{}"

    @model_validator(mode="before")
    @classmethod
    def reject_boolean_values(cls, values: Any) -> Any:
        if isinstance(values, Mapping) and isinstance(values.get("value"), bool):
            raise ValueError("factor result value cannot be boolean")
        return values

    @model_validator(mode="after")
    def validate_availability(self) -> "FactorResult":
        if self.available and self.value is None:
            raise ValueError("available factor result requires a value")
        if not self.available and self.value is not None:
            raise ValueError("unavailable factor result cannot contain a value")
        return self


@dataclass(frozen=True, slots=True)
class FactorRequest:
    """One factor implementation and validated-parameter request."""

    key: str
    parameters: Mapping[str, Any] | FactorParameters = field(default_factory=dict)
    implementation_version: str | None = None

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("factor request key is required")
        values = self.parameters.model_dump(mode="python") if isinstance(self.parameters, BaseModel) else dict(self.parameters)
        object.__setattr__(self, "parameters", _freeze(values))
        if self.implementation_version is not None and not self.implementation_version.strip():
            raise ValueError("factor request implementation version cannot be empty")

    @property
    def parameter_identity(self) -> str:
        return json.dumps(_jsonable(self.parameters), sort_keys=True, separators=(",", ":"))

    @property
    def request_id(self) -> str:
        return f"{self.key}:{self.implementation_version or '?'}:{_digest(self.parameters)[:16]}"

    def __hash__(self) -> int:
        return hash((self.key, self.implementation_version, self.parameter_identity))


@dataclass(frozen=True, slots=True)
class FactorDependency:
    """A local dependency name bound to one exact factor request."""

    name: str
    request: FactorRequest

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("factor dependency name is required")


Dependency = FactorDependency


@dataclass
class EvaluationContext:
    """Per-run factor memo and trace for one immutable input snapshot."""

    snapshot: InputSnapshot
    memo: dict[tuple[str, str], FactorResult] = field(default_factory=dict)
    trace: list[dict[str, str]] = field(default_factory=list)
    max_computations: int = 4096

    def __post_init__(self) -> None:
        if self.max_computations <= 0:
            raise ValueError("factor evaluation computation bound must be positive")


FactorEvaluator = Callable[[InputSnapshot, FactorParameters, Mapping[str, FactorResult]], FactorResult]


@dataclass(frozen=True, slots=True)
class FactorDefinition:
    key: str
    implementation_version: str
    parameters_type: type[FactorParameters]
    requires: tuple[str, ...]
    dependencies: tuple[str | FactorDependency, ...]
    evaluate: FactorEvaluator


class FactorResults(dict[FactorRequest, FactorResult]):
    """Request-keyed results with a safe compatibility lookup by factor key."""

    def __getitem__(self, key: FactorRequest | str) -> FactorResult:
        if isinstance(key, FactorRequest):
            try:
                return super().__getitem__(key)
            except KeyError:
                matches = [
                    result
                    for request, result in self.items()
                    if request.key == key.key
                    and request.parameter_identity == key.parameter_identity
                    and (key.implementation_version is None or request.implementation_version == key.implementation_version)
                ]
                if len(matches) == 1:
                    return matches[0]
                raise
        matches = [result for request, result in self.items() if request.request_id == key]
        if not matches:
            matches = [result for request, result in self.items() if request.key == key]
        if len(matches) != 1:
            raise KeyError(key)
        return matches[0]

    def by_request(self, request: FactorRequest) -> FactorResult:
        return self[request]


def _dependency_parts(dependency: str | FactorDependency) -> tuple[str, FactorRequest]:
    if isinstance(dependency, FactorDependency):
        return dependency.name, dependency.request
    return dependency, FactorRequest(dependency)


def _unavailable(
    definition: FactorDefinition,
    *blockers: str,
    request: FactorRequest | None = None,
) -> FactorResult:
    return FactorResult(
        key=definition.key,
        implementation_version=definition.implementation_version,
        available=False,
        blockers=tuple(dict.fromkeys(blockers)),
        request_id=request.request_id if request else None,
        parameter_identity=request.parameter_identity if request else "{}",
    )


def _required_prices(raw_values: Any, period: int) -> tuple[list[float] | None, str | None]:
    if not isinstance(raw_values, Sequence) or isinstance(raw_values, (str, bytes)):
        return None, "daily_closes_missing"
    if len(raw_values) <= period:
        return None, "daily_closes_insufficient"
    window = raw_values[-(period + 1) :]
    values: list[float] = []
    for value in window:
        if value is None:
            return None, "daily_closes_missing_window"
        if isinstance(value, bool):
            return None, "daily_closes_invalid"
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None, "daily_closes_invalid"
        if not isfinite(parsed) or parsed <= 0:
            return None, "daily_closes_invalid"
        values.append(parsed)
    return values, None


def _price_momentum(
    snapshot: InputSnapshot,
    params: MomentumParams,
    _dependencies: Mapping[str, FactorResult],
) -> FactorResult:
    values, blocker = _required_prices(snapshot.values.get("daily_closes"), params.lookback_days)
    if blocker is not None or values is None:
        return _unavailable(PRICE_MOMENTUM, blocker or "daily_closes_invalid")
    return FactorResult(
        key=PRICE_MOMENTUM.key,
        implementation_version=PRICE_MOMENTUM.implementation_version,
        available=True,
        value=values[-1] / values[0] - 1,
        units="return",
        evidence_refs=snapshot.evidence_refs,
    )


def _realized_volatility(
    snapshot: InputSnapshot,
    params: VolatilityParams,
    _dependencies: Mapping[str, FactorResult],
) -> FactorResult:
    values, blocker = _required_prices(snapshot.values.get("daily_closes"), params.period)
    if blocker is not None or values is None:
        return _unavailable(REALIZED_VOLATILITY, blocker or "daily_closes_invalid")
    returns = [log(current / previous) for previous, current in zip(values, values[1:], strict=False)]
    if len(returns) < 2:
        return _unavailable(REALIZED_VOLATILITY, "daily_closes_insufficient")
    return FactorResult(
        key=REALIZED_VOLATILITY.key,
        implementation_version=REALIZED_VOLATILITY.implementation_version,
        available=True,
        value=pstdev(returns) * sqrt(252),
        units="annualized_volatility",
        evidence_refs=snapshot.evidence_refs,
    )


def _relative_strength(
    snapshot: InputSnapshot,
    params: RelativeStrengthParams,
    _dependencies: Mapping[str, FactorResult],
) -> FactorResult:
    prices, blocker = _required_prices(snapshot.values.get("daily_closes"), params.period)
    benchmark, benchmark_blocker = _required_prices(snapshot.values.get("benchmark_closes"), params.period)
    if blocker or benchmark_blocker or prices is None or benchmark is None:
        return _unavailable(
            RELATIVE_STRENGTH,
            blocker or benchmark_blocker or "relative_strength_invalid",
        )
    return FactorResult(
        key=RELATIVE_STRENGTH.key,
        implementation_version=RELATIVE_STRENGTH.implementation_version,
        available=True,
        value=(prices[-1] / prices[0]) / (benchmark[-1] / benchmark[0]) - 1,
        units="relative_return",
        evidence_refs=snapshot.evidence_refs,
    )


PRICE_MOMENTUM = FactorDefinition(
    key="price.momentum",
    implementation_version="2",
    parameters_type=MomentumParams,
    requires=("daily_closes",),
    dependencies=(),
    evaluate=_price_momentum,
)

REALIZED_VOLATILITY = FactorDefinition(
    key="risk.realized_volatility",
    implementation_version="1",
    parameters_type=VolatilityParams,
    requires=("daily_closes",),
    dependencies=(),
    evaluate=_realized_volatility,
)

RELATIVE_STRENGTH = FactorDefinition(
    key="price.relative_strength",
    implementation_version="1",
    parameters_type=RelativeStrengthParams,
    requires=("daily_closes", "benchmark_closes"),
    dependencies=(),
    evaluate=_relative_strength,
)

FACTOR_CATALOG: Mapping[str, FactorDefinition] = MappingProxyType({
    definition.key: definition
    for definition in (PRICE_MOMENTUM, REALIZED_VOLATILITY, RELATIVE_STRENGTH)
})


def validate_factor_catalog(definitions: Mapping[str, FactorDefinition]) -> None:
    """Reject malformed or cyclic static factor registrations."""

    for key, definition in definitions.items():
        if key != definition.key or not definition.implementation_version:
            raise ValueError("factor definition identity is inconsistent")
        if len(set(definition.requires)) != len(definition.requires):
            raise ValueError(f"factor {key} repeats an input dependency")
        names: list[str] = []
        for dependency in definition.dependencies:
            name, request = _dependency_parts(dependency)
            names.append(name)
            if request.key not in definitions:
                raise ValueError(f"factor {key} has missing dependencies: {request.key}")
        if len(set(names)) != len(names):
            raise ValueError(f"factor {key} repeats a dependency name")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise ValueError("factor dependency cycle detected")
        if key in visited:
            return
        visiting.add(key)
        for dependency in definitions[key].dependencies:
            visit(_dependency_parts(dependency)[1].key)
        visiting.remove(key)
        visited.add(key)

    for key in definitions:
        visit(key)


def _requested_items(
    requested: Mapping[str | FactorRequest, Mapping[str, Any] | FactorParameters]
    | Sequence[str | FactorRequest]
    | FactorRequest,
) -> list[FactorRequest]:
    if isinstance(requested, FactorRequest):
        return [requested]
    if isinstance(requested, Mapping):
        result: list[FactorRequest] = []
        for key, parameters in requested.items():
            if isinstance(key, FactorRequest):
                if parameters is not None and parameters != {}:
                    raise ValueError("factor request parameters supplied twice")
                result.append(key)
            else:
                result.append(FactorRequest(key, parameters))
        return result
    return [item if isinstance(item, FactorRequest) else FactorRequest(item) for item in requested]


def _validated_parameters(
    definition: FactorDefinition,
    request: FactorRequest,
) -> FactorParameters:
    try:
        return definition.parameters_type.model_validate(dict(request.parameters))
    except ValidationError as exc:
        raise ValueError(f"factor parameters invalid for {request.key}") from exc


def evaluate_factors(
    snapshot: InputSnapshot,
    requested: Mapping[str | FactorRequest, Mapping[str, Any] | FactorParameters]
    | Sequence[str | FactorRequest]
    | FactorRequest,
    *,
    definitions: Mapping[str, FactorDefinition] = FACTOR_CATALOG,
    context: EvaluationContext | None = None,
) -> FactorResults:
    """Evaluate requested factors once for this snapshot and parameter identity."""

    validate_factor_catalog(definitions)
    active_context = context or EvaluationContext(snapshot)
    if active_context.snapshot.content_identity != snapshot.content_identity:
        raise ValueError("factor evaluation context snapshot does not match input snapshot")
    requests = _requested_items(requested)

    def evaluate(request: FactorRequest) -> FactorResult:
        definition = definitions.get(request.key)
        if definition is None:
            raise ValueError(f"unknown factor: {request.key}")
        if request.implementation_version not in (None, definition.implementation_version):
            raise ValueError(f"factor implementation version mismatch for {request.key}")
        params = _validated_parameters(definition, request)
        normalized_request = FactorRequest(
            definition.key,
            params,
            definition.implementation_version,
        )
        memo_key = (active_context.snapshot.content_identity, normalized_request.request_id)
        if memo_key in active_context.memo:
            return active_context.memo[memo_key]
        if len(active_context.memo) >= active_context.max_computations:
            raise ValueError("factor evaluation context exceeded its computation bound")

        dependencies: dict[str, FactorResult] = {}
        for dependency in definition.dependencies:
            name, dependency_request = _dependency_parts(dependency)
            dependencies[name] = evaluate(dependency_request)
        missing_inputs = tuple(
            f"{input_name}_missing" for input_name in definition.requires
            if input_name not in active_context.snapshot.values
        )
        unavailable_dependencies = tuple(
            f"dependency_{name}_unavailable"
            for name, result in dependencies.items()
            if not result.available
        )
        if missing_inputs or unavailable_dependencies:
            result = _unavailable(
                definition,
                *missing_inputs,
                *unavailable_dependencies,
                request=normalized_request,
            )
        else:
            try:
                result = definition.evaluate(active_context.snapshot, params, dependencies)
                if not isinstance(result, FactorResult):
                    result = FactorResult.model_validate(result)
            except (TypeError, ValueError, ValidationError) as exc:
                raise ValueError(f"factor evaluator rejected output for {request.key}") from exc
            if result.key != definition.key or result.implementation_version != definition.implementation_version:
                raise ValueError(f"factor evaluator identity mismatch for {request.key}")
            result = result.model_copy(update={
                "request_id": normalized_request.request_id,
                "parameter_identity": normalized_request.parameter_identity,
            })
        active_context.memo[memo_key] = result
        active_context.trace.append({"request_id": normalized_request.request_id, "key": definition.key})
        return result

    results = FactorResults()
    for request in requests:
        result = evaluate(request)
        definition = definitions[request.key]
        canonical_request = FactorRequest(
            definition.key,
            _validated_parameters(definition, request),
            definition.implementation_version,
        )
        results[canonical_request] = result
    return results


__all__ = [
    "Dependency", "EvaluationContext", "FACTOR_CATALOG", "EmptyFactorParameters", "FactorDefinition",
    "FactorDependency", "FactorParameters", "FactorRequest", "FactorResult", "FactorResults",
    "InputSnapshot", "MomentumParams", "PRICE_MOMENTUM", "REALIZED_VOLATILITY", "RELATIVE_STRENGTH",
    "RelativeStrengthParams", "VolatilityParams", "evaluate_factors", "validate_factor_catalog",
]
