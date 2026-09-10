"""Small typed signal catalog built on the factor evaluation context.

Signals are interpretations of measurements. They are not calibrated return
forecasts and do not grant actionability.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investment_panel.domain.factors import (
    EvaluationContext,
    FactorRequest,
    FactorResults,
    InputSnapshot,
    MomentumParams,
    VolatilityParams,
    evaluate_factors,
)


SignalDirection = Literal["long", "short", "flat"]


class SignalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    key: str = Field(min_length=1)
    implementation_version: str = Field(min_length=1)
    available: bool
    direction: SignalDirection | None = None
    score: float | None = None
    horizon: str = "daily"
    evidence_refs: tuple[str, ...] = ()
    factor_request_ids: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def reject_boolean_score(cls, values: Any) -> Any:
        if isinstance(values, Mapping) and isinstance(values.get("score"), bool):
            raise ValueError("signal score cannot be boolean")
        return values

    @model_validator(mode="after")
    def validate_availability(self) -> "SignalResult":
        if self.available and self.score is None:
            raise ValueError("available signal requires a score")
        if not self.available and self.score is not None:
            raise ValueError("unavailable signal cannot contain a score")
        if self.available and self.direction is None:
            raise ValueError("available signal requires a direction")
        return self


@dataclass(frozen=True, slots=True)
class SignalDefinition:
    key: str
    implementation_version: str
    factor_requests: tuple[FactorRequest, ...]
    evaluate: Callable[[InputSnapshot, FactorResults], SignalResult]


def _direction(value: float) -> SignalDirection:
    return "long" if value > 0 else "short" if value < 0 else "flat"


def _momentum_direction(snapshot: InputSnapshot, factors: FactorResults) -> SignalResult:
    request = FactorRequest("price.momentum", MomentumParams())
    factor = factors.by_request(request)
    if not factor.available or factor.value is None:
        return SignalResult(
            key=MOMENTUM_DIRECTION.key,
            implementation_version=MOMENTUM_DIRECTION.implementation_version,
            available=False,
            evidence_refs=snapshot.evidence_refs,
            factor_request_ids=(factor.request_id or request.request_id,),
            blockers=factor.blockers,
        )
    return SignalResult(
        key=MOMENTUM_DIRECTION.key,
        implementation_version=MOMENTUM_DIRECTION.implementation_version,
        available=True,
        direction=_direction(factor.value),
        score=factor.value,
        evidence_refs=snapshot.evidence_refs,
        factor_request_ids=(factor.request_id or request.request_id,),
        evidence={"measurement": "price.momentum", "interpretation": "direction_only"},
    )


def _momentum_volatility(snapshot: InputSnapshot, factors: FactorResults) -> SignalResult:
    momentum_request = FactorRequest("price.momentum", MomentumParams())
    volatility_request = FactorRequest("risk.realized_volatility", VolatilityParams())
    momentum = factors.by_request(momentum_request)
    volatility = factors.by_request(volatility_request)
    blockers = tuple(dict.fromkeys((*momentum.blockers, *volatility.blockers)))
    if not momentum.available or momentum.value is None or not volatility.available or volatility.value is None:
        return SignalResult(
            key=MOMENTUM_VOLATILITY.key,
            implementation_version=MOMENTUM_VOLATILITY.implementation_version,
            available=False,
            evidence_refs=snapshot.evidence_refs,
            factor_request_ids=(momentum.request_id or momentum_request.request_id, volatility.request_id or volatility_request.request_id),
            blockers=blockers or ("momentum_or_volatility_unavailable",),
        )
    return SignalResult(
        key=MOMENTUM_VOLATILITY.key,
        implementation_version=MOMENTUM_VOLATILITY.implementation_version,
        available=True,
        direction=_direction(momentum.value),
        score=momentum.value,
        evidence_refs=snapshot.evidence_refs,
        factor_request_ids=(momentum.request_id or momentum_request.request_id, volatility.request_id or volatility_request.request_id),
        evidence={"realized_volatility": volatility.value, "interpretation": "momentum_with_risk_context"},
    )


MOMENTUM_DIRECTION = SignalDefinition(
    key="signal.momentum_direction",
    implementation_version="1",
    factor_requests=(FactorRequest("price.momentum", MomentumParams()),),
    evaluate=_momentum_direction,
)

MOMENTUM_VOLATILITY = SignalDefinition(
    key="signal.momentum_volatility",
    implementation_version="1",
    factor_requests=(
        FactorRequest("price.momentum", MomentumParams()),
        FactorRequest("risk.realized_volatility", VolatilityParams()),
    ),
    evaluate=_momentum_volatility,
)

SIGNAL_CATALOG: Mapping[str, SignalDefinition] = MappingProxyType({
    definition.key: definition for definition in (MOMENTUM_DIRECTION, MOMENTUM_VOLATILITY)
})


def evaluate_signals(
    snapshot: InputSnapshot,
    requested: Sequence[str] | str,
    *,
    context: EvaluationContext | None = None,
) -> dict[str, SignalResult]:
    names = (requested,) if isinstance(requested, str) else tuple(requested)
    definitions = []
    factor_requests: list[FactorRequest] = []
    for name in names:
        definition = SIGNAL_CATALOG.get(name)
        if definition is None:
            raise ValueError(f"unknown signal: {name}")
        definitions.append(definition)
        factor_requests.extend(definition.factor_requests)
    factor_results = evaluate_factors(snapshot, factor_requests, context=context)
    results: dict[str, SignalResult] = {}
    for definition in definitions:
        result = definition.evaluate(snapshot, factor_results)
        if result.key != definition.key or result.implementation_version != definition.implementation_version:
            raise ValueError(f"signal evaluator identity mismatch for {definition.key}")
        results[definition.key] = result
    return results


__all__ = [
    "MOMENTUM_DIRECTION", "MOMENTUM_VOLATILITY", "SIGNAL_CATALOG", "SignalDefinition", "SignalResult",
    "evaluate_signals",
]
