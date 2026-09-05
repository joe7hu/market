"""Small, deterministic Phase 4 portfolio and paper-telemetry contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from hashlib import sha256
import json
from math import isfinite
from typing import Any, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy.optimize import minimize

def _json(value: Any) -> Any:
    if isinstance(value, datetime):
        # Match analysis.phase4_canonical_json: timestamps are stored as UTC
        # timestamp-without-time-zone values in the canonical JSON payload.
        return value.astimezone(UTC).isoformat().removesuffix("+00:00")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json(value.value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, float) and isfinite(value) and value.is_integer():
        # PostgreSQL jsonb emits integral doubles as JSON integers.
        return int(value)
    if isinstance(value, BaseModel):
        return _json(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {
            str(key): _json(value[key])
            for key in sorted(value, key=lambda key: (len(str(key)), str(key)))
        }
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


def _hash(value: Any) -> str:
    return sha256(json.dumps(_json(value), ensure_ascii=False, separators=(", ", ": ")).encode()).hexdigest()


def _finite(value: float | None, name: str) -> float | None:
    if value is not None and not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _allocation_payload(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        items = value["items"]
        items = tuple(sorted(items, key=lambda item: str(item.get("allocation_item_id") if isinstance(item, Mapping) else item.allocation_item_id)))
        return {
            "as_of": value["as_of"],
            "cash_hurdle": value["cash_hurdle"],
            "items": items,
            "forecast_ids": value["forecast_ids"],
            "action_ids": value["action_ids"],
            "strategy_registry_ids": value["strategy_registry_ids"],
            "metadata": value.get("metadata", {}),
        }
    return {
        "as_of": value.as_of,
        "cash_hurdle": value.cash_hurdle,
        "items": tuple(sorted(value.items, key=lambda item: item.allocation_item_id)),
        "forecast_ids": value.forecast_ids,
        "action_ids": value.action_ids,
        "strategy_registry_ids": value.strategy_registry_ids,
        "metadata": value.metadata,
    }


def allocation_id_for_snapshot(value: Mapping[str, Any] | Any) -> str:
    """Return the content address for one complete allocation snapshot."""
    return f"allocation:{_hash(_allocation_payload(value))}"


def _execution_payload(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    return {
        "allocation_id": value.allocation_id,
        "input_cutoff": value.input_cutoff,
        "model_version": value.model_version,
        "calibration_status": value.calibration_status,
        "sample_count": value.sample_count,
        "fill_probability": value.fill_probability,
        "spread_bps": value.spread_bps,
        "latency_ms": value.latency_ms,
        "impact_bps": value.impact_bps,
        "metadata": value.metadata,
    }


def execution_model_id_for_snapshot(value: Mapping[str, Any] | Any) -> str:
    return f"execution:{_hash(_execution_payload(value))}"


def _attribution_payload(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {
            "allocation_id": value["allocation_id"],
            "allocation_item_id": value["allocation_item_id"],
            "strategy_forecast_id": value.get("strategy_forecast_id"),
            "hypothesis_id": value.get("hypothesis_id"),
            "action_id": value.get("action_id"), "rank_id": value.get("rank_id"),
            "expression": value.get("expression"),
            "experiment_id": value.get("experiment_id"), "trial_id": value.get("trial_id"),
            "result_id": value.get("result_id"),
            "paper_execution_observation_id": value.get("paper_execution_observation_id"),
            "pnl_status": value["pnl_status"],
            "realized_pnl": value.get("realized_pnl"),
            "attribution": value["attribution"],
            "input_cutoff": value["input_cutoff"],
        }
    return {
        "allocation_id": value.allocation_id,
        "allocation_item_id": value.allocation_item_id,
        "strategy_forecast_id": value.strategy_forecast_id,
        "hypothesis_id": value.hypothesis_id,
        "action_id": value.action_id, "rank_id": value.rank_id,
        "expression": value.expression,
        "experiment_id": value.experiment_id,
        "trial_id": value.trial_id,
        "result_id": value.result_id,
        "paper_execution_observation_id": value.paper_execution_observation_id,
        "pnl_status": value.pnl_status,
        "realized_pnl": value.realized_pnl,
        "attribution": value.attribution,
        "input_cutoff": value.input_cutoff,
    }


def attribution_id_for_record(value: Mapping[str, Any] | Any) -> str:
    return f"attribution:{_hash(_attribution_payload(value))}"


def canonical_content_hash(value: Mapping[str, Any] | Any) -> str:
    """Hash the exact canonical payload used by the PostgreSQL row trigger."""

    def field(name: str) -> Any:
        return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)

    if field("candidate_id") is not None and field("allocation_item_id") is not None:
        payload = {
            "allocation_item_id": field("allocation_item_id"), "allocation_id": field("allocation_id"),
            "candidate_id": field("candidate_id"), "ticker": field("ticker"),
            "strategy_forecast_id": field("strategy_forecast_id"), "action_id": field("action_id"),
            "rank_id": field("rank_id"), "hypothesis_id": field("hypothesis_id"),
            "disposition": field("disposition"), "target_weight": field("target_weight"),
            "current_weight": field("current_weight"), "marginal_book_utility": field("marginal_book_utility"),
            "trace": field("trace"), "blockers": field("blockers"),
            "funding_source": field("funding_source"), "funding_amount": field("funding_amount"),
            "funding_sources": field("funding_sources") or {},
        }
    elif field("scenario_artifact_id") is not None:
        payload = {
            "scenario_artifact_id": field("scenario_artifact_id"), "allocation_id": field("allocation_id"),
            "model_version": field("model_version"), "probability_semantics": field("probability_semantics"),
            "scenarios": field("scenarios"), "tail_dependence": field("tail_dependence"),
            "simultaneous_unwind": field("simultaneous_unwind"), "input_cutoff": field("input_cutoff"),
        }
    elif field("execution_model_snapshot_id") is not None:
        payload = {
            "execution_model_snapshot_id": field("execution_model_snapshot_id"), "allocation_id": field("allocation_id"),
            "model_version": field("model_version"), "calibration_status": field("calibration_status"),
            "sample_count": field("sample_count"), "fill_probability": field("fill_probability"),
            "spread_bps": field("spread_bps"), "latency_ms": field("latency_ms"),
            "impact_bps": field("impact_bps"), "input_cutoff": field("input_cutoff"),
            "metadata": field("metadata") or {},
        }
    elif field("book_attribution_id") is not None:
        payload = {
            "book_attribution_id": field("book_attribution_id"), "allocation_id": field("allocation_id"),
            "allocation_item_id": field("allocation_item_id"), "strategy_forecast_id": field("strategy_forecast_id"),
            "hypothesis_id": field("hypothesis_id"), "action_id": field("action_id"),
            "rank_id": field("rank_id"), "expression": field("expression"),
            "experiment_id": field("experiment_id"), "trial_id": field("trial_id"),
            "result_id": field("result_id"), "paper_execution_observation_id": field("paper_execution_observation_id"),
            "pnl_status": field("pnl_status"), "realized_pnl": field("realized_pnl"),
            "attribution": field("attribution"), "input_cutoff": field("input_cutoff"),
        }
    elif field("decision_id") is not None:
        payload = {
            "decision_id": field("decision_id"), "allocation_id": field("allocation_id"),
            "allocation_item_id": field("allocation_item_id"), "drift_score": field("drift_score"),
            "rollback_threshold": field("rollback_threshold"), "proposed_weight": field("proposed_weight"),
            "action": field("action"), "input_cutoff": field("input_cutoff"), "metadata": field("metadata") or {},
        }
    elif field("allocation_id") is None:
        payload = value
    else:
        payload = {
            "allocation_id": field("allocation_id"), "as_of": field("as_of"),
            "input_cutoff": field("input_cutoff"), "status": field("status"),
            "cash_hurdle": field("cash_hurdle"), "forecast_ids": field("forecast_ids") or [],
            "action_ids": field("action_ids") or [], "strategy_registry_ids": field("strategy_registry_ids") or [],
            "metadata": field("metadata") or {},
        }
    return _hash(payload)


class PortfolioCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    strategy_forecast_id: str | None = None
    action_id: str | None = None
    rank_id: str | None = None
    hypothesis_id: str | None = None
    strategy_registry_id: str | None = None
    portfolio_impact_id: str | None = None
    source_decision_id: str | None = None
    source_input_hash: str | None = None
    source_decision_input_hash: str | None = None
    experiment_id: str | None = None
    trial_id: str | None = None
    result_id: str | None = None
    expected_return: float | None = None
    uncertainty: float | None = None
    volatility: float | None = None
    risk_budget: float | None = None
    kelly_cap: float | None = None
    drawdown_cap: float | None = None
    capacity: float | None = None
    days_to_exit: float | None = None
    liquidity: dict[str, Any] | None = None
    overlap_penalty: float | None = None
    execution_penalty: float | None = None
    covariance: dict[str, float] | None = None
    factor_exposure: dict[str, float] | None = None
    sector: str | None = None
    asset_class: str | None = None
    greeks: dict[str, float] | None = None
    venue: str | None = None
    expression: dict[str, Any] | None = None
    invalidation: dict[str, Any] | None = None
    why_trade: str | None = None
    why_now: tuple[str, ...] = ()
    rank_position: int | None = None
    rank_utility: float | None = None
    missing_data: tuple[str, ...] = ()
    current_weight: float = Field(default=0, ge=0, le=1)
    cash_available: float | None = None
    cash_source_id: str | None = None
    trim_position_id: str | None = None
    trim_available: float | None = None
    input_cutoff: datetime | None = None
    available_at: datetime | None = None
    evidence_status: str = "available"
    blockers: tuple[str, ...] = ()
    risk_evidence: "PortfolioImpactRiskEvidence | None" = None

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
        if self.cash_available is not None and (not isfinite(self.cash_available) or self.cash_available < 0):
            raise ValueError("cash_available must be finite and non-negative")
        if self.trim_available is not None and (not isfinite(self.trim_available) or self.trim_available < 0):
            raise ValueError("trim_available must be finite and non-negative")
        if self.input_cutoff and self.input_cutoff.tzinfo is None:
            raise ValueError("input_cutoff must be timezone-aware")
        if self.available_at and self.available_at.tzinfo is None:
            raise ValueError("available_at must be timezone-aware")
        if self.covariance is not None:
            if not self.covariance or any(not isfinite(float(value)) for value in self.covariance.values()):
                raise ValueError("covariance must contain finite values")
            variance = self.covariance.get(self.ticker)
            if variance is not None and float(variance) < 0:
                raise ValueError("covariance variance must be non-negative")
        for name in ("factor_exposure", "greeks"):
            values = getattr(self, name)
            if values is not None and any(not isfinite(float(value)) for value in values.values()):
                raise ValueError(f"{name} must contain finite values")
        if self.rank_position is not None and self.rank_position < 1:
            raise ValueError("rank position must be positive")
        _finite(self.rank_utility, "rank_utility")
        return self


class PortfolioImpactRiskEvidence(BaseModel):
    """Typed, database-bound risk inputs used by the production allocator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    impact_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    source_decision_id: str = Field(min_length=1)
    source_input_hash: str = Field(min_length=64, max_length=64)
    source_decision_input_hash: str = Field(min_length=64, max_length=64)
    input_cutoff: datetime
    expected_return: float
    uncertainty: float
    volatility: float
    risk_budget: float
    kelly_cap: float
    drawdown_cap: float
    capacity: float
    overlap_penalty: float = 0
    execution_penalty: float = 0
    covariance: dict[str, float]

    @model_validator(mode="after")
    def validate_evidence(self) -> "PortfolioImpactRiskEvidence":
        for name in ("source_input_hash", "source_decision_input_hash"):
            value = getattr(self, name)
            if value == "0" * 64 or any(char not in "0123456789abcdef" for char in value.lower()):
                raise ValueError("portfolio impact risk evidence requires canonical source digests")
        if self.input_cutoff.tzinfo is None:
            raise ValueError("portfolio impact risk evidence requires a timezone-aware cutoff")
        for name in ("expected_return", "uncertainty", "volatility", "risk_budget", "kelly_cap", "drawdown_cap", "capacity", "overlap_penalty", "execution_penalty"):
            if not isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if not self.covariance or any(not isfinite(float(value)) for value in self.covariance.values()):
            raise ValueError("portfolio impact risk evidence requires covariance")
        return self


PortfolioCandidate.model_rebuild()


class PortfolioBookEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str | None = None
    net_liquidation: float | None = None
    cash_available: float | None = None
    cash_source_id: str | None = None
    positions: dict[str, str] = Field(default_factory=dict)
    position_weights: dict[str, float] = Field(default_factory=dict)
    position_exposures: dict[str, dict[str, Any]] = Field(default_factory=dict)
    input_cutoff: datetime

    @model_validator(mode="after")
    def validate_book(self) -> "PortfolioBookEvidence":
        for name in ("net_liquidation", "cash_available"):
            _finite(getattr(self, name), name)
        if self.net_liquidation is not None and self.net_liquidation <= 0:
            raise ValueError("portfolio book requires positive net liquidation")
        if self.cash_available is not None and self.cash_available < 0:
            raise ValueError("portfolio book cash cannot be negative")
        if self.cash_available is not None and not self.cash_source_id:
            raise ValueError("portfolio book cash requires a persisted source identity")
        if self.input_cutoff.tzinfo is None:
            raise ValueError("portfolio book requires a timezone-aware cutoff")
        if set(self.position_weights) - set(self.positions):
            raise ValueError("portfolio position weights require persisted position identities")
        if set(self.position_exposures) - set(self.positions):
            raise ValueError("portfolio position exposures require persisted position identities")
        if any(not isfinite(value) or value < 0 or value > 1 for value in self.position_weights.values()):
            raise ValueError("portfolio position weights must be finite portfolio weights")
        return self


class PortfolioConstraintEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cash_hurdle: float | None = None
    constraint_hash: str
    volatility_source: str | None = None
    capacity_source: str | None = None
    covariance_source: str | None = None
    risk_policy_hash: str | None = None
    risk_policy_version: str | None = None
    position_limit: float | None = None
    aggregate_loss_limit: float | None = None
    sector_limits: dict[str, float] = Field(default_factory=dict)
    asset_class_limits: dict[str, float] = Field(default_factory=dict)
    factor_limits: dict[str, float] = Field(default_factory=dict)
    greek_limits: dict[str, float] = Field(default_factory=dict)
    min_liquidity: float | None = None
    allowed_venues: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_constraint_evidence(self) -> "PortfolioConstraintEvidence":
        if not self.constraint_hash.strip():
            raise ValueError("portfolio constraints require a content digest")
        if self.cash_hurdle is not None and (not isfinite(self.cash_hurdle) or self.cash_hurdle <= 0):
            raise ValueError("portfolio cash hurdle must be persisted and positive")
        for name in ("position_limit", "aggregate_loss_limit"):
            value = getattr(self, name)
            if value is not None and (not isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be positive")
        for limits in (self.sector_limits, self.asset_class_limits, self.factor_limits, self.greek_limits):
            if any(not isfinite(float(value)) or value <= 0 for value in limits.values()):
                raise ValueError("portfolio constraint limits must be positive finite values")
        if self.min_liquidity is not None and (not isfinite(self.min_liquidity) or self.min_liquidity < 0):
            raise ValueError("portfolio minimum liquidity must be non-negative")
        if self.risk_policy_hash is not None and (
            len(self.risk_policy_hash) != 64
            or self.risk_policy_hash == "0" * 64
            or any(char not in "0123456789abcdef" for char in self.risk_policy_hash.lower())
        ):
            raise ValueError("portfolio constraints require the persisted risk-policy digest")
        return self


class PortfolioExecutionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str | None = None
    calibration_status: str
    sample_count: int = Field(ge=0)
    input_cutoff: datetime


class PortfolioScenarioEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str | None = None
    observations: tuple[dict[str, Any], ...]
    input_cutoff: datetime

    @model_validator(mode="after")
    def validate_scenario_evidence(self) -> "PortfolioScenarioEvidence":
        if self.artifact_id is not None and not self.artifact_id.strip():
            raise ValueError("scenario artifact identity cannot be empty")
        return self


class AuthoritativePortfolioBundle(BaseModel):
    """Repository-issued PostgreSQL inputs consumed by the allocator as one unit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_cutoff: datetime
    candidates: tuple[PortfolioCandidate, ...]
    book: PortfolioBookEvidence
    constraints: PortfolioConstraintEvidence
    execution: PortfolioExecutionEvidence
    scenario: PortfolioScenarioEvidence
    drift_scores: dict[str, float] = Field(default_factory=dict)
    complete: bool = True
    authority_snapshot_id: str = Field(min_length=1)
    authority_content_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_bundle(self) -> "AuthoritativePortfolioBundle":
        if self.authority_content_hash == "0" * 64 or any(char not in "0123456789abcdef" for char in self.authority_content_hash.lower()):
            raise ValueError("authoritative bundle requires a canonical source digest")
        if self.input_cutoff.tzinfo is None:
            raise ValueError("authoritative portfolio cutoff must be timezone-aware")
        for evidence in (self.book, self.scenario):
            if evidence.input_cutoff.astimezone(UTC) != self.input_cutoff.astimezone(UTC):
                raise ValueError("authoritative book and scenario clocks must match the bundle cutoff")
        if self.execution.input_cutoff.astimezone(UTC) > self.input_cutoff.astimezone(UTC):
            raise ValueError("execution evidence cannot be newer than the bundle cutoff")
        if self.complete and (
            self.book.net_liquidation is None
            or self.book.cash_available is None
            or not self.book.cash_source_id
        ):
            raise ValueError("complete authoritative bundle requires PostgreSQL book and cash evidence")
        if self.complete and (
            not self.constraints.risk_policy_hash
            or not self.constraints.risk_policy_version
            or self.constraints.position_limit is None
            or self.constraints.aggregate_loss_limit is None
        ):
            raise ValueError("complete authoritative bundle requires persisted PostgreSQL risk constraints")
        for candidate in self.candidates:
            if candidate.input_cutoff and candidate.input_cutoff.astimezone(UTC) > self.input_cutoff.astimezone(UTC):
                raise ValueError("authoritative candidate is newer than the bundle cutoff")
            if self.complete and (
                not candidate.portfolio_impact_id
                or not candidate.source_decision_id
                or not candidate.source_input_hash
                or candidate.source_input_hash == "0" * 64
                or not candidate.source_decision_input_hash
                or candidate.source_decision_input_hash == "0" * 64
                or candidate.risk_evidence is None
                or not candidate.experiment_id
                or not candidate.trial_id
                or not candidate.result_id
            ):
                raise ValueError("authoritative candidate is missing PostgreSQL decision provenance")
            if candidate.risk_evidence is not None:
                evidence = candidate.risk_evidence
                if evidence.impact_id != candidate.portfolio_impact_id or evidence.ticker != candidate.ticker:
                    raise ValueError("authoritative candidate risk evidence does not match its impact")
                if evidence.source_input_hash != candidate.source_input_hash or evidence.source_decision_input_hash != candidate.source_decision_input_hash:
                    raise ValueError("authoritative candidate risk digest lineage does not match persisted decision")
                if candidate.covariance is None or set(candidate.covariance) != {item.ticker for item in self.candidates}:
                    raise ValueError("authoritative candidate covariance does not cover the complete bundle")
                for name in ("expected_return", "uncertainty", "volatility", "risk_budget", "kelly_cap", "drawdown_cap", "capacity", "overlap_penalty", "execution_penalty", "covariance"):
                    if getattr(evidence, name) != getattr(candidate, name):
                        raise ValueError("authoritative candidate risk values do not match typed PostgreSQL evidence")
            if self.complete and (candidate.rank_position is None or candidate.rank_utility is None):
                raise ValueError("authoritative candidate is missing the persisted opportunity rank")
        if self.complete:
            for candidate in self.candidates:
                row = candidate.covariance or {}
                diagonal = float(row.get(candidate.ticker, 0.0))
                if diagonal < 0:
                    raise ValueError("authoritative covariance variance must be non-negative")
                for other in self.candidates:
                    other_row = other.covariance or {}
                    forward = float(row.get(other.ticker, 0.0))
                    reverse = float(other_row.get(candidate.ticker, 0.0))
                    if abs(forward - reverse) > 1e-9 or diagonal * float(other_row.get(other.ticker, 0.0)) - forward * reverse < -1e-9:
                        raise ValueError("authoritative covariance must be symmetric and positive semidefinite")
        return self

    @property
    def cash_hurdle(self) -> float | None:
        return self.constraints.cash_hurdle

    @property
    def scenario_observations(self) -> tuple[dict[str, Any], ...]:
        return self.scenario.observations


class PortfolioAllocationItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allocation_item_id: str
    candidate_id: str
    ticker: str
    strategy_forecast_id: str | None = None
    action_id: str | None = None
    rank_id: str | None = None
    hypothesis_id: str | None = None
    disposition: str
    target_weight: float = Field(ge=0, le=1)
    current_weight: float = Field(ge=0, le=1)
    marginal_book_utility: float
    funding_source: str | None = None
    funding_amount: float | None = None
    funding_sources: dict[str, float] = Field(default_factory=dict)
    blockers: tuple[str, ...] = ()
    trace: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_item(self) -> "PortfolioAllocationItem":
        if self.disposition not in {"selected", "ranked_out", "rejected", "rollback"}:
            raise ValueError("invalid allocation disposition")
        if not isfinite(self.marginal_book_utility):
            raise ValueError("marginal_book_utility must be finite")
        if self.disposition == "selected" and self.ticker != "CASH" and (
            self.target_weight <= 0 or self.marginal_book_utility <= 0
        ):
            raise ValueError("funded allocation requires positive marginal book utility")
        if self.disposition == "selected" and self.ticker != "CASH" and self.target_weight > self.current_weight and not self.funding_source:
            raise ValueError("funded allocation requires a funding source")
        if self.disposition == "selected" and self.ticker != "CASH" and self.target_weight > self.current_weight and (
            self.funding_amount is None or not isfinite(self.funding_amount) or self.funding_amount <= 0
        ):
            raise ValueError("funded allocation requires a positive funding amount")
        if self.disposition == "rollback" and (
            not self.funding_source or self.funding_amount is None or not isfinite(self.funding_amount) or self.funding_amount <= 0
        ):
            raise ValueError("rollback allocation requires persisted trim funding")
        if any(not source or not isfinite(amount) or amount <= 0 for source, amount in self.funding_sources.items()):
            raise ValueError("funding sources must be positive named amounts")
        if self.funding_sources and self.funding_amount is not None and abs(sum(self.funding_sources.values()) - self.funding_amount) > 1e-9:
            raise ValueError("funding sources must conserve the funded amount")
        return self


class PortfolioAllocationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allocation_id: str
    as_of: datetime
    input_cutoff: datetime
    cash_hurdle: float | None = Field(default=None, ge=0)
    status: str
    items: tuple[PortfolioAllocationItem, ...]
    forecast_ids: tuple[str, ...] = ()
    action_ids: tuple[str, ...] = ()
    strategy_registry_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_identity(self) -> "PortfolioAllocationSnapshot":
        if self.as_of.tzinfo is None or self.input_cutoff.tzinfo is None:
            raise ValueError("allocation clocks must be timezone-aware")
        if self.as_of.astimezone(UTC) != self.input_cutoff.astimezone(UTC):
            raise ValueError("allocation as_of and input_cutoff must match")
        if self.status == "available" and (self.cash_hurdle is None or self.cash_hurdle <= 0):
            raise ValueError("available allocation requires a positive persisted cash hurdle")
        if self.allocation_id != allocation_id_for_snapshot(self):
            raise ValueError("allocation identity does not match its immutable payload")
        return self


class PortfolioActionDTO(BaseModel):
    """Canonical action read model for every Phase 4 consumer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allocation_id: str
    allocation_item_id: str
    ticker: str
    disposition: str
    strategy_forecast_id: str | None = None
    action_id: str | None = None
    rank_id: str | None = None
    expression: dict[str, Any] | None = None
    invalidation: dict[str, Any] | None = None
    why_trade: str | None = None
    why_now: tuple[str, ...] = ()
    next_action: str | None = None
    missing_data: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    target_weight: float
    current_weight: float
    marginal_book_utility: float
    current_mrc: float | None = None
    proposed_mrc: float | None = None
    funding_source: str | None = None
    funding_sources: dict[str, float] = Field(default_factory=dict)
    sizing_trace: dict[str, Any]

    @model_validator(mode="after")
    def validate_funding(self) -> "PortfolioActionDTO":
        if self.disposition == "selected" and self.ticker != "CASH" and self.target_weight > self.current_weight and not self.funding_source:
            raise ValueError("canonical funded action requires funding")
        return self


class PortfolioScenarioDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_artifact_id: str
    allocation_id: str
    scenarios: tuple[dict[str, Any], ...]
    tail_dependence: dict[str, Any]
    simultaneous_unwind: dict[str, Any]


class PortfolioExecutionDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_model_snapshot_id: str
    allocation_id: str
    calibration_status: str
    sample_count: int = Field(ge=0)


class PortfolioIntegratedDTO(BaseModel):
    """One typed, immutable allocation view shared by all five workspaces."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allocation_id: str
    input_cutoff: datetime
    status: str
    actions: tuple[PortfolioActionDTO, ...]
    scenario_artifact_id: str | None = None
    execution_model_snapshot_id: str | None = None
    scenario: PortfolioScenarioDTO | None = None
    execution: PortfolioExecutionDTO | None = None
    attribution_count: int = 0
    postmortem: tuple[dict[str, Any], ...] = ()


def integrated_portfolio_dto(
    allocation: PortfolioAllocationSnapshot,
    *,
    scenario_artifact_id: str | None = None,
    execution_model_snapshot_id: str | None = None,
    scenario: Mapping[str, Any] | None = None,
    execution: Mapping[str, Any] | None = None,
    attribution_count: int = 0,
    postmortem: tuple[Mapping[str, Any], ...] = (),
) -> PortfolioIntegratedDTO:
    """Project persisted allocation rows into the single canonical UI DTO."""

    actions: list[PortfolioActionDTO] = []
    for item in allocation.items:
        trace = dict(item.trace)
        actions.append(PortfolioActionDTO(
            allocation_id=allocation.allocation_id,
            allocation_item_id=item.allocation_item_id,
            ticker=item.ticker,
            disposition=item.disposition,
            strategy_forecast_id=item.strategy_forecast_id,
            action_id=item.action_id,
            rank_id=item.rank_id,
            expression=trace.get("expression") if isinstance(trace.get("expression"), dict) else None,
            invalidation=trace.get("invalidation") if isinstance(trace.get("invalidation"), dict) else None,
            why_trade=(str(trace["why_trade"]) if trace.get("why_trade") is not None else None),
            why_now=tuple(str(value) for value in trace.get("why_now", ()) or ()),
            next_action=(str(trace["next_action"]) if trace.get("next_action") is not None else None),
            missing_data=tuple(str(value) for value in trace.get("missing_data", ()) or ()),
            blockers=item.blockers,
            target_weight=item.target_weight,
            current_weight=item.current_weight,
            marginal_book_utility=item.marginal_book_utility,
            current_mrc=(trace.get("current_marginal_risk_contribution") if isinstance(trace.get("current_marginal_risk_contribution"), (int, float)) else None),
            proposed_mrc=(trace.get("proposed_marginal_risk_contribution") if isinstance(trace.get("proposed_marginal_risk_contribution"), (int, float)) else None),
            funding_source=item.funding_source,
            funding_sources=dict(item.funding_sources),
            sizing_trace=trace,
        ))
    return PortfolioIntegratedDTO(
        allocation_id=allocation.allocation_id,
        input_cutoff=allocation.input_cutoff,
        status=allocation.status,
        actions=tuple(actions),
        scenario_artifact_id=scenario_artifact_id,
        execution_model_snapshot_id=execution_model_snapshot_id,
        scenario=(PortfolioScenarioDTO.model_validate(scenario) if scenario is not None else None),
        execution=(PortfolioExecutionDTO.model_validate(execution) if execution is not None else None),
        attribution_count=attribution_count,
        postmortem=tuple(dict(row) for row in postmortem),
    )


def _rejection(candidate: PortfolioCandidate, blockers: tuple[str, ...]) -> PortfolioAllocationItem:
    payload = {"candidate_id": candidate.candidate_id, "ticker": candidate.ticker, "disposition": "rejected", "blockers": blockers}
    return PortfolioAllocationItem(
        allocation_item_id=f"allocation-item:{_hash(payload)}", candidate_id=candidate.candidate_id,
        ticker=candidate.ticker, strategy_forecast_id=candidate.strategy_forecast_id,
        action_id=candidate.action_id, rank_id=candidate.rank_id, hypothesis_id=candidate.hypothesis_id, disposition="rejected",
        target_weight=0, current_weight=candidate.current_weight, marginal_book_utility=0,
        blockers=blockers, trace={"gate": "fail_closed"},
    )


def allocate_portfolio(
    bundle: AuthoritativePortfolioBundle,
    *,
    as_of: datetime,
) -> PortfolioAllocationSnapshot:
    """Production allocation is issued only by PortfolioLoopRepository.

    A Python object cannot be a repository authority.  Keeping this public
    compatibility entry point fail-closed prevents callers from minting an
    authoritative allocation with imported module state.
    """

    del bundle, as_of
    raise TypeError("production allocation is repository and PostgreSQL bound")


def _compute_portfolio_allocation(
    candidates: list[PortfolioCandidate | Mapping[str, Any]] | AuthoritativePortfolioBundle,
    *,
    as_of: datetime,
    cash_hurdle: float | None = None,
    book: PortfolioBookEvidence | None = None,
    constraints: PortfolioConstraintEvidence | None = None,
    execution: PortfolioExecutionEvidence | None = None,
    connection: Any | None = None,
) -> PortfolioAllocationSnapshot:
    """Compute weights from already separated inputs; never issue authority.

    A bundle is a repository output, not a callable authorization token.  The
    production repository passes its separately verified evidence models.  A
    caller-built bundle is rejected here, so importing this module cannot mint
    a PostgreSQL allocation.
    """

    if as_of.tzinfo is None:
        raise ValueError("allocation clock and cash hurdle must be valid")
    if isinstance(candidates, AuthoritativePortfolioBundle):
        raise TypeError("authoritative allocation requires the PostgreSQL repository")
    authoritative = book is not None or constraints is not None or execution is not None
    if authoritative and (book is None or constraints is None or execution is None):
        raise TypeError("allocator authority inputs must be complete")
    if authoritative and connection is None:
        raise TypeError("authoritative allocation requires a PostgreSQL connection")
    if authoritative:
        if cash_hurdle is None or execution.calibration_status != "calibrated" or not execution.snapshot_id or execution.sample_count <= 0:
            return cash_only_allocation(as_of, cash_hurdle, "execution_calibration_pending")
        if execution.input_cutoff.astimezone(UTC) > as_of.astimezone(UTC):
            return cash_only_allocation(as_of, cash_hurdle, "execution_calibration_cutoff_mismatch")
        normalized = [item if isinstance(item, PortfolioCandidate) else PortfolioCandidate.model_validate(item) for item in candidates]
        for candidate in normalized:
            if not connection.execute(
                """SELECT 1 FROM analysis.ticker_decision
                   WHERE id::text = %s AND input_hash = %s
                     AND status = 'published' AND published_at IS NOT NULL
                     AND input_manifest->'trade_plan'->>'trade_plan_id' = %s
                     AND input_manifest->'trade_plan'->>'rank_id' = %s
                     AND input_manifest->'trade_plan'->>'strategy_forecast_id' = %s
                   LIMIT 1""",
                [candidate.source_decision_id, candidate.source_decision_input_hash,
                 candidate.action_id, candidate.rank_id, candidate.strategy_forecast_id],
            ).fetchone():
                return cash_only_allocation(as_of, cash_hurdle, "postgresql_decision_lineage_unverified")
        allocator_metadata = {}
    else:
        if cash_hurdle is None or not isfinite(cash_hurdle) or cash_hurdle < 0:
            raise ValueError("allocation cash hurdle must be explicit")
        cash_hurdle = float(cash_hurdle)
        if any(isinstance(item, Mapping) for item in candidates):
            raise TypeError("production allocation requires a PostgreSQL AuthoritativePortfolioBundle; mappings are not allocator authority")
        normalized = [item if isinstance(item, PortfolioCandidate) else PortfolioCandidate.model_validate(item) for item in candidates]
        allocator_metadata = {}
    if cash_hurdle is None or not isfinite(cash_hurdle) or cash_hurdle <= 0:
        return cash_only_allocation(as_of, cash_hurdle, "cash_hurdle_missing")
    cash_hurdle = float(cash_hurdle)
    if len({item.candidate_id for item in normalized}) != len(normalized):
        raise ValueError("allocation candidates must have unique IDs")
    ranked: list[PortfolioAllocationItem] = []
    eligible: list[tuple[PortfolioCandidate, float, dict[str, Any]]] = []
    for candidate in normalized:
        blockers = list(candidate.blockers)
        if authoritative:
            for name in ("factor_exposure", "sector", "asset_class", "greeks", "liquidity", "venue"):
                if getattr(candidate, name) is None:
                    blockers.append(f"{name}_evidence_missing")
        if candidate.evidence_status != "available":
            blockers.append(f"evidence_{candidate.evidence_status}")
        missing = [name for name in ("strategy_forecast_id", "action_id", "rank_id", "expected_return", "uncertainty", "volatility", "risk_budget", "kelly_cap", "drawdown_cap", "capacity", "covariance") if getattr(candidate, name) is None]
        blockers.extend(f"{name}_missing" for name in missing)
        blockers.extend(candidate.missing_data)
        if candidate.input_cutoff is None or candidate.available_at is None:
            blockers.append("pit_lineage_missing")
        elif candidate.available_at.astimezone(UTC) > candidate.input_cutoff.astimezone(UTC):
            blockers.append("pit_lineage_conflict")
        if candidate.volatility is not None and candidate.volatility <= 0:
            blockers.append("volatility_invalid")
        if candidate.capacity is not None and candidate.capacity <= 0:
            blockers.append("capacity_unavailable")
        if candidate.overlap_penalty is not None and candidate.overlap_penalty > 0:
            blockers.append("overlap_conflict")
        if candidate.execution_penalty is not None and candidate.execution_penalty > 0:
            blockers.append("execution_unavailable")
        if candidate.current_weight <= 0 and (
            candidate.cash_available is None or candidate.cash_available <= 0 or not candidate.cash_source_id
        ):
            blockers.append("cash_funding_missing")
        if candidate.current_weight > 0 and (not candidate.trim_position_id or candidate.trim_available is None or candidate.trim_available <= 0):
            blockers.append("trim_position_missing")
        if blockers:
            ranked.append(_rejection(candidate, tuple(dict.fromkeys(blockers))))
            continue
        utility = candidate.rank_utility
        if utility is None or not isfinite(utility):
            ranked.append(_rejection(candidate, ("rank_utility_missing",)))
            continue
        trace = {
            "input_cutoff": candidate.input_cutoff,
            "available_at": candidate.available_at,
            "strategy_forecast_id": candidate.strategy_forecast_id,
            "action_id": candidate.action_id,
            "rank_id": candidate.rank_id,
            "portfolio_impact_id": candidate.portfolio_impact_id,
            "source_decision_id": candidate.source_decision_id,
            "source_input_hash": candidate.source_input_hash,
            "source_decision_input_hash": candidate.source_decision_input_hash,
            "experiment_id": candidate.experiment_id,
            "trial_id": candidate.trial_id,
            "result_id": candidate.result_id,
            "current_weight": candidate.current_weight,
            "expected_return": candidate.expected_return,
            "uncertainty": candidate.uncertainty,
            "uncertainty_adjusted_return": candidate.expected_return - candidate.uncertainty,
            "overlap_penalty": candidate.overlap_penalty or 0,
            "execution_penalty": candidate.execution_penalty or 0,
            "marginal_book_utility": utility,
            "rank_position": candidate.rank_position,
            "rank_utility": utility,
            "volatility": candidate.volatility,
            "risk_budget": candidate.risk_budget,
            "kelly_cap": candidate.kelly_cap,
            "drawdown_cap": candidate.drawdown_cap,
                "capacity": candidate.capacity,
                "days_to_exit": candidate.days_to_exit,
                "liquidity": candidate.liquidity,
                "expression": candidate.expression,
                "invalidation": candidate.invalidation,
                "why_trade": candidate.why_trade,
                "why_now": list(candidate.why_now),
            "missing_data": list(candidate.missing_data),
            "cash_available": candidate.cash_available,
            "cash_source_id": candidate.cash_source_id,
            "trim_position_id": candidate.trim_position_id,
            "trim_available": candidate.trim_available,
            "factor_exposure": candidate.factor_exposure,
            "sector": candidate.sector,
            "asset_class": candidate.asset_class,
            "greeks": candidate.greeks,
            "venue": candidate.venue,
        }
        eligible.append((candidate, utility, trace))
    eligible.sort(key=lambda item: (item[0].rank_position or 2**31, item[0].candidate_id))
    nav = book.net_liquidation if authoritative else None
    fundable = [(candidate, utility, trace) for candidate, utility, trace in eligible if utility > cash_hurdle]
    for candidate, utility, trace in eligible:
        if utility <= cash_hurdle and candidate.current_weight > 0 and candidate.trim_position_id and (candidate.trim_available or 0) > 0:
            trim_release = min(
                float(candidate.trim_available),
                candidate.current_weight * float(nav),
            ) if nav is not None and nav > 0 else float(candidate.trim_available)
            ranked.append(PortfolioAllocationItem(
                allocation_item_id=f"allocation-item:{_hash({'candidate_id': candidate.candidate_id, 'disposition': 'rollback', 'trim': candidate.trim_position_id})}",
                candidate_id=candidate.candidate_id, ticker=candidate.ticker, strategy_forecast_id=candidate.strategy_forecast_id,
                action_id=candidate.action_id, rank_id=candidate.rank_id, hypothesis_id=candidate.hypothesis_id, disposition="rollback",
                target_weight=0, current_weight=candidate.current_weight, marginal_book_utility=utility,
                funding_source=f"TRIM:{candidate.trim_position_id}", funding_amount=trim_release,
                funding_sources={f"TRIM:{candidate.trim_position_id}": trim_release},
                blockers=("below_cash_hurdle",), trace=_json({**trace, "next_action": "Review the holding for exit and release its persisted trim value."}),
            ))
        elif utility <= cash_hurdle and candidate.current_weight <= 0:
            ranked.append(PortfolioAllocationItem(
                allocation_item_id=f"allocation-item:{_hash({'candidate_id': candidate.candidate_id, 'blockers': ('below_cash_hurdle',)})}",
                candidate_id=candidate.candidate_id, ticker=candidate.ticker, strategy_forecast_id=candidate.strategy_forecast_id,
                action_id=candidate.action_id, rank_id=candidate.rank_id, hypothesis_id=candidate.hypothesis_id, disposition="ranked_out",
                target_weight=0, current_weight=candidate.current_weight, marginal_book_utility=utility,
                blockers=("below_cash_hurdle",), trace=_json(trace),
            ))
    if authoritative:
        policy = constraints
        admissible: list[tuple[PortfolioCandidate, float, dict[str, Any]]] = []
        for candidate, utility, trace in fundable:
            liquidity = (candidate.liquidity or {}).get("score", (candidate.liquidity or {}).get("available_notional"))
            blockers = ([] if policy.min_liquidity is None or (liquidity is not None and float(liquidity) >= policy.min_liquidity) else ["liquidity_limit"])
            if policy.allowed_venues and candidate.venue not in policy.allowed_venues:
                blockers.append("venue_not_allowed")
            if blockers:
                ranked.append(_rejection(candidate, tuple(blockers)))
            else:
                admissible.append((candidate, utility, trace))
        fundable = admissible
    position_limit = constraints.position_limit if authoritative and constraints.position_limit is not None else 1.0
    # Existing holdings are fixed book exposure.  If the repository cannot
    # supply their factor/sector/Greek evidence, it must not add risk under a
    # joint policy.  This prevents an optimizer from treating unknown holdings
    # as cash.
    existing_weight = 0.0
    existing_exposures: list[tuple[str, float, dict[str, Any]]] = []
    if authoritative:
        candidate_tickers = {candidate.ticker.upper() for candidate, _, _ in eligible}
        non_candidate = set(book.position_weights) - candidate_tickers
        existing_weight = sum(book.position_weights[ticker] for ticker in non_candidate)
        existing_exposures = [
            (ticker, book.position_weights[ticker], book.position_exposures.get(ticker, {}))
            for ticker in non_candidate
        ]
        policy = constraints
        for limits_map, key in ((policy.sector_limits, "sector"), (policy.asset_class_limits, "asset_class"),
                                (policy.factor_limits, "factor_exposure"), (policy.greek_limits, "greeks")):
            if limits_map and any(key not in exposure for _, _, exposure in existing_exposures):
                return cash_only_allocation(as_of, cash_hurdle, "existing_holding_joint_evidence_missing")
    caps = [min(candidate.risk_budget, candidate.kelly_cap, candidate.drawdown_cap, candidate.capacity, position_limit) * max(0.0, min(1.0, 1.0 - candidate.uncertainty / max(abs(candidate.expected_return), 1e-12))) for candidate, _, _ in fundable]
    # A candidate is never allowed to fund its own increase by naming its held
    # position as a trim source.  Held names can hold or reduce only.
    caps = [min(cap, candidate.current_weight) if candidate.current_weight > 0 else cap for cap, (candidate, _, _) in zip(caps, fundable)]
    limits = [(0.0, max(0.0, min(1.0, cap))) for cap in caps]
    policy = constraints if authoritative else None
    n_fundable = len(fundable)

    # Funding is part of the optimizer state.  The flow variables below are
    # source-to-increase allocations, while each trim source's capacity is a
    # function of the same joint weight vector.  This keeps source release and
    # source consumption conserved in one solution instead of assigning a
    # scalar budget with a later first-fit pass.
    source_limits: dict[str, float] = {}
    source_max: dict[str, float] = {}
    fixed_releases: dict[str, float] = {}
    trim_indices: dict[str, list[tuple[int, float]]] = {}
    increase_indices = [index for index, (candidate, _, _) in enumerate(fundable) if candidate.current_weight <= 0]
    scale = float(nav if nav is not None else 1.0)
    if authoritative or fundable:
        if authoritative and book.cash_source_id and book.cash_available is not None:
            cash_source = f"CASH:{book.cash_source_id}"
            source_limits[cash_source] = float(book.cash_available)
            source_max[cash_source] = float(book.cash_available)
        if not authoritative:
            for candidate, _, _ in fundable:
                if candidate.current_weight <= 0 and candidate.cash_source_id and candidate.cash_available is not None:
                    source = f"CASH:{candidate.cash_source_id}"
                    source_limits[source] = max(source_limits.get(source, 0.0), float(candidate.cash_available))
                    source_max[source] = max(source_max.get(source, 0.0), float(candidate.cash_available))
        for candidate, utility, _ in eligible:
            if utility > cash_hurdle or candidate.current_weight <= 0 or not candidate.trim_position_id:
                continue
            release = min(candidate.current_weight * scale, float(candidate.trim_available or 0.0))
            if release <= 0:
                continue
            source = f"TRIM:{candidate.trim_position_id}"
            fixed_releases[source] = max(fixed_releases.get(source, 0.0), release)
            source_max[source] = max(source_max.get(source, 0.0), release)
        for index, (candidate, _, _) in enumerate(fundable):
            if candidate.current_weight <= 0 or not candidate.trim_position_id or candidate.trim_available is None:
                continue
            source = f"TRIM:{candidate.trim_position_id}"
            available = min(candidate.current_weight * scale, float(candidate.trim_available))
            if available <= 0:
                continue
            # One persisted position is one source.  Duplicate candidate rows
            # cannot mint duplicate capacity; the source is bounded by the
            # largest authoritative position value observed for that ID.
            source_max[source] = max(source_max.get(source, 0.0), available)
            trim_indices.setdefault(source, []).append((index, available))

    def _weights(vector: Any) -> list[float]:
        return [float(value) for value in vector[:n_fundable]]

    flow_pairs = [
        (candidate_index, source)
        for candidate_index in increase_indices
        for source in sorted(source_max)
    ] if source_max else []
    flow_offset = n_fundable

    def _flow(vector: Any, candidate_index: int, source: str) -> float:
        try:
            return max(0.0, float(vector[flow_offset + flow_pairs.index((candidate_index, source))]))
        except ValueError:
            return 0.0

    def _source_capacity(vector: Any, source: str) -> float:
        if not source_max:
            return 0.0
        weights = _weights(vector)
        released = fixed_releases.get(source, 0.0)
        for candidate_index, available in trim_indices.get(source, ()):
            candidate = fundable[candidate_index][0]
            released += min(max(0.0, candidate.current_weight - weights[candidate_index]) * scale, available)
        return min(source_max.get(source, 0.0), released + source_limits.get(source, 0.0)) / scale

    constraints = [{"type": "ineq", "fun": lambda vector: 1.0 - existing_weight - sum(_weights(vector))}]
    if source_max:
        for source in sorted(source_max):
            constraints.append({
                "type": "ineq",
                "fun": lambda vector, source=source: _source_capacity(vector, source)
                - sum(_flow(vector, candidate_index, source) for candidate_index in increase_indices),
            })
        for candidate_index in increase_indices:
            constraints.append({
                "type": "eq",
                "fun": lambda vector, candidate_index=candidate_index: sum(
                    _flow(vector, candidate_index, source) for source in sorted(source_max)
                ) - _weights(vector)[candidate_index],
            })
    if authoritative:
        covariance_values = [float((candidate.covariance or {}).get(candidate.ticker, 0.0)) for candidate, _, _ in fundable]
        if any(not isfinite(value) or value < 0 for value in covariance_values):
            return cash_only_allocation(as_of, cash_hurdle, "covariance_not_positive_semidefinite")
        if any(
            abs(float((left.covariance or {}).get(right.ticker, 0.0)) - float((right.covariance or {}).get(left.ticker, 0.0))) > 1e-9
            for left, _, _ in fundable for right, _, _ in fundable
        ):
            return cash_only_allocation(as_of, cash_hurdle, "covariance_not_symmetric")
        for ticker in {candidate.ticker.upper() for candidate, _, _ in fundable}:
            constraints.append({"type": "ineq", "fun": lambda vector, ticker=ticker: position_limit - sum(float(weight) for weight, (candidate, _, _) in zip(_weights(vector), fundable) if candidate.ticker.upper() == ticker)})
        for label, limits_map, attribute in (("sector", policy.sector_limits, "sector"), ("asset_class", policy.asset_class_limits, "asset_class")):
            for key, limit in limits_map.items():
                constraints.append({"type": "ineq", "fun": lambda vector, key=key, limit=limit, attribute=attribute: limit - sum(weight for _, weight, exposure in existing_exposures if exposure.get(attribute) == key) - sum(float(weight) for weight, (candidate, _, _) in zip(_weights(vector), fundable) if getattr(candidate, attribute) == key)})
        for limits_map, attribute in ((policy.factor_limits, "factor_exposure"), (policy.greek_limits, "greeks")):
            for key, limit in limits_map.items():
                constraints.append({"type": "ineq", "fun": lambda vector, key=key, limit=limit, attribute=attribute: limit - sum(abs(float(weight) * float(exposure.get(attribute, {}).get(key, 0))) for _, weight, exposure in existing_exposures) - sum(abs(float(weight) * float((getattr(candidate, attribute) or {}).get(key, 0))) for weight, (candidate, _, _) in zip(_weights(vector), fundable))})
        risk_limit = min(sum(candidate.risk_budget for candidate, _, _ in fundable), policy.aggregate_loss_limit or float("inf"))
        constraints.append({"type": "ineq", "fun": lambda vector: risk_limit - sum(float(left) * float(right) * float((candidate.covariance or {}).get(other.ticker, 0)) for left, (candidate, _, _), right, (other, _, _) in ((left, first, right, second) for left, first in zip(_weights(vector), fundable) for right, second in zip(_weights(vector), fundable))) ** 0.5})
    bounds = limits + ([(0.0, None)] * len(flow_pairs) if source_max else [])
    initial = [lower for lower, _ in limits] + ([0.0] * len(flow_pairs) if source_max else [])
    result = minimize(
        lambda vector: -sum(float(weight) * (utility if utility > cash_hurdle else -abs(utility)) for weight, (_, utility, _) in zip(_weights(vector), fundable)),
        initial, bounds=bounds, constraints=constraints, method="SLSQP",
    ) if fundable else None
    vector = list(result.x) if result is not None and result.success else [0.0] * len(bounds)
    weights = _weights(vector)
    for candidate_index, (candidate, utility, trace) in enumerate(fundable):
        target = max(0.0, float(weights[candidate_index]))
        if target <= 1e-8:
            ranked.append(_rejection(candidate, ("joint_optimizer_ranked_out",)))
            continue
        increased = target > candidate.current_weight + 1e-8
        decreased = target < candidate.current_weight - 1e-8
        funding_amount = abs(target - candidate.current_weight) * (nav if nav is not None else 1.0)
        funding_source = None
        funding_sources: dict[str, float] = {}
        if increased:
            solved_flows = {
                source: _flow(vector, candidate_index, source) * scale
                for source in sorted(source_max)
                if _flow(vector, candidate_index, source) > 1e-8
            }
            flow_total = sum(solved_flows.values())
            if flow_total <= 0 or abs(flow_total - funding_amount) > max(1e-6, funding_amount * 1e-7):
                ranked.append(_rejection(candidate, ("funding_source_conservation_failed",)))
                continue
            # Only normalize solver round-off.  The source proportions come
            # from the joint solution, not from a first-fit assignment.
            funding_sources = {source: amount * funding_amount / flow_total for source, amount in solved_flows.items()}
            funding_source = next(iter(funding_sources)) if len(funding_sources) == 1 else "MULTI_SOURCE"
        elif decreased:
            funding_source = f"TRIM:{candidate.trim_position_id}" if candidate.trim_position_id else "TRIM:book"
            funding_sources = {funding_source: funding_amount}
        trace = {**trace, "optimizer": "SLSQP", "constraint_weight": caps[candidate_index], "uncertainty_haircut": max(0.0, min(1.0, 1.0 - candidate.uncertainty / max(abs(candidate.expected_return), 1e-12))), "weight_delta": target - candidate.current_weight, "funding_amount": funding_amount, "funding_sources": funding_sources, "trim_position_id": candidate.trim_position_id, "released_trim_funding": funding_amount if decreased else 0.0}
        ranked.append(PortfolioAllocationItem(
            allocation_item_id=f"allocation-item:{_hash({'candidate_id': candidate.candidate_id, 'target_weight': target, 'trace': trace})}",
            candidate_id=candidate.candidate_id, ticker=candidate.ticker, strategy_forecast_id=candidate.strategy_forecast_id,
            action_id=candidate.action_id, rank_id=candidate.rank_id, hypothesis_id=candidate.hypothesis_id, disposition="selected",
            target_weight=target, current_weight=candidate.current_weight, marginal_book_utility=utility,
            funding_source=funding_source,
            funding_amount=funding_amount, funding_sources=funding_sources, trace=_json(trace),
        ))
    remaining = max(0.0, 1.0 - existing_weight - sum(item.target_weight for item in ranked if item.disposition == "selected"))
    cash_payload = {"candidate_id": "CASH", "ticker": "CASH", "disposition": "selected", "target_weight": remaining}
    ranked.append(PortfolioAllocationItem(
        allocation_item_id=f"allocation-item:{_hash(cash_payload)}", candidate_id="CASH", ticker="CASH",
        disposition="selected", target_weight=remaining, current_weight=0, marginal_book_utility=0,
        funding_source="CASH", funding_amount=max(remaining, 1e-12), trace={
            "cash_hurdle": cash_hurdle, "why_trade": "Preserve capital until a candidate clears the joint constraints.",
            "why_now": ["The persisted risk and execution evidence does not justify a funded increase."],
            "expression": {"kind": "CASH"}, "invalidation": {"kind": "data", "reason": "candidate_not_fundable"},
            "missing_data": [], "next_action": "Refresh the PostgreSQL decision bundle and wait for a valid opportunity.",
        },
    ))
    candidates_by_id = {candidate.candidate_id: candidate for candidate in normalized}
    proposed_weights = {item.candidate_id: item.target_weight for item in ranked if item.disposition == "selected" and item.ticker != "CASH"}
    current_weights = {candidate.candidate_id: candidate.current_weight for candidate in normalized}
    covariance = {candidate.candidate_id: candidate.covariance or {} for candidate in normalized}

    def portfolio_volatility(weights: dict[str, float]) -> float:
        variance = sum(
            weight_i * weight_j * float(covariance.get(candidate_id_i, {}).get(candidates_by_id[candidate_id_j].ticker, covariance.get(candidate_id_i, {}).get(candidate_id_j, 0.0)))
            for candidate_id_i, weight_i in weights.items()
            for candidate_id_j, weight_j in weights.items()
        )
        return max(variance, 0.0) ** 0.5

    proposed_volatility = portfolio_volatility(proposed_weights)
    current_volatility = portfolio_volatility(current_weights)
    updated: list[PortfolioAllocationItem] = []
    for item in ranked:
        candidate = candidates_by_id.get(item.candidate_id)
        if candidate is None or item.ticker == "CASH":
            updated.append(item)
            continue
        proposed_mrc = (
            sum(weight * float(covariance[item.candidate_id].get(candidates_by_id[ticker].ticker, covariance[item.candidate_id].get(ticker, 0.0))) for ticker, weight in proposed_weights.items()) / proposed_volatility
            if proposed_volatility > 0 else None
        )
        current_mrc = (
            sum(weight * float(covariance[item.candidate_id].get(candidates_by_id[ticker].ticker, covariance[item.candidate_id].get(ticker, 0.0))) for ticker, weight in current_weights.items()) / current_volatility
            if current_volatility > 0 else None
        )
        trace = {
            **item.trace,
            "proposed_portfolio_volatility": proposed_volatility,
            "current_portfolio_volatility": current_volatility,
            "proposed_marginal_risk_contribution": proposed_mrc,
            "current_marginal_risk_contribution": current_mrc,
        }
        item_id = f"allocation-item:{_hash({'candidate_id': item.candidate_id, 'ticker': item.ticker, 'disposition': item.disposition, 'target_weight': item.target_weight, 'trace': trace})}"
        updated.append(item.model_copy(update={"allocation_item_id": item_id, "trace": trace}))
    allocation_seed = _hash({"as_of": as_of, "cash_hurdle": cash_hurdle, "candidate_ids": sorted(candidate.candidate_id for candidate in normalized)})
    ranked = [item.model_copy(update={
        "allocation_item_id": f"allocation-item:{_hash({'allocation_seed': allocation_seed, 'item': item.model_dump(mode='json')})}"
    }) for item in updated]
    ranked.sort(key=lambda item: (0 if item.ticker == "CASH" else 1, item.disposition, item.candidate_id))
    selected = [item for item in ranked if item.disposition == "selected" and item.ticker != "CASH"]
    persisted_items = [item for item in ranked if item.disposition in {"selected", "rollback"} and item.ticker != "CASH"]
    forecast_ids = tuple(sorted(item.strategy_forecast_id for item in persisted_items if item.strategy_forecast_id))
    action_ids = tuple(sorted(item.action_id for item in persisted_items if item.action_id))
    registry_ids = tuple(sorted(candidate.strategy_registry_id for candidate, _, _ in eligible if candidate.strategy_registry_id))
    allocation_payload = {"as_of": as_of, "cash_hurdle": cash_hurdle, "items": ranked, "forecast_ids": forecast_ids, "action_ids": action_ids, "strategy_registry_ids": registry_ids, "metadata": allocator_metadata}
    return PortfolioAllocationSnapshot(
        allocation_id=allocation_id_for_snapshot(allocation_payload), as_of=as_of, input_cutoff=as_of,
        cash_hurdle=cash_hurdle, status="available" if selected else "cash_only", items=tuple(ranked),
        forecast_ids=forecast_ids, action_ids=action_ids, strategy_registry_ids=registry_ids,
        metadata=allocator_metadata,
    )


def _allocate_portfolio(
    candidates: list[PortfolioCandidate | Mapping[str, Any]] | AuthoritativePortfolioBundle,
    *,
    as_of: datetime,
    cash_hurdle: float | None = None,
    book: PortfolioBookEvidence | None = None,
    constraints: PortfolioConstraintEvidence | None = None,
    execution: PortfolioExecutionEvidence | None = None,
) -> PortfolioAllocationSnapshot:
    if book is not None or constraints is not None or execution is not None or isinstance(candidates, AuthoritativePortfolioBundle):
        raise TypeError("repository and PostgreSQL repository bound")
    return _compute_portfolio_allocation(candidates, as_of=as_of, cash_hurdle=cash_hurdle)


def allocate_portfolio_for_tests(
    candidates: list[PortfolioCandidate | Mapping[str, Any]],
    *,
    as_of: datetime,
    cash_hurdle: float | None = None,
    book: PortfolioBookEvidence | None = None,
    constraints: PortfolioConstraintEvidence | None = None,
    execution: PortfolioExecutionEvidence | None = None,
) -> PortfolioAllocationSnapshot:
    """Explicit non-authoritative adapter for deterministic core tests."""

    if book is None and constraints is None and execution is None:
        return _compute_portfolio_allocation(candidates, as_of=as_of, cash_hurdle=cash_hurdle)
    return _compute_portfolio_allocation(
        candidates, as_of=as_of, cash_hurdle=cash_hurdle, book=book,
        constraints=constraints, execution=execution, connection=_TestConnection(),
    )


class _TestConnection:
    """Test-only evidence seam; production always passes a PostgreSQL cursor."""

    def execute(self, *_args: Any, **_kwargs: Any) -> "_TestConnection":
        return self

    def fetchone(self) -> dict[str, bool]:
        return {"verified": True}


def cash_only_allocation(as_of: datetime, cash_hurdle: float | None, reason: str) -> PortfolioAllocationSnapshot:
    """Return a typed safe state when PostgreSQL evidence cannot fund a trade."""

    missing = f"Required PostgreSQL evidence is missing or invalid: {reason}."
    explanation = {
        "why_trade": "No trade: the Phase 4 evidence gate is not satisfied.",
        "why_now": ["Remain in CASH until the missing evidence is persisted and validated."],
        "next_action": "Persist and validate the missing PostgreSQL evidence before considering a trade.",
        "expression": {"kind": "CASH"},
        "invalidation": {"kind": "data", "reason": reason},
        "missing_data": [missing],
    }
    payload = {"candidate_id": "CASH", "ticker": "CASH", "disposition": "selected",
               "target_weight": 1.0, "trace": {"gate": "fail_closed", "reason": reason, **explanation}}
    item = PortfolioAllocationItem(
        allocation_item_id=f"allocation-item:{_hash(payload)}", candidate_id="CASH", ticker="CASH",
        disposition="selected", target_weight=1, current_weight=0, marginal_book_utility=0,
        funding_source="CASH", trace=payload["trace"],
    )
    metadata = {"authority": "postgresql", "safe_state_reason": reason, **explanation}
    allocation_payload = {"as_of": as_of, "cash_hurdle": cash_hurdle, "items": (item,),
                          "forecast_ids": (), "action_ids": (), "strategy_registry_ids": (),
                          "metadata": metadata}
    return PortfolioAllocationSnapshot(
        allocation_id=f"allocation:{_hash(allocation_payload)}", as_of=as_of, input_cutoff=as_of,
        cash_hurdle=cash_hurdle, status="unavailable" if cash_hurdle is None else "cash_only",
        items=(item,), metadata=metadata,
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

    @model_validator(mode="after")
    def validate_artifact(self) -> "PortfolioScenarioArtifact":
        if self.input_cutoff.tzinfo is None or not self.scenarios or not self.model_version.strip() or not self.probability_semantics.strip():
            raise ValueError("scenario artifact requires a bounded input clock and paths")
        probabilities = [float(item.get("probability", -1)) for item in self.scenarios]
        if any(not isfinite(value) or value < 0 or value > 1 for value in probabilities):
            raise ValueError("scenario probabilities must be bounded")
        if abs(sum(probabilities) - 1.0) > 1e-9:
            raise ValueError("scenario probabilities must sum to one")
        for item in self.scenarios:
            returns = item.get("returns")
            shocks = item.get("shocks")
            if not isinstance(returns, Mapping) or not returns:
                raise ValueError("scenario paths require non-empty returns")
            if not isinstance(shocks, Mapping) or not shocks:
                raise ValueError("scenario paths require non-empty shocks")
            if dict(returns) == dict(shocks):
                raise ValueError("scenario shocks must be an independent persisted path")
            if set(returns) != set(shocks):
                raise ValueError("scenario returns and shocks must cover the same persisted names")
            if not any(float(returns[key]) != float(shocks[key]) for key in returns):
                raise ValueError("scenario shocks must differ from returns")
            for values in (returns, shocks):
                if any(not isfinite(float(value)) for value in values.values()):
                    raise ValueError("scenario values must be finite")
            if self.model_version == "strategy_pnl_tape.v1":
                provenance = item.get("provenance")
                if not isinstance(provenance, list) or not provenance or any(
                    not isinstance(source, Mapping)
                    or not source.get("strategy_pnl_tape_id")
                    or not source.get("input_hash")
                    for source in provenance
                ):
                    raise ValueError("persisted scenario paths require complete tape provenance")
        payload = {
            "allocation_id": self.allocation_id, "model_version": self.model_version,
            "probability_semantics": self.probability_semantics, "scenarios": self.scenarios,
            "tail_dependence": self.tail_dependence, "simultaneous_unwind": self.simultaneous_unwind,
            "input_cutoff": self.input_cutoff,
        }
        if self.scenario_artifact_id != f"scenario:{_hash(payload)}":
            raise ValueError("scenario identity does not match its immutable payload")
        co_exceedance = self.tail_dependence.get("negative_return_co_exceedance") or self.tail_dependence.get("co_exceedance")
        if not isinstance(co_exceedance, Mapping) or not co_exceedance:
            raise ValueError("scenario artifact requires tail co-exceedance results")
        if (
            not isfinite(float(self.simultaneous_unwind.get("probability", -1)))
            or not 0 <= float(self.simultaneous_unwind.get("probability", -1)) <= 1
            or not isinstance(self.simultaneous_unwind.get("observations"), int)
            or self.simultaneous_unwind["observations"] <= 0
        ):
            raise ValueError("scenario artifact requires simultaneous-unwind assumptions and results")
        if self.model_version == "strategy_pnl_tape.v1" and (
            self.tail_dependence.get("shock_threshold") is None
            or not isinstance(self.simultaneous_unwind.get("capacity_evidence"), Mapping)
            or not isinstance(self.simultaneous_unwind.get("execution_impact_evidence"), Mapping)
            or not isinstance(self.simultaneous_unwind.get("exit_time_evidence"), Mapping)
        ):
            raise ValueError("persisted scenario artifact requires tail and unwind evidence")
        return self


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
        if item.ticker == "CASH" or item.disposition not in {"selected", "rollback"}:
            continue
        if item.disposition == "rollback":
            score = drift_scores.get(item.allocation_item_id, rollback_threshold)
            payload = {"allocation_id": allocation.allocation_id, "allocation_item_id": item.allocation_item_id, "drift_score": score, "rollback_threshold": rollback_threshold, "proposed_weight": 0.0, "action": "rollback"}
            decisions.append(PortfolioDriftDecision(
                decision_id=f"drift:{_hash(payload)}", allocation_id=allocation.allocation_id,
                allocation_item_id=item.allocation_item_id, drift_score=score,
                rollback_threshold=rollback_threshold, proposed_weight=0.0, action="rollback",
            ))
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


def apply_decay_to_allocation(
    allocation: PortfolioAllocationSnapshot,
    drift_scores: Mapping[str, float],
    *,
    rollback_threshold: float,
) -> tuple[PortfolioAllocationSnapshot, tuple[PortfolioDriftDecision, ...]]:
    """Apply decay reductions before the final immutable allocation is stored."""

    initial = apply_decay_guard(allocation, drift_scores, rollback_threshold=rollback_threshold)
    by_item = {decision.allocation_item_id: decision for decision in initial}
    updated: list[PortfolioAllocationItem] = []
    released_weight = 0.0
    final_decisions: list[PortfolioDriftDecision] = []
    for item in allocation.items:
        decision = by_item.get(item.allocation_item_id)
        if decision is None or decision.action == "hold":
            updated.append(item)
            if decision is not None:
                final_decisions.append(decision)
            continue
        released_weight += max(0.0, item.target_weight - decision.proposed_weight)
        trace = {**item.trace, "drift_action": decision.action, "drift_score": decision.drift_score,
                 "pre_decay_target_weight": item.target_weight, "post_decay_target_weight": decision.proposed_weight}
        if decision.action == "rollback":
            trace["rollback_evidence"] = {
                "drift_score": decision.drift_score,
                "rollback_threshold": decision.rollback_threshold,
                "released_weight": max(0.0, item.target_weight - decision.proposed_weight),
            }
        new_item_id = f"allocation-item:{_hash({'candidate_id': item.candidate_id, 'ticker': item.ticker, 'disposition': item.disposition, 'target_weight': decision.proposed_weight, 'trace': trace})}"
        updated.append(item.model_copy(update={
            "target_weight": decision.proposed_weight,
            "disposition": "selected" if decision.proposed_weight > 0 else "rollback",
            "blockers": tuple((*item.blockers, f"drift_{decision.action}")),
            "allocation_item_id": new_item_id,
            "trace": trace,
        }))
        final_decisions.append(decision.model_copy(update={
            "allocation_item_id": new_item_id,
            "decision_id": f"drift:{_hash({'allocation_id': allocation.allocation_id, 'allocation_item_id': new_item_id, 'drift_score': decision.drift_score, 'rollback_threshold': decision.rollback_threshold, 'proposed_weight': decision.proposed_weight, 'action': decision.action})}",
        }))
    if released_weight > 0:
        cash_index = next((index for index, item in enumerate(updated) if item.ticker == "CASH"), None)
        if cash_index is not None:
            cash = updated[cash_index]
            cash_trace = {**cash.trace, "drift_released_to_cash": released_weight}
            cash_weight = min(1.0, cash.target_weight + released_weight)
            cash_item_id = f"allocation-item:{_hash({'candidate_id': cash.candidate_id, 'ticker': cash.ticker, 'disposition': cash.disposition, 'target_weight': cash_weight, 'trace': cash_trace})}"
            updated[cash_index] = cash.model_copy(update={
                "target_weight": cash_weight,
                "allocation_item_id": cash_item_id,
                "trace": cash_trace,
            })
    selected = [item for item in updated if item.disposition == "selected" and item.ticker != "CASH" and item.target_weight > 0]
    payload = {
        "as_of": allocation.as_of, "cash_hurdle": allocation.cash_hurdle, "items": updated,
        "forecast_ids": allocation.forecast_ids, "action_ids": allocation.action_ids,
        "strategy_registry_ids": allocation.strategy_registry_ids,
        "metadata": allocation.metadata,
    }
    adjusted = PortfolioAllocationSnapshot(
        allocation_id=allocation_id_for_snapshot(payload), as_of=allocation.as_of,
        input_cutoff=allocation.input_cutoff, cash_hurdle=allocation.cash_hurdle,
        status="available" if selected else "cash_only", items=tuple(updated),
        forecast_ids=allocation.forecast_ids, action_ids=allocation.action_ids,
        strategy_registry_ids=allocation.strategy_registry_ids,
        metadata=allocation.metadata,
    )
    persisted_decisions = tuple(
        decision.model_copy(update={
            "allocation_id": adjusted.allocation_id,
            "decision_id": f"drift:{_hash({'allocation_id': adjusted.allocation_id, 'allocation_item_id': decision.allocation_item_id, 'drift_score': decision.drift_score, 'rollback_threshold': decision.rollback_threshold, 'proposed_weight': decision.proposed_weight, 'action': decision.action})}",
        })
        for decision in final_decisions
    )
    return adjusted, persisted_decisions


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
        if not isinstance(item.get("returns"), Mapping) or not item.get("returns"):
            raise ValueError("scenario path requires non-empty returns")
        if not isinstance(item.get("shocks"), Mapping) or not item.get("shocks"):
            raise ValueError("scenario path requires non-empty shocks")
        if dict(item["returns"]) == dict(item["shocks"]):
            raise ValueError("scenario shocks must be an independent persisted path")
    payload = {"allocation_id": allocation.allocation_id, "model_version": model_version, "probability_semantics": probability_semantics, "scenarios": scenarios, "tail_dependence": tail_dependence, "simultaneous_unwind": simultaneous_unwind, "input_cutoff": allocation.input_cutoff}
    return PortfolioScenarioArtifact(
        scenario_artifact_id=f"scenario:{_hash(payload)}", allocation_id=allocation.allocation_id,
        model_version=model_version, probability_semantics=probability_semantics,
        scenarios=tuple(dict(item) for item in scenarios),
        tail_dependence=dict(tail_dependence), simultaneous_unwind=dict(simultaneous_unwind), input_cutoff=allocation.input_cutoff,
    )


def build_scenario_artifact_from_observations(
    allocation: PortfolioAllocationSnapshot,
    observations: list[Mapping[str, Any]],
) -> PortfolioScenarioArtifact:
    """Build portfolio paths only from persisted, point-in-time return rows."""

    selected = {item.ticker for item in allocation.items if item.disposition == "selected" and item.ticker != "CASH"}
    grouped: dict[str, dict[str, Any]] = {}
    for row in observations[:64]:
        outcome = row.get("outcome") if isinstance(row.get("outcome"), Mapping) else {}
        date_key = str(row.get("pnl_date") or row.get("date") or outcome.get("pnl_date") or row.get("observed_at") or "").strip()
        ticker = str(row.get("ticker") or "").upper()
        value = row.get("net_return", outcome.get("net_return"))
        shock = row.get("tail_return", outcome.get("tail_return"))
        if (not date_key or ticker not in selected or value is None or shock is None
                or not isfinite(float(value)) or not isfinite(float(shock))):
            continue
        grouped.setdefault(date_key, {"returns": {}, "shocks": {}, "provenance": []})["returns"][ticker] = float(value)
        grouped[date_key]["shocks"][ticker] = float(shock)
        grouped[date_key]["provenance"].append({
            "strategy_pnl_tape_id": row.get("strategy_pnl_tape_id") or row.get("id"),
            "strategy_forecast_id": row.get("strategy_forecast_id"),
            "pnl_date": row.get("pnl_date") or row.get("date"),
            "input_cutoff": row.get("input_cutoff"),
            "available_at": row.get("available_at"),
            "observed_at": row.get("observed_at"),
            "input_hash": row.get("input_hash"),
            "result_hash": row.get("result_hash"),
            "universe_manifest_hash": row.get("universe_manifest_hash"),
        })
    paths = [values for _, values in sorted(grouped.items())
             if selected <= values["returns"].keys() and selected <= values["shocks"].keys()
             and values["provenance"]]
    if not paths:
        raise ValueError("portfolio scenario requires persisted returns for every selected item")
    probability = 1.0 / len(paths)
    scenarios = [
        {"name": f"observed:{index}", "probability": probability, "returns": values["returns"], "shocks": values["shocks"], "provenance": values["provenance"]}
        for index, values in enumerate(paths)
    ]
    # Tail dependence is measured from the persisted tail-shock tape, not
    # from ordinary negative returns. The allocation trace carries the
    # capacity and execution evidence used by the unwind result.
    co_exceedance: dict[str, Any] = {}
    tail_threshold = -0.10
    for left in sorted(selected):
        for right in sorted(selected):
            if left > right:
                continue
            count = sum(values["shocks"][left] <= tail_threshold and values["shocks"][right] <= tail_threshold for values in paths)
            co_exceedance[f"{left}|{right}"] = {
                "count": count, "observations": len(paths), "probability": count / len(paths),
                "threshold": tail_threshold,
            }
    selected_items = {item.ticker: item for item in allocation.items if item.ticker in selected}
    capacity = {ticker: selected_items[ticker].trace.get("capacity") for ticker in sorted(selected)}
    execution_impact = {ticker: selected_items[ticker].trace.get("execution_penalty") for ticker in sorted(selected)}
    exit_time = {ticker: selected_items[ticker].trace.get("days_to_exit") for ticker in sorted(selected)}
    simultaneous = sum(all(values["shocks"][ticker] <= tail_threshold for ticker in selected) for values in paths)
    return build_scenario_artifact(
        allocation, scenarios, model_version="strategy_pnl_tape.v1",
        probability_semantics="equal-weight persisted P&L observations",
        tail_dependence={"negative_return_co_exceedance": co_exceedance, "shock_threshold": tail_threshold},
        simultaneous_unwind={
            "all_selected_negative_count": simultaneous, "observations": len(paths),
            "probability": simultaneous / len(paths), "capacity_evidence": capacity,
            "execution_impact_evidence": execution_impact, "exit_time_evidence": exit_time,
        },
    )


class PaperExecutionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    paper_execution_observation_id: str
    allocation_item_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    paper_order_id: str = Field(min_length=1)
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
    side: str = "buy"
    exit_price: float | None = None
    event_fee: float | None = None
    contract_multiplier: float | None = None
    observed_at: datetime
    available_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def enforce_paper(self) -> "PaperExecutionObservation":
        if self.execution_mode != "paper" or not self.paper_only or self.status not in {"planned", "submitted", "partial", "filled", "partial_exited", "exited", "cancelled", "unavailable"}:
            raise ValueError("execution telemetry is paper-only")
        if self.side not in {"buy", "sell"}:
            raise ValueError("execution side is invalid")
        if not self.allocation_item_id or not self.action_id:
            raise ValueError("paper execution requires allocation and action lineage")
        for name in ("requested_quantity", "filled_quantity", "requested_price", "fill_price", "spread_bps", "latency_ms", "impact_bps", "exit_price", "event_fee", "contract_multiplier"):
            _finite(getattr(self, name), name)
        if self.filled_quantity > self.requested_quantity or (self.filled_quantity and self.fill_price is None):
            raise ValueError("filled execution requires bounded quantity and fill price")
        if self.status in {"partial_exited", "exited"} and (self.filled_quantity <= 0 or self.exit_price is None):
            raise ValueError("exited execution requires a genuine fill and exit price")
        if self.observed_at.tzinfo is None or self.available_at.tzinfo is None:
            raise ValueError("execution observation clocks must be timezone-aware")
        if self.available_at.astimezone(UTC) < self.observed_at.astimezone(UTC):
            raise ValueError("execution observation availability cannot precede observation")
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
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_identity(self) -> "ExecutionModelSnapshot":
        if self.execution_model_snapshot_id != execution_model_id_for_snapshot(self):
            raise ValueError("execution model identity does not match its immutable payload")
        return self

    @model_validator(mode="after")
    def validate_calibration(self) -> "ExecutionModelSnapshot":
        if self.calibration_status not in {"calibrated", "calibration_pending", "unavailable"}:
            raise ValueError("invalid execution calibration status")
        if self.calibration_status == "calibrated" and (
            self.sample_count == 0
            or self.metadata.get("source") != "paper_execution_observation"
            or self.metadata.get("genuine_fill_count") != self.sample_count
            or len(self.metadata.get("paper_observation_ids") or ()) != self.sample_count
        ):
            raise ValueError("calibrated execution telemetry requires persisted genuine paper fills")
        for name in ("fill_probability", "spread_bps", "latency_ms", "impact_bps"):
            _finite(getattr(self, name), name)
        return self


def build_execution_model_snapshot(
    allocation_id: str | None,
    input_cutoff: datetime,
    observations: list[PaperExecutionObservation],
) -> ExecutionModelSnapshot:
    def metadata_timestamp(item: PaperExecutionObservation, name: str) -> datetime | None:
        value = item.metadata.get(name)
        if isinstance(value, datetime):
            return value if value.tzinfo is not None and value.utcoffset() is not None else None
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
        return None

    genuine = [
        item for item in observations
        if item.status in {"partial", "filled", "partial_exited", "exited"}
        and item.filled_quantity > 0 and item.fill_price is not None
        and item.available_at.astimezone(UTC) <= input_cutoff.astimezone(UTC)
        and item.observed_at.astimezone(UTC) < item.available_at.astimezone(UTC)
        and isinstance(item.metadata.get("paper_order_id"), str)
        and metadata_timestamp(item, "submitted_at") is not None
        and metadata_timestamp(item, "filled_at") is not None
    ]
    latency_values: list[float] = []
    for item in genuine:
        submitted_at = metadata_timestamp(item, "submitted_at")
        filled_at = metadata_timestamp(item, "filled_at")
        if submitted_at is None or filled_at is None:
            continue
        latency_values.append((filled_at.astimezone(UTC) - submitted_at.astimezone(UTC)).total_seconds() * 1000)
    impact_values = [
        abs(item.fill_price - item.requested_price) / item.requested_price * 10_000
        for item in genuine if item.fill_price is not None and item.requested_price not in (None, 0)
    ]
    payload = {
        "allocation_id": allocation_id, "input_cutoff": input_cutoff, "model_version": "paper-telemetry.v1",
        "calibration_status": "calibrated" if genuine else "calibration_pending", "sample_count": len(genuine),
        # The calibrated sample is the set of persisted genuine fills named in
        # the model metadata.  A pending observation must not create a caller-
        # supplied zero fill probability or otherwise calibrate the model.
        "fill_probability": 1.0 if genuine else None,
        "spread_bps": (sum(item.spread_bps for item in genuine if item.spread_bps is not None) / len([item for item in genuine if item.spread_bps is not None])) if any(item.spread_bps is not None for item in genuine) else None,
        "latency_ms": sum(latency_values) / len(latency_values) if latency_values else None,
        "impact_bps": sum(impact_values) / len(impact_values) if impact_values else None,
        "metadata": {
            "paper_observation_ids": sorted(item.paper_execution_observation_id for item in genuine),
            "genuine_fill_count": len(genuine),
            "source": "paper_execution_observation",
        },
    }
    return ExecutionModelSnapshot(
        execution_model_snapshot_id=f"execution:{_hash(payload)}", allocation_id=allocation_id,
        model_version="paper-telemetry.v1", calibration_status="calibrated" if genuine else "calibration_pending",
        sample_count=len(genuine), fill_probability=payload["fill_probability"], spread_bps=payload["spread_bps"],
        latency_ms=payload["latency_ms"], impact_bps=payload["impact_bps"], input_cutoff=input_cutoff,
        metadata=payload["metadata"],
    )


class BookAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    book_attribution_id: str
    allocation_id: str
    allocation_item_id: str
    strategy_forecast_id: str = Field(min_length=1)
    hypothesis_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    rank_id: str = Field(min_length=1)
    expression: dict[str, Any]
    experiment_id: str = Field(min_length=1)
    trial_id: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    paper_execution_observation_id: str = Field(min_length=1)
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
        if not all((self.hypothesis_id, self.experiment_id, self.trial_id, self.result_id, self.paper_execution_observation_id)):
            raise ValueError("book attribution requires complete hypothesis experiment trial result lineage")
        if not self.expression:
            raise ValueError("book attribution requires the persisted expression lineage")
        if self.pnl_status == "realized":
            required = {"hypothesis_id", "experiment_id", "trial_id", "result_id", "forecast_id", "action_id", "rank_id", "expression", "fill_id", "pnl", "cost_decomposition"}
            if not required.issubset(self.attribution):
                raise ValueError("realized attribution requires the canonical lineage and cost decomposition")
        if self.book_attribution_id != attribution_id_for_record(self):
            raise ValueError("attribution identity does not match its immutable payload")
        return self


def attribute_paper_pnl(
    allocation: PortfolioAllocationSnapshot,
    item: PortfolioAllocationItem,
    *,
    observation: PaperExecutionObservation | None = None,
    observations: list[PaperExecutionObservation] | None = None,
    realized_pnl: float | None = None,
) -> BookAttribution:
    if item.allocation_item_id not in {candidate.allocation_item_id for candidate in allocation.items}:
        raise ValueError("allocation item does not belong to allocation")
    if realized_pnl is not None:
        raise ValueError("realized P&L must be derived from the genuine paper observation")
    if observation is None:
        raise ValueError("attribution requires a genuine linked paper fill")
    if observation.allocation_item_id != item.allocation_item_id:
        raise ValueError("execution observation does not belong to allocation item")
    if observation.status != "exited" or observation.filled_quantity <= 0 or observation.fill_price is None or observation.exit_price is None:
        raise ValueError("attribution requires a genuine exited paper fill")
    events = observations or [observation]
    if any(event.allocation_item_id != item.allocation_item_id or event.paper_order_id != observation.paper_order_id
           or event.status not in {"partial_exited", "exited"} or event.filled_quantity <= 0
           or event.fill_price is None or event.exit_price is None for event in events):
        raise ValueError("attribution requires complete partial-exit telemetry")
    status = "realized"
    multipliers = [event.contract_multiplier if event.contract_multiplier is not None else event.metadata.get("contract_multiplier") for event in events]
    fees_by_event = [event.event_fee if event.event_fee is not None else event.metadata.get("fees") for event in events]
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(float(value)) or float(value) <= 0 for value in multipliers):
        raise ValueError("attribution requires a finite persisted contract multiplier")
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(float(value)) or float(value) < 0 for value in fees_by_event):
        raise ValueError("attribution requires finite persisted paper fees")
    gross_pnl = sum((1 if event.side == "buy" else -1) * (event.exit_price - event.fill_price) * event.filled_quantity * float(multiplier)
                    for event, multiplier in zip(events, multipliers))
    fees = sum(float(value) for value in fees_by_event)
    derived_pnl = gross_pnl - float(fees)
    trace = item.trace
    lineage = {
        "experiment_id": trace.get("experiment_id"),
        "trial_id": trace.get("trial_id"),
        "result_id": trace.get("result_id"),
    }
    if not item.hypothesis_id or any(not lineage[key] for key in lineage):
        raise ValueError("attribution requires persisted hypothesis, experiment, trial, and result lineage")
    expression = trace.get("expression")
    if not item.action_id or not item.rank_id or not isinstance(expression, dict) or not expression:
        raise ValueError("attribution requires persisted action, rank, and expression lineage")
    attribution = {
        "source": "paper_execution_observation", "derived": True,
        "hypothesis_id": item.hypothesis_id, **lineage,
        "forecast_id": item.strategy_forecast_id, "action_id": item.action_id,
        "rank_id": item.rank_id, "expression": trace.get("expression"),
        "invalidation": trace.get("invalidation"),
        "fill_id": observation.paper_execution_observation_id if observation else None,
        "pnl": {"gross": gross_pnl, "realized": derived_pnl if status == "realized" else None, "fees": float(fees), "net": derived_pnl,
                "quantity": sum(event.filled_quantity for event in events) if observation else None,
                "contract_multiplier": float(multipliers[-1]),
                "entry_price": observation.fill_price if observation else None, "exit_price": observation.exit_price if observation else None},
        "cost_decomposition": {"spread_bps": observation.spread_bps if observation else None,
                               "impact_bps": observation.impact_bps if observation else None, "fees": float(fees)},
    }
    record_payload = {
        "allocation_id": allocation.allocation_id, "allocation_item_id": item.allocation_item_id,
        "strategy_forecast_id": item.strategy_forecast_id, "hypothesis_id": item.hypothesis_id,
        "action_id": item.action_id, "rank_id": item.rank_id, "expression": expression,
        **lineage,
        "paper_execution_observation_id": observation.paper_execution_observation_id if observation else None,
        "pnl_status": status, "realized_pnl": derived_pnl,
        "attribution": attribution, "input_cutoff": allocation.input_cutoff,
    }
    return BookAttribution(
        book_attribution_id=attribution_id_for_record(record_payload), allocation_id=allocation.allocation_id,
        allocation_item_id=item.allocation_item_id, strategy_forecast_id=item.strategy_forecast_id,
        hypothesis_id=item.hypothesis_id, action_id=item.action_id, rank_id=item.rank_id,
        expression=expression, experiment_id=lineage["experiment_id"],
        trial_id=lineage["trial_id"], result_id=lineage["result_id"],
        paper_execution_observation_id=observation.paper_execution_observation_id,
        pnl_status=status, realized_pnl=derived_pnl,
        attribution=attribution, input_cutoff=allocation.input_cutoff,
    )
