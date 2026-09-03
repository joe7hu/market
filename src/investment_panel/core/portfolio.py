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


def _allocation_payload(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {
            "as_of": value["as_of"],
            "cash_hurdle": value["cash_hurdle"],
            "items": value["items"],
            "forecast_ids": value["forecast_ids"],
            "action_ids": value["action_ids"],
            "strategy_registry_ids": value["strategy_registry_ids"],
        }
    return {
        "as_of": value.as_of,
        "cash_hurdle": value.cash_hurdle,
        "items": value.items,
        "forecast_ids": value.forecast_ids,
        "action_ids": value.action_ids,
        "strategy_registry_ids": value.strategy_registry_ids,
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
        "paper_execution_observation_id": value.paper_execution_observation_id,
        "pnl_status": value.pnl_status,
        "realized_pnl": value.realized_pnl,
        "attribution": value.attribution,
        "input_cutoff": value.input_cutoff,
    }


def attribution_id_for_record(value: Mapping[str, Any] | Any) -> str:
    return f"attribution:{_hash(_attribution_payload(value))}"


class PortfolioCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    strategy_forecast_id: str | None = None
    action_id: str | None = None
    rank_id: str | None = None
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
    covariance: dict[str, float] | None = None
    expression: dict[str, Any] | None = None
    invalidation: dict[str, Any] | None = None
    missing_data: tuple[str, ...] = ()
    current_weight: float = Field(default=0, ge=0, le=1)
    cash_available: float | None = None
    cash_source_id: str | None = None
    trim_position_id: str | None = None
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
        if self.cash_available is not None and (not isfinite(self.cash_available) or self.cash_available < 0):
            raise ValueError("cash_available must be finite and non-negative")
        if self.input_cutoff and self.input_cutoff.tzinfo is None:
            raise ValueError("input_cutoff must be timezone-aware")
        if self.available_at and self.available_at.tzinfo is None:
            raise ValueError("available_at must be timezone-aware")
        if self.covariance is not None:
            if not self.covariance or any(not isfinite(float(value)) for value in self.covariance.values()):
                raise ValueError("covariance must contain finite values")
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
        if self.disposition == "selected" and self.ticker != "CASH" and not self.funding_source:
            raise ValueError("funded allocation requires a funding source")
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

    @model_validator(mode="after")
    def validate_identity(self) -> "PortfolioAllocationSnapshot":
        if self.as_of.tzinfo is None or self.input_cutoff.tzinfo is None:
            raise ValueError("allocation clocks must be timezone-aware")
        if self.as_of.astimezone(UTC) != self.input_cutoff.astimezone(UTC):
            raise ValueError("allocation as_of and input_cutoff must match")
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
    expression: dict[str, Any] | None = None
    invalidation: dict[str, Any] | None = None
    missing_data: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    target_weight: float
    current_weight: float
    marginal_book_utility: float
    funding_source: str | None = None
    sizing_trace: dict[str, Any]

    @model_validator(mode="after")
    def validate_funding(self) -> "PortfolioActionDTO":
        if self.disposition == "selected" and self.ticker != "CASH" and not self.funding_source:
            raise ValueError("canonical funded action requires funding")
        return self


class PortfolioIntegratedDTO(BaseModel):
    """One typed, immutable allocation view shared by all five workspaces."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allocation_id: str
    input_cutoff: datetime
    status: str
    actions: tuple[PortfolioActionDTO, ...]
    scenario_artifact_id: str | None = None
    execution_model_snapshot_id: str | None = None
    attribution_count: int = 0


def integrated_portfolio_dto(
    allocation: PortfolioAllocationSnapshot,
    *,
    scenario_artifact_id: str | None = None,
    execution_model_snapshot_id: str | None = None,
    attribution_count: int = 0,
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
            expression=trace.get("expression") if isinstance(trace.get("expression"), dict) else None,
            invalidation=trace.get("invalidation") if isinstance(trace.get("invalidation"), dict) else None,
            missing_data=tuple(str(value) for value in trace.get("missing_data", ()) or ()),
            blockers=item.blockers,
            target_weight=item.target_weight,
            current_weight=item.current_weight,
            marginal_book_utility=item.marginal_book_utility,
            funding_source=item.funding_source,
            sizing_trace=trace,
        ))
    return PortfolioIntegratedDTO(
        allocation_id=allocation.allocation_id,
        input_cutoff=allocation.input_cutoff,
        status=allocation.status,
        actions=tuple(actions),
        scenario_artifact_id=scenario_artifact_id,
        execution_model_snapshot_id=execution_model_snapshot_id,
        attribution_count=attribution_count,
    )


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
    cash_hurdle = float(cash_hurdle)
    normalized = [item if isinstance(item, PortfolioCandidate) else PortfolioCandidate.model_validate(item) for item in candidates]
    if len({item.candidate_id for item in normalized}) != len(normalized):
        raise ValueError("allocation candidates must have unique IDs")
    ranked: list[PortfolioAllocationItem] = []
    eligible: list[tuple[PortfolioCandidate, float, dict[str, Any]]] = []
    for candidate in normalized:
        blockers = list(candidate.blockers)
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
        if candidate.current_weight <= 0 and (
            candidate.cash_available is None or candidate.cash_available <= 0 or not candidate.cash_source_id
        ):
            blockers.append("cash_funding_missing")
        if candidate.current_weight > 0 and not candidate.trim_position_id:
            blockers.append("trim_position_missing")
        if blockers:
            ranked.append(_rejection(candidate, tuple(dict.fromkeys(blockers))))
            continue
        utility = candidate.expected_return - candidate.uncertainty - (candidate.overlap_penalty or 0) - (candidate.execution_penalty or 0)
        trace = {
            "input_cutoff": candidate.input_cutoff,
            "available_at": candidate.available_at,
            "strategy_forecast_id": candidate.strategy_forecast_id,
            "action_id": candidate.action_id,
            "rank_id": candidate.rank_id,
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
            "expression": candidate.expression,
            "invalidation": candidate.invalidation,
            "missing_data": list(candidate.missing_data),
            "cash_available": candidate.cash_available,
            "cash_source_id": candidate.cash_source_id,
            "trim_position_id": candidate.trim_position_id,
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
                blockers=("below_cash_hurdle",), trace=_json(trace),
            ))
            continue
        cap = min(candidate.risk_budget, candidate.kelly_cap, candidate.drawdown_cap, candidate.capacity, remaining)
        uncertainty_haircut = max(0.0, min(1.0, 1.0 - candidate.uncertainty / max(abs(candidate.expected_return), 1e-12)))
        target = max(0.0, cap / candidate.volatility * uncertainty_haircut)
        if target <= 0:
            ranked.append(_rejection(candidate, ("capacity_or_risk_cap_zero",)))
            continue
        target = min(target, remaining)
        trace["uncertainty_scale"] = (candidate.expected_return - candidate.uncertainty) / candidate.expected_return if candidate.expected_return else 0
        trace["uncertainty_haircut"] = uncertainty_haircut
        trace["uncertainty_scaled_weight"] = target
        trace["constraint_weight"] = cap
        payload = {"candidate_id": candidate.candidate_id, "ticker": candidate.ticker, "disposition": "selected", "target_weight": target, "trace": trace}
        ranked.append(PortfolioAllocationItem(
            allocation_item_id=f"allocation-item:{_hash(payload)}", candidate_id=candidate.candidate_id,
            ticker=candidate.ticker, strategy_forecast_id=candidate.strategy_forecast_id,
            action_id=candidate.action_id, hypothesis_id=candidate.hypothesis_id, disposition="selected",
            target_weight=target, current_weight=candidate.current_weight, marginal_book_utility=utility,
            funding_source=(f"CASH:{candidate.cash_source_id}" if candidate.current_weight == 0 else f"TRIM:{candidate.trim_position_id}"), trace=_json(trace),
        ))
        remaining -= target
    cash_payload = {"candidate_id": "CASH", "ticker": "CASH", "disposition": "selected", "target_weight": remaining}
    ranked.append(PortfolioAllocationItem(
        allocation_item_id=f"allocation-item:{_hash(cash_payload)}", candidate_id="CASH", ticker="CASH",
        disposition="selected", target_weight=remaining, current_weight=0, marginal_book_utility=0,
        funding_source="CASH", trace={"cash_hurdle": cash_hurdle},
    ))
    candidates_by_ticker = {candidate.ticker: candidate for candidate in normalized}
    proposed_weights = {item.ticker: item.target_weight for item in ranked if item.disposition == "selected" and item.ticker != "CASH"}
    current_weights = {candidate.ticker: candidate.current_weight for candidate in normalized}
    covariance = {ticker: candidate.covariance or {} for ticker, candidate in candidates_by_ticker.items()}

    def portfolio_volatility(weights: dict[str, float]) -> float:
        variance = sum(
            weight_i * weight_j * float(covariance.get(ticker_i, {}).get(ticker_j, 0.0))
            for ticker_i, weight_i in weights.items()
            for ticker_j, weight_j in weights.items()
        )
        return max(variance, 0.0) ** 0.5

    proposed_volatility = portfolio_volatility(proposed_weights)
    current_volatility = portfolio_volatility(current_weights)
    updated: list[PortfolioAllocationItem] = []
    for item in ranked:
        candidate = candidates_by_ticker.get(item.ticker)
        if candidate is None or item.ticker == "CASH":
            updated.append(item)
            continue
        proposed_mrc = (
            sum(weight * float(covariance[item.ticker].get(ticker, 0.0)) for ticker, weight in proposed_weights.items()) / proposed_volatility
            if proposed_volatility > 0 else None
        )
        current_mrc = (
            sum(weight * float(covariance[item.ticker].get(ticker, 0.0)) for ticker, weight in current_weights.items()) / current_volatility
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
    ranked = updated
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

    @model_validator(mode="after")
    def validate_artifact(self) -> "PortfolioScenarioArtifact":
        if self.input_cutoff.tzinfo is None or not self.scenarios:
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
            for values in (returns, shocks):
                if any(not isfinite(float(value)) for value in values.values()):
                    raise ValueError("scenario values must be finite")
        payload = {
            "allocation_id": self.allocation_id, "model_version": self.model_version,
            "probability_semantics": self.probability_semantics, "scenarios": self.scenarios,
            "tail_dependence": self.tail_dependence, "simultaneous_unwind": self.simultaneous_unwind,
        }
        if self.scenario_artifact_id != f"scenario:{_hash(payload)}":
            raise ValueError("scenario identity does not match its immutable payload")
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
        if not isinstance(item.get("returns"), Mapping) or not item.get("returns"):
            raise ValueError("scenario path requires non-empty returns")
        if not isinstance(item.get("shocks"), Mapping) or not item.get("shocks"):
            raise ValueError("scenario path requires non-empty shocks")
    payload = {"allocation_id": allocation.allocation_id, "model_version": model_version, "probability_semantics": probability_semantics, "scenarios": scenarios, "tail_dependence": tail_dependence, "simultaneous_unwind": simultaneous_unwind}
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
    grouped: dict[str, dict[str, float]] = {}
    for row in observations[:64]:
        date_key = str(row.get("pnl_date") or row.get("observed_at") or "").strip()
        ticker = str(row.get("ticker") or "").upper()
        value = row.get("net_return")
        if not date_key or ticker not in selected or value is None or not isfinite(float(value)):
            continue
        grouped.setdefault(date_key, {})[ticker] = float(value)
    paths = [returns for _, returns in sorted(grouped.items()) if selected <= returns.keys()]
    if not paths:
        raise ValueError("portfolio scenario requires persisted returns for every selected item")
    probability = 1.0 / len(paths)
    scenarios = [
        {"name": f"observed:{index}", "probability": probability, "returns": values, "shocks": dict(values)}
        for index, values in enumerate(paths)
    ]
    co_exceedance: dict[str, Any] = {}
    for left in sorted(selected):
        for right in sorted(selected):
            if left > right:
                continue
            count = sum(values[left] < 0 and values[right] < 0 for values in paths)
            co_exceedance[f"{left}|{right}"] = {"count": count, "observations": len(paths), "probability": count / len(paths)}
    simultaneous = sum(all(values[ticker] < 0 for ticker in selected) for values in paths)
    return build_scenario_artifact(
        allocation, scenarios, model_version="strategy_pnl_tape.v1",
        probability_semantics="equal-weight persisted P&L observations",
        tail_dependence={"negative_return_co_exceedance": co_exceedance},
        simultaneous_unwind={"all_selected_negative_count": simultaneous, "observations": len(paths), "probability": simultaneous / len(paths)},
    )


class PaperExecutionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    paper_execution_observation_id: str
    allocation_item_id: str | None = None
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
    observed_at: datetime
    available_at: datetime

    @model_validator(mode="after")
    def enforce_paper(self) -> "PaperExecutionObservation":
        if self.execution_mode != "paper" or not self.paper_only or self.status not in {"planned", "submitted", "partial", "filled", "exited", "cancelled", "unavailable"}:
            raise ValueError("execution telemetry is paper-only")
        if self.side not in {"buy", "sell"}:
            raise ValueError("execution side is invalid")
        for name in ("requested_quantity", "filled_quantity", "requested_price", "fill_price", "spread_bps", "latency_ms", "impact_bps", "exit_price"):
            _finite(getattr(self, name), name)
        if self.filled_quantity > self.requested_quantity or (self.filled_quantity and self.fill_price is None):
            raise ValueError("filled execution requires bounded quantity and fill price")
        if self.status == "exited" and (self.filled_quantity <= 0 or self.exit_price is None):
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

    @model_validator(mode="after")
    def validate_identity(self) -> "ExecutionModelSnapshot":
        if self.execution_model_snapshot_id != execution_model_id_for_snapshot(self):
            raise ValueError("execution model identity does not match its immutable payload")
        return self

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
        "allocation_id": allocation_id, "input_cutoff": input_cutoff, "model_version": "paper-telemetry.v1",
        "calibration_status": "calibrated" if genuine else "calibration_pending", "sample_count": len(genuine),
        "fill_probability": (sum(item.filled_quantity > 0 for item in observations) / len(observations)) if observations else None,
        "spread_bps": (sum(item.spread_bps for item in genuine if item.spread_bps is not None) / len([item for item in genuine if item.spread_bps is not None])) if any(item.spread_bps is not None for item in genuine) else None,
        "latency_ms": None, "impact_bps": None,
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
        if self.pnl_status == "realized" and not self.paper_execution_observation_id:
            raise ValueError("realized attribution requires an execution observation")
        if self.book_attribution_id != attribution_id_for_record(self):
            raise ValueError("attribution identity does not match its immutable payload")
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
    if realized_pnl is not None:
        raise ValueError("realized P&L must be derived from the genuine paper observation")
    if observation is None:
        status, derived_pnl = "pending_fill", None
    elif observation.allocation_item_id not in {None, item.allocation_item_id}:
        raise ValueError("execution observation does not belong to allocation item")
    elif observation.filled_quantity <= 0 or observation.fill_price is None or observation.exit_price is None:
        status, derived_pnl = "pending_fill", None
    else:
        direction = 1 if observation.side == "buy" else -1
        status = "realized"
        derived_pnl = direction * (observation.exit_price - observation.fill_price) * observation.filled_quantity
    attribution = {"source": "paper_execution_observation", "hypothesis_id": item.hypothesis_id, "derived": True}
    record_payload = {
        "allocation_id": allocation.allocation_id, "allocation_item_id": item.allocation_item_id,
        "strategy_forecast_id": item.strategy_forecast_id, "hypothesis_id": item.hypothesis_id,
        "paper_execution_observation_id": observation.paper_execution_observation_id if observation else None,
        "pnl_status": status, "realized_pnl": derived_pnl if status == "realized" else None,
        "attribution": attribution, "input_cutoff": allocation.input_cutoff,
    }
    return BookAttribution(
        book_attribution_id=attribution_id_for_record(record_payload), allocation_id=allocation.allocation_id,
        allocation_item_id=item.allocation_item_id, strategy_forecast_id=item.strategy_forecast_id,
        hypothesis_id=item.hypothesis_id, paper_execution_observation_id=observation.paper_execution_observation_id if observation else None,
        pnl_status=status, realized_pnl=derived_pnl if status == "realized" else None,
        attribution=attribution, input_cutoff=allocation.input_cutoff,
    )
