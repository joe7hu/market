"""Small, deterministic Phase 4 portfolio and paper-telemetry contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from math import isfinite
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _json(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


def _hash(value: Any) -> str:
    return sha256(json.dumps(_json(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _finite(value: float | None, name: str) -> float | None:
    if value is not None and not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


class PortfolioCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    strategy_forecast_id: str | None = None
    action_id: str | None = None
    hypothesis_id: str | None = None
    strategy_registry_id: str | None = None
    expected_return: float | None = None
    uncertainty: float | None = None
    volatility: float | None = None
    risk_budget: float | None = None
    kelly_cap: float | None = None
    drawdown_cap: float | None = None
    capacity: float | None = None
    overlap_penalty: float | None = None
    execution_penalty: float | None = None
    current_weight: float = Field(default=0, ge=0, le=1)
    input_cutoff: datetime | None = None
    available_at: datetime | None = None
    evidence_status: str = "available"
    blockers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_numbers(self) -> "PortfolioCandidate":
        for name in (
            "expected_return", "uncertainty", "volatility", "risk_budget", "kelly_cap",
            "drawdown_cap", "capacity", "overlap_penalty", "execution_penalty",
        ):
            _finite(getattr(self, name), name)
        for name in ("uncertainty", "volatility", "risk_budget", "kelly_cap", "drawdown_cap", "capacity"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.input_cutoff and self.input_cutoff.tzinfo is None:
            raise ValueError("input_cutoff must be timezone-aware")
        if self.available_at and self.available_at.tzinfo is None:
            raise ValueError("available_at must be timezone-aware")
        return self


class PortfolioAllocationItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allocation_item_id: str
    candidate_id: str
    ticker: str
    strategy_forecast_id: str | None = None
    action_id: str | None = None
    hypothesis_id: str | None = None
    disposition: str
    target_weight: float = Field(ge=0, le=1)
    current_weight: float = Field(ge=0, le=1)
    marginal_book_utility: float
    funding_source: str | None = None
    blockers: tuple[str, ...] = ()
    trace: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_item(self) -> "PortfolioAllocationItem":
        if self.disposition not in {"selected", "ranked_out", "rejected"}:
            raise ValueError("invalid allocation disposition")
        if not isfinite(self.marginal_book_utility):
            raise ValueError("marginal_book_utility must be finite")
        if self.disposition == "selected" and self.ticker != "CASH" and (
            self.target_weight <= 0 or self.marginal_book_utility <= 0
        ):
            raise ValueError("funded allocation requires positive marginal book utility")
        return self


class PortfolioAllocationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allocation_id: str
    as_of: datetime
    input_cutoff: datetime
    cash_hurdle: float = Field(ge=0)
    status: str
    items: tuple[PortfolioAllocationItem, ...]
    forecast_ids: tuple[str, ...] = ()
    action_ids: tuple[str, ...] = ()
    strategy_registry_ids: tuple[str, ...] = ()


def _rejection(candidate: PortfolioCandidate, blockers: tuple[str, ...]) -> PortfolioAllocationItem:
    payload = {"candidate_id": candidate.candidate_id, "ticker": candidate.ticker, "disposition": "rejected", "blockers": blockers}
    return PortfolioAllocationItem(
        allocation_item_id=f"allocation-item:{_hash(payload)}", candidate_id=candidate.candidate_id,
        ticker=candidate.ticker, strategy_forecast_id=candidate.strategy_forecast_id,
        action_id=candidate.action_id, hypothesis_id=candidate.hypothesis_id, disposition="rejected",
        target_weight=0, current_weight=candidate.current_weight, marginal_book_utility=0,
        blockers=blockers, trace={"gate": "fail_closed"},
    )


def allocate_portfolio(
    candidates: list[PortfolioCandidate | Mapping[str, Any]],
    *,
    as_of: datetime,
    cash_hurdle: float = 0,
) -> PortfolioAllocationSnapshot:
    """Allocate only complete, PIT, positive-marginal-utility candidates."""

    if as_of.tzinfo is None or not isfinite(cash_hurdle) or cash_hurdle < 0:
        raise ValueError("allocation clock and cash hurdle must be valid")
    normalized = [item if isinstance(item, PortfolioCandidate) else PortfolioCandidate.model_validate(item) for item in candidates]
    if len({item.candidate_id for item in normalized}) != len(normalized):
        raise ValueError("allocation candidates must have unique IDs")
    ranked: list[PortfolioAllocationItem] = []
    eligible: list[tuple[PortfolioCandidate, float, dict[str, Any]]] = []
    for candidate in normalized:
        blockers = list(candidate.blockers)
        if candidate.evidence_status != "available":
            blockers.append(f"evidence_{candidate.evidence_status}")
        missing = [name for name in ("strategy_forecast_id", "action_id", "expected_return", "uncertainty", "volatility", "risk_budget", "kelly_cap", "drawdown_cap", "capacity") if getattr(candidate, name) is None]
        blockers.extend(f"{name}_missing" for name in missing)
        if candidate.input_cutoff is None or candidate.available_at is None:
            blockers.append("pit_lineage_missing")
        elif candidate.available_at.astimezone(UTC) > candidate.input_cutoff.astimezone(UTC):
            blockers.append("pit_lineage_conflict")
        if candidate.volatility is not None and candidate.volatility <= 0:
            blockers.append("volatility_invalid")
        if candidate.capacity is not None and candidate.capacity <= 0:
            blockers.append("capacity_unavailable")
        if blockers:
            ranked.append(_rejection(candidate, tuple(dict.fromkeys(blockers))))
            continue
        utility = candidate.expected_return - candidate.uncertainty - (candidate.overlap_penalty or 0) - (candidate.execution_penalty or 0)
        trace = {
            "input_cutoff": candidate.input_cutoff,
            "available_at": candidate.available_at,
            "strategy_forecast_id": candidate.strategy_forecast_id,
            "action_id": candidate.action_id,
            "current_weight": candidate.current_weight,
            "expected_return": candidate.expected_return,
            "uncertainty": candidate.uncertainty,
            "uncertainty_adjusted_return": candidate.expected_return - candidate.uncertainty,
            "overlap_penalty": candidate.overlap_penalty or 0,
            "execution_penalty": candidate.execution_penalty or 0,
            "marginal_book_utility": utility,
            "volatility": candidate.volatility,
            "risk_budget": candidate.risk_budget,
            "kelly_cap": candidate.kelly_cap,
            "drawdown_cap": candidate.drawdown_cap,
            "capacity": candidate.capacity,
            "current_risk_contribution": candidate.current_weight * candidate.volatility,
        }
        eligible.append((candidate, utility, trace))
    eligible.sort(key=lambda item: (-item[1], item[0].candidate_id))
    remaining = 1.0
    for candidate, utility, trace in eligible:
        if utility <= cash_hurdle:
            payload = {"candidate_id": candidate.candidate_id, "ticker": candidate.ticker, "disposition": "ranked_out", "trace": trace}
            ranked.append(PortfolioAllocationItem(
                allocation_item_id=f"allocation-item:{_hash(payload)}", candidate_id=candidate.candidate_id,
                ticker=candidate.ticker, strategy_forecast_id=candidate.strategy_forecast_id,
                action_id=candidate.action_id, hypothesis_id=candidate.hypothesis_id, disposition="ranked_out",
                target_weight=0, current_weight=candidate.current_weight, marginal_book_utility=utility,
                blockers=("below_cash_hurdle",), trace=trace,
            ))
            continue
        cap = min(candidate.risk_budget, candidate.kelly_cap, candidate.drawdown_cap, candidate.capacity, remaining)
        target = max(0.0, cap / candidate.volatility)
        if target <= 0:
            ranked.append(_rejection(candidate, ("capacity_or_risk_cap_zero",)))
            continue
        target = min(target, remaining)
        trace["uncertainty_scale"] = (candidate.expected_return - candidate.uncertainty) / candidate.expected_return if candidate.expected_return else 0
        trace["uncertainty_scaled_weight"] = target
        trace["constraint_weight"] = cap
        trace["proposed_risk_contribution"] = target * candidate.volatility
        payload = {"candidate_id": candidate.candidate_id, "ticker": candidate.ticker, "disposition": "selected", "target_weight": target, "trace": trace}
        ranked.append(PortfolioAllocationItem(
            allocation_item_id=f"allocation-item:{_hash(payload)}", candidate_id=candidate.candidate_id,
            ticker=candidate.ticker, strategy_forecast_id=candidate.strategy_forecast_id,
            action_id=candidate.action_id, hypothesis_id=candidate.hypothesis_id, disposition="selected",
            target_weight=target, current_weight=candidate.current_weight, marginal_book_utility=utility,
            funding_source="CASH" if candidate.current_weight == 0 else f"TRIM:{candidate.ticker}", trace=trace,
        ))
        remaining -= target
    cash_payload = {"candidate_id": "CASH", "ticker": "CASH", "disposition": "selected", "target_weight": remaining}
    ranked.append(PortfolioAllocationItem(
        allocation_item_id=f"allocation-item:{_hash(cash_payload)}", candidate_id="CASH", ticker="CASH",
        disposition="selected", target_weight=remaining, current_weight=0, marginal_book_utility=0,
        funding_source="CASH", trace={"cash_hurdle": cash_hurdle},
    ))
    ranked.sort(key=lambda item: (0 if item.ticker == "CASH" else 1, item.disposition, item.candidate_id))
    selected = [item for item in ranked if item.disposition == "selected" and item.ticker != "CASH"]
    forecast_ids = tuple(sorted(item.strategy_forecast_id for item in selected if item.strategy_forecast_id))
    action_ids = tuple(sorted(item.action_id for item in selected if item.action_id))
    registry_ids = tuple(sorted(candidate.strategy_registry_id for candidate, _, _ in eligible if candidate.strategy_registry_id))
    allocation_payload = {"as_of": as_of, "cash_hurdle": cash_hurdle, "items": ranked, "forecast_ids": forecast_ids, "action_ids": action_ids, "strategy_registry_ids": registry_ids}
    return PortfolioAllocationSnapshot(
        allocation_id=f"allocation:{_hash(allocation_payload)}", as_of=as_of, input_cutoff=as_of,
        cash_hurdle=cash_hurdle, status="available" if selected else "cash_only", items=tuple(ranked),
        forecast_ids=forecast_ids, action_ids=action_ids, strategy_registry_ids=registry_ids,
    )


class PortfolioScenarioArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_artifact_id: str
    allocation_id: str
    model_version: str
    probability_semantics: str
    scenarios: tuple[dict[str, Any], ...]
    tail_dependence: dict[str, Any]
    simultaneous_unwind: dict[str, Any]
    input_cutoff: datetime


class PortfolioDriftDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    allocation_id: str
    allocation_item_id: str
    drift_score: float
    rollback_threshold: float
    proposed_weight: float = Field(ge=0, le=1)
    action: str


def apply_decay_guard(
    allocation: PortfolioAllocationSnapshot,
    drift_scores: Mapping[str, float],
    *,
    rollback_threshold: float,
) -> tuple[PortfolioDriftDecision, ...]:
    """Reduce exposure on observed decay, then roll back at the hard threshold."""

    if not isfinite(rollback_threshold) or rollback_threshold <= 0:
        raise ValueError("rollback threshold must be positive")
    decisions: list[PortfolioDriftDecision] = []
    for item in allocation.items:
        if item.ticker == "CASH" or item.disposition != "selected":
            continue
        score = drift_scores.get(item.allocation_item_id)
        if score is None or not isfinite(score) or score < 0:
            action, weight, score_value = "unavailable", 0.0, 0.0
        elif score >= rollback_threshold:
            action, weight, score_value = "rollback", 0.0, score
        elif score >= rollback_threshold / 2:
            action, weight, score_value = "reduce", item.target_weight / 2, score
        else:
            action, weight, score_value = "hold", item.target_weight, score
        payload = {"allocation_id": allocation.allocation_id, "allocation_item_id": item.allocation_item_id, "drift_score": score_value, "rollback_threshold": rollback_threshold, "proposed_weight": weight, "action": action}
        decisions.append(PortfolioDriftDecision(
            decision_id=f"drift:{_hash(payload)}", allocation_id=allocation.allocation_id,
            allocation_item_id=item.allocation_item_id, drift_score=score_value,
            rollback_threshold=rollback_threshold, proposed_weight=weight, action=action,
        ))
    return tuple(decisions)


def build_scenario_artifact(
    allocation: PortfolioAllocationSnapshot,
    scenarios: list[Mapping[str, Any]],
    *,
    model_version: str,
    probability_semantics: str,
    tail_dependence: Mapping[str, Any],
    simultaneous_unwind: Mapping[str, Any],
) -> PortfolioScenarioArtifact:
    if not scenarios or not model_version.strip() or not probability_semantics.strip() or not tail_dependence or not simultaneous_unwind:
        raise ValueError("scenario artifact requires paths and semantics")
    probabilities = [item.get("probability") for item in scenarios]
    if any(value is None or not isfinite(float(value)) or not 0 <= float(value) <= 1 for value in probabilities):
        raise ValueError("scenario paths require bounded probabilities")
    probability_total = sum(float(value) for value in probabilities)
    if abs(probability_total - 1) > 1e-9 or len(scenarios) > 64:
        raise ValueError("scenario probabilities must sum to one within a bounded path set")
    for item in scenarios:
        if not isinstance(item.get("returns"), Mapping):
            raise ValueError("scenario path requires explicit returns")
    payload = {"allocation_id": allocation.allocation_id, "model_version": model_version, "probability_semantics": probability_semantics, "scenarios": scenarios, "tail_dependence": tail_dependence, "simultaneous_unwind": simultaneous_unwind}
    return PortfolioScenarioArtifact(
        scenario_artifact_id=f"scenario:{_hash(payload)}", allocation_id=allocation.allocation_id,
        model_version=model_version, probability_semantics=probability_semantics,
        scenarios=tuple(dict(item) for item in scenarios),
        tail_dependence=dict(tail_dependence), simultaneous_unwind=dict(simultaneous_unwind), input_cutoff=allocation.input_cutoff,
    )


class PaperExecutionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    paper_execution_observation_id: str
    allocation_item_id: str | None = None
    paper_order_id: str | None = None
    execution_mode: str = "paper"
    paper_only: bool = True
    status: str
    requested_quantity: float = Field(ge=0)
    filled_quantity: float = Field(ge=0)
    requested_price: float | None = None
    fill_price: float | None = None
    spread_bps: float | None = None
    latency_ms: float | None = None
    impact_bps: float | None = None
    observed_at: datetime
    available_at: datetime

    @model_validator(mode="after")
    def enforce_paper(self) -> "PaperExecutionObservation":
        if self.execution_mode != "paper" or not self.paper_only or self.status not in {"planned", "submitted", "partial", "filled", "exited", "cancelled", "unavailable"}:
            raise ValueError("execution telemetry is paper-only")
        for name in ("requested_quantity", "filled_quantity", "requested_price", "fill_price", "spread_bps", "latency_ms", "impact_bps"):
            _finite(getattr(self, name), name)
        if self.filled_quantity > self.requested_quantity or (self.filled_quantity and self.fill_price is None):
            raise ValueError("filled execution requires bounded quantity and fill price")
        if self.observed_at.tzinfo is None or self.available_at.tzinfo is None:
            raise ValueError("execution observation clocks must be timezone-aware")
        return self


class ExecutionModelSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_model_snapshot_id: str
    allocation_id: str | None = None
    model_version: str
    calibration_status: str
    sample_count: int = Field(ge=0)
    fill_probability: float | None = None
    spread_bps: float | None = None
    latency_ms: float | None = None
    impact_bps: float | None = None
    input_cutoff: datetime

    @model_validator(mode="after")
    def validate_calibration(self) -> "ExecutionModelSnapshot":
        if self.calibration_status not in {"calibrated", "calibration_pending", "unavailable"}:
            raise ValueError("invalid execution calibration status")
        if self.calibration_status == "calibrated" and self.sample_count == 0:
            raise ValueError("calibrated execution telemetry requires genuine fills")
        for name in ("fill_probability", "spread_bps", "latency_ms", "impact_bps"):
            _finite(getattr(self, name), name)
        return self


def build_execution_model_snapshot(
    allocation_id: str | None,
    input_cutoff: datetime,
    observations: list[PaperExecutionObservation],
) -> ExecutionModelSnapshot:
    genuine = [item for item in observations if item.filled_quantity > 0 and item.fill_price is not None]
    payload = {
        "allocation_id": allocation_id, "input_cutoff": input_cutoff, "sample_count": len(genuine),
        "fill_probability": (sum(item.filled_quantity > 0 for item in observations) / len(observations)) if observations else None,
        "spread_bps": (sum(item.spread_bps for item in genuine if item.spread_bps is not None) / len([item for item in genuine if item.spread_bps is not None])) if any(item.spread_bps is not None for item in genuine) else None,
    }
    return ExecutionModelSnapshot(
        execution_model_snapshot_id=f"execution:{_hash(payload)}", allocation_id=allocation_id,
        model_version="paper-telemetry.v1", calibration_status="calibrated" if genuine else "calibration_pending",
        sample_count=len(genuine), fill_probability=payload["fill_probability"], spread_bps=payload["spread_bps"],
        latency_ms=None, impact_bps=None, input_cutoff=input_cutoff,
    )


class BookAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    book_attribution_id: str
    allocation_id: str
    allocation_item_id: str
    strategy_forecast_id: str | None = None
    hypothesis_id: str | None = None
    paper_execution_observation_id: str | None = None
    pnl_status: str
    realized_pnl: float | None = None
    attribution: dict[str, Any]
    input_cutoff: datetime

    @model_validator(mode="after")
    def validate_status(self) -> "BookAttribution":
        if self.pnl_status not in {"pending_fill", "realized", "unavailable"}:
            raise ValueError("invalid attribution status")
        if self.pnl_status == "realized" and self.realized_pnl is None:
            raise ValueError("realized attribution requires realized P&L")
        _finite(self.realized_pnl, "realized_pnl")
        return self


def attribute_paper_pnl(
    allocation: PortfolioAllocationSnapshot,
    item: PortfolioAllocationItem,
    *,
    observation: PaperExecutionObservation | None = None,
    realized_pnl: float | None = None,
) -> BookAttribution:
    if item.allocation_item_id not in {candidate.allocation_item_id for candidate in allocation.items}:
        raise ValueError("allocation item does not belong to allocation")
    status = "realized" if observation and observation.filled_quantity > 0 and realized_pnl is not None else "pending_fill"
    payload = {"allocation_id": allocation.allocation_id, "allocation_item_id": item.allocation_item_id, "observation": observation, "realized_pnl": realized_pnl, "status": status}
    return BookAttribution(
        book_attribution_id=f"attribution:{_hash(payload)}", allocation_id=allocation.allocation_id,
        allocation_item_id=item.allocation_item_id, strategy_forecast_id=item.strategy_forecast_id,
        hypothesis_id=item.hypothesis_id, paper_execution_observation_id=observation.paper_execution_observation_id if observation else None,
        pnl_status=status, realized_pnl=realized_pnl if status == "realized" else None,
        attribution={"source": "paper_execution_observation", "hypothesis_id": item.hypothesis_id}, input_cutoff=allocation.input_cutoff,
    )
