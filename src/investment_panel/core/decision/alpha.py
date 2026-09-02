"""Point-in-time ticker alpha and opportunity-rank contracts.

The module is pure: callers provide bounded rows and explicit portfolio
context. Missing evidence stays missing, so a research rank cannot become an
order authority by accident.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from math import isfinite
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investment_panel.core.decision.ticker import (
    AvailabilityStatus,
    InputLineage,
    MarketStateSnapshot,
    NumericRange,
    availability_status_for_blockers,
    trade_expression_identity,
)


INSTRUMENT_STATE_SNAPSHOT_CONTRACT_VERSION = "instrument-state-snapshot.v1"
ALPHA_SIGNAL_CONTRACT_VERSION = "alpha-signal.v1"
OPPORTUNITY_RANK_CONTRACT_VERSION = "opportunity-rank.v1"
TICKER_OPPORTUNITY_RANKING_VERSION = "ticker-opportunity-ranking.v1"
STRATEGY_FORECAST_CONTRACT_VERSION = "strategy-forecast.v1"


class EligibleUniverseSnapshot(BaseModel):
    """Point-in-time universe coverage used to gate trade ranks."""

    model_config = ConfigDict(extra="allow", frozen=True)

    intended: tuple[str, ...] = ()
    available: tuple[str, ...] = ()
    excluded_reasons: dict[str, str] = Field(default_factory=dict)
    excluded_materiality: dict[str, bool] = Field(default_factory=dict)
    source_failures: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    systemic_failure_reasons: tuple[str, ...] = ()
    coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    threshold: float = Field(default=1.0, ge=0.0, le=1.0)
    policy_version: str = "ticker-universe-coverage.v1"
    systemic_failure: bool = False

    @model_validator(mode="after")
    def validate_membership(self) -> "EligibleUniverseSnapshot":
        intended = set(self.intended)
        available = set(self.available)
        if not available <= intended:
            raise ValueError("eligible universe available symbols must be intended")
        if not set(self.excluded_reasons) <= intended - available:
            raise ValueError("eligible universe exclusions must be unavailable intended symbols")
        expected_ratio = len(available) / len(intended) if intended else 0.0
        if abs(self.coverage_ratio - expected_ratio) > 1e-9:
            raise ValueError("eligible universe coverage ratio must match membership")
        if set(self.excluded_reasons) != intended - available:
            raise ValueError("eligible universe must explain every excluded symbol")
        if not set(self.excluded_materiality) <= set(self.excluded_reasons):
            raise ValueError("eligible universe materiality must reference excluded symbols")
        if self.systemic_failure and not self.systemic_failure_reasons:
            raise ValueError("systemic universe failure requires reasons")
        return self


class InstrumentStateSnapshot(BaseModel):
    """One ticker's bounded point-in-time state, without neutral fallbacks."""

    model_config = ConfigDict(extra="allow", frozen=True)

    contract_version: str = INSTRUMENT_STATE_SNAPSHOT_CONTRACT_VERSION
    snapshot_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    as_of: datetime
    input_cutoff: datetime
    market_snapshot_id: str | None = None
    market_state_publication_id: str | None = None
    fundamental: dict[str, Any] | None = None
    technical: dict[str, Any] | None = None
    event: dict[str, Any] | None = None
    positioning: dict[str, Any] | None = None
    liquidity: dict[str, Any] | None = None
    valuation: dict[str, Any] | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)
    blockers: tuple[str, ...] = ()
    input_lineage: tuple[InputLineage, ...] = ()

    @model_validator(mode="after")
    def enforce_cutoff(self) -> "InstrumentStateSnapshot":
        if self.as_of.tzinfo is None or self.input_cutoff.tzinfo is None:
            raise ValueError("instrument state snapshot timestamps must be timezone-aware")
        if _utc(self.as_of) != _utc(self.input_cutoff):
            raise ValueError("instrument state snapshot as_of and input_cutoff must match")
        cutoff = _utc(self.input_cutoff)
        if any(_utc(item.available_at) > cutoff for item in self.input_lineage):
            raise ValueError("instrument state snapshot lineage cannot be newer than its cutoff")
        return self


class StrategyForecast(BaseModel):
    """The model-owned forecast authority consumed by an AlphaSignal."""

    model_config = ConfigDict(extra="allow", frozen=True)

    contract_version: str = STRATEGY_FORECAST_CONTRACT_VERSION
    strategy_forecast_id: str = Field(min_length=1)
    strategy_revision_id: int
    strategy_evaluation_id: str | None = None
    ticker: str = Field(min_length=1)
    opportunity_episode_id: str = Field(min_length=1)
    target: str = Field(min_length=1)
    horizon: str = Field(min_length=1)
    forecast_value: float | None = None
    forecast_range: NumericRange | None = None
    forecast_distribution: dict[str, float] | None = None
    probability_semantics: str | None = None
    model_artifact_id: str = Field(min_length=1)
    artifact_hash: str = Field(min_length=1)
    input_hash: str = Field(min_length=1)
    as_of: datetime
    input_cutoff: datetime
    generated_at: datetime
    available_at: datetime

    @model_validator(mode="after")
    def enforce_lineage(self) -> "StrategyForecast":
        timestamps = (self.as_of, self.input_cutoff, self.generated_at, self.available_at)
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            raise ValueError("strategy forecast timestamps must be timezone-aware")
        if _utc(self.as_of) != _utc(self.input_cutoff):
            raise ValueError("strategy forecast as_of and input_cutoff must match")
        if _utc(self.available_at) < _utc(self.generated_at):
            raise ValueError("strategy forecast availability cannot precede generation")
        # Actual wall-clock generation and availability are retained. Historical
        # readers and promotion queries apply the cutoff filter; backdating here
        # would hide future-information traps.
        for name, value in (("artifact_hash", self.artifact_hash), ("input_hash", self.input_hash)):
            if len(value) != 64 or value.lower() == "0" * 64 or any(char not in "0123456789abcdefABCDEF" for char in value):
                raise ValueError(f"strategy forecast {name} must be a non-zero SHA-256")
        if self.forecast_value is None and self.forecast_range is None and self.forecast_distribution is None:
            raise ValueError("strategy forecast must contain a value, range, or distribution")
        if self.strategy_forecast_id != strategy_forecast_id_for_payload(self.model_dump(mode="json", exclude={"strategy_forecast_id"})):
            raise ValueError("strategy forecast identity does not match its immutable payload")
        return self


def build_strategy_forecast(
    *, ticker: str, opportunity_episode_id: str, strategy_revision_id: int,
    strategy_evaluation_id: str | None, target: str, horizon: str,
    forecast_value: float | None = None, forecast_range: NumericRange | Mapping[str, Any] | None = None,
    forecast_distribution: Mapping[str, float] | None = None,
    probability_semantics: str | None = None, model_artifact_id: str,
    artifact_hash: str, input_hash: str, as_of: datetime, generated_at: datetime,
    available_at: datetime,
) -> StrategyForecast:
    """Build one content-addressed forecast from qualified model output."""

    range_value = forecast_range
    if isinstance(range_value, Mapping):
        range_value = NumericRange.model_validate(range_value)
    payload = {
        "contract_version": STRATEGY_FORECAST_CONTRACT_VERSION,
        "ticker": ticker.strip().upper(), "opportunity_episode_id": opportunity_episode_id,
        "strategy_revision_id": strategy_revision_id, "strategy_evaluation_id": strategy_evaluation_id,
        "target": target, "horizon": horizon, "forecast_value": forecast_value,
        "forecast_range": _jsonable(range_value), "forecast_distribution": dict(forecast_distribution or {}) or None,
        "probability_semantics": probability_semantics, "model_artifact_id": model_artifact_id,
        "artifact_hash": artifact_hash, "input_hash": input_hash, "as_of": _jsonable(as_of),
        "input_cutoff": _jsonable(as_of), "generated_at": _jsonable(generated_at),
        "available_at": _jsonable(available_at),
    }
    return StrategyForecast(
        strategy_forecast_id=strategy_forecast_id_for_payload(payload),
        ticker=payload["ticker"], opportunity_episode_id=opportunity_episode_id,
        strategy_revision_id=strategy_revision_id, strategy_evaluation_id=strategy_evaluation_id,
        target=target, horizon=horizon, forecast_value=forecast_value, forecast_range=range_value,
        forecast_distribution=dict(forecast_distribution or {}) or None,
        probability_semantics=probability_semantics, model_artifact_id=model_artifact_id,
        artifact_hash=artifact_hash, input_hash=input_hash, as_of=as_of, input_cutoff=as_of,
        generated_at=generated_at, available_at=available_at,
    )


class AlphaSignal(BaseModel):
    """A forecast with explicit target, horizon, calibration, and lineage."""

    model_config = ConfigDict(extra="allow", frozen=True)

    contract_version: str = ALPHA_SIGNAL_CONTRACT_VERSION
    signal_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    opportunity_episode_id: str = Field(min_length=1)
    decision_revision: str = Field(min_length=1)
    instrument_state_snapshot_id: str = Field(min_length=1)
    target: str | None = None
    horizon: str | None = None
    direction: str | None = None
    forecast_value: float | None = None
    forecast_range: NumericRange | None = None
    forecast_distribution: dict[str, float] | None = None
    probability_semantics: str | None = None
    cohort_id: str | None = None
    calibration_state: str | None = None
    availability_status: AvailabilityStatus = AvailabilityStatus.MISSING
    strategy_key: str | None = None
    strategy_revision_id: int | None = None
    strategy_forecast_id: str | None = None
    model_artifact_id: str | None = None
    artifact_hash: str | None = None
    input_hash: str | None = None
    strategy_evaluation_id: str | None = None
    artifact_published_at: datetime | None = None
    evaluation_evaluated_at: datetime | None = None
    evaluation_available_at: datetime | None = None
    model_version: str | None = None
    feature_version: str | None = None
    evaluation_stage: str | None = None
    oos_period_start: datetime | None = None
    oos_period_end: datetime | None = None
    cohort_path: tuple[str, ...] = ()
    fallback_parent: str | None = None
    effective_sample_size: int | None = None
    calibration_metrics: dict[str, float | int | None] = Field(default_factory=dict)
    research_score: float | None = None
    cost_model_version: str | None = None
    promotion_stage: str | None = None
    lower_confidence_net_utility_after_costs: float | None = None
    as_of: datetime
    input_cutoff: datetime
    input_lineage: tuple[InputLineage, ...] = ()
    blockers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def enforce_forecast_metadata(self) -> "AlphaSignal":
        if self.as_of.tzinfo is None or self.input_cutoff.tzinfo is None:
            raise ValueError("alpha signal timestamps must be timezone-aware")
        if _utc(self.as_of) != _utc(self.input_cutoff):
            raise ValueError("alpha signal as_of and input_cutoff must match")
        numerical = (
            self.forecast_value is not None
            or self.forecast_range is not None
            or self.forecast_distribution is not None
        )
        if numerical:
            missing = [
                name for name, value in (
                    ("target", self.target),
                    ("horizon", self.horizon),
                    ("cohort_id", self.cohort_id),
                    ("calibration_state", self.calibration_state),
                    ("model_version", self.model_version),
                ) if not str(value or "").strip()
            ]
            if missing:
                raise ValueError("numerical alpha forecasts require: " + ", ".join(missing))
        if self.availability_status is AvailabilityStatus.AVAILABLE:
            qualification_timestamps = (
                ("artifact_published_at", self.artifact_published_at),
                ("evaluation_evaluated_at", self.evaluation_evaluated_at),
                ("evaluation_available_at", self.evaluation_available_at),
            )
            missing_artifact = [
                name for name, value in (
                    ("strategy_key", self.strategy_key),
                    ("strategy_revision_id", self.strategy_revision_id),
                    ("model_artifact_id", self.model_artifact_id),
                    ("strategy_evaluation_id", self.strategy_evaluation_id),
                    *qualification_timestamps,
                ) if value in {None, ""}
            ]
            if missing_artifact:
                raise ValueError("available alpha signals require qualified artifact evidence: " + ", ".join(missing_artifact))
            phase2_missing = [
                name for name, value in (
                    ("oos_period_start", self.oos_period_start),
                    ("oos_period_end", self.oos_period_end),
                    ("cohort_path", self.cohort_path),
                    ("effective_sample_size", self.effective_sample_size),
                    ("calibration_metrics", self.calibration_metrics),
                    ("research_score", self.research_score),
                    ("cost_model_version", self.cost_model_version),
                    ("promotion_stage", self.promotion_stage),
                    ("lower_confidence_net_utility_after_costs", self.lower_confidence_net_utility_after_costs),
                ) if value is None or value == "" or value == () or value == {}
            ]
            if phase2_missing:
                raise ValueError("available alpha signals require Phase 2 evidence: " + ", ".join(phase2_missing))
            if self.effective_sample_size is not None and self.effective_sample_size <= 0:
                raise ValueError("available alpha effective sample size must be positive")
            if str(self.promotion_stage or "").lower() not in {"paper", "advisory"}:
                raise ValueError("available alpha promotion stage must be paper or advisory")
            oos_timestamps = (self.oos_period_start, self.oos_period_end)
            if any(value is None or value.tzinfo is None or value.utcoffset() is None for value in oos_timestamps):
                raise ValueError("available alpha OOS timestamps must be timezone-aware")
            if self.oos_period_start > self.oos_period_end or _utc(self.oos_period_end) > _utc(self.input_cutoff):
                raise ValueError("available alpha OOS interval must end at or before input_cutoff")
            if (
                self.lower_confidence_net_utility_after_costs is None
                or not isfinite(self.lower_confidence_net_utility_after_costs)
            ):
                raise ValueError("available alpha lower-confidence net utility must be finite")
            naive_timestamps = [
                name for name, value in qualification_timestamps
                if value is not None
                and (value.tzinfo is None or value.utcoffset() is None)
            ]
            if naive_timestamps:
                raise ValueError(
                    "available alpha qualification timestamps must be timezone-aware: "
                    + ", ".join(naive_timestamps)
                )
            cutoff = _utc(self.input_cutoff)
            future_timestamps = [
                name for name, value in qualification_timestamps
                if value is not None and _utc(value) > cutoff
            ]
            if future_timestamps:
                raise ValueError(
                    "available alpha qualification timestamps cannot be newer than input_cutoff: "
                    + ", ".join(future_timestamps)
                )
            if not self.strategy_forecast_id:
                raise ValueError("available alpha qualification timestamps require a persisted strategy forecast: strategy_forecast_id")
            if self.blockers:
                raise ValueError("available alpha signals cannot have blockers")
        elif numerical and not self.blockers:
            raise ValueError("unavailable numerical alpha signals require blockers")
        if self.forecast_distribution is not None:
            if not self.probability_semantics:
                raise ValueError("alpha probability distributions require probability semantics")
            values = tuple(float(value) for value in self.forecast_distribution.values())
            if any(not isfinite(value) or value < 0 for value in values):
                raise ValueError("alpha probability distribution must be finite and non-negative")
            if not values or abs(sum(values) - 1.0) > 1e-6:
                raise ValueError("alpha probability distribution must total one")
        cutoff = _utc(self.input_cutoff)
        if any(_utc(item.available_at) > cutoff for item in self.input_lineage):
            raise ValueError("alpha signal lineage cannot be newer than its cutoff")
        return self


class TradeUtility(BaseModel):
    """Explicit utility inputs and the one calculated trade utility."""

    model_config = ConfigDict(extra="allow", frozen=True)

    lower_confidence_expected_gross_pnl: float | None = None
    expected_transaction_costs: float | None = None
    lower_confidence_expected_net_pnl: float | None = None
    tail_risk_penalty: float | None = None
    portfolio_overlap_penalty: float | None = None
    diversification_benefit: float | None = None
    capital_at_risk: float | None = None
    trade_utility: float | None = None


class OpportunityRank(BaseModel):
    """Book-level research and trade rank for one ticker opportunity."""

    model_config = ConfigDict(extra="allow", frozen=True)

    contract_version: str = OPPORTUNITY_RANK_CONTRACT_VERSION
    rank_id: str = Field(min_length=1)
    ranking_version: str = TICKER_OPPORTUNITY_RANKING_VERSION
    ticker: str = Field(min_length=1)
    opportunity_episode_id: str = Field(min_length=1)
    decision_revision: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    selected_expression_identity: str | None = None
    selected_expression_kind: str | None = None
    portfolio_impact_id: str | None = None
    risk_policy_version: str | None = None
    alpha_signal_id: str | None = None
    strategy_forecast_id: str | None = None
    instrument_state_snapshot_id: str | None = None
    market_snapshot_id: str | None = None
    market_state_publication_id: str | None = None
    cutoff: datetime
    input_cutoff: datetime
    input_lineage: tuple[InputLineage, ...] = ()
    research_priority_score: float | None = None
    research_rank: int | None = None
    trade_rank: int | None = None
    trade_rank_unavailable_reason: str | None = None
    availability_status: AvailabilityStatus = AvailabilityStatus.MISSING
    primary_blocker: str | None = None
    utility: TradeUtility = Field(default_factory=TradeUtility)
    lower_confidence_expected_net_pnl: float | None = None
    trade_utility: float | None = None
    evaluated_universe_complete: bool = False
    ranking_universe_incomplete: bool = True
    eligible_universe: EligibleUniverseSnapshot | None = None
    blockers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def enforce_cutoff(self) -> "OpportunityRank":
        if self.cutoff.tzinfo is None or self.input_cutoff.tzinfo is None:
            raise ValueError("opportunity rank timestamps must be timezone-aware")
        if _utc(self.cutoff) != _utc(self.input_cutoff):
            raise ValueError("opportunity rank cutoff and input_cutoff must match")
        if self.trade_rank is not None and self.trade_rank < 1:
            raise ValueError("trade rank must be positive")
        return self


def build_instrument_state_snapshot(
    ticker: str,
    tables: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
    *,
    as_of: datetime,
    input_cutoff: datetime | None = None,
    market_snapshot: MarketStateSnapshot | Mapping[str, Any] | None = None,
    market_snapshot_id: str | None = None,
    market_state_publication_id: str | None = None,
    input_lineage: Iterable[InputLineage] = (),
) -> InstrumentStateSnapshot:
    """Build one snapshot from already bounded rows; missing dimensions stay null."""

    reference = _utc(input_cutoff or as_of)
    source_rows = tables or {}
    dimensions = {
        "fundamental": ("fundamentals",),
        "technical": ("technicals",),
        "event": ("earnings", "catalysts"),
        "positioning": ("ownership_consensus", "short_interest"),
        "liquidity": ("liquidity",),
        "valuation": ("valuations",),
    }
    values: dict[str, dict[str, Any] | None] = {}
    coverage: dict[str, Any] = {}
    blockers: list[str] = []
    symbol = ticker.strip().upper()
    for dimension, names in dimensions.items():
        row = _latest_row(source_rows, names, symbol, reference)
        values[dimension] = _jsonable(row) if row is not None else None
        coverage[dimension] = {
            "status": "available" if row is not None else "unavailable",
            "source_id": str((row or {}).get("source") or (row or {}).get("source_id") or "") or None,
            "source_version": str((row or {}).get("source_version") or "") or None,
            "available_at": _iso((row or {}).get("available_at")) if row is not None else None,
        }
        if row is None:
            blockers.append(f"{dimension}_state_unavailable")
    market = _model_dump(market_snapshot)
    snapshot_id_value = market_snapshot_id or str(market.get("snapshot_id") or "") or None
    publication_id = market_state_publication_id or str(market.get("publication_id") or "") or None
    lineage = tuple(input_lineage)
    identity_payload = {
        "ticker": symbol,
        "as_of": reference.isoformat(),
        "market_snapshot_id": snapshot_id_value,
        "market_state_publication_id": publication_id,
        "dimensions": values,
        "coverage": coverage,
        "blockers": blockers,
        "input_lineage": [_jsonable(item) for item in lineage],
    }
    return InstrumentStateSnapshot(
        snapshot_id=_content_id("instrument-state", identity_payload),
        ticker=symbol,
        as_of=reference,
        input_cutoff=reference,
        market_snapshot_id=snapshot_id_value,
        market_state_publication_id=publication_id,
        **values,
        coverage=coverage,
        blockers=tuple(blockers),
        input_lineage=lineage,
    )


def build_alpha_signal(
    *,
    ticker: str,
    opportunity_episode_id: str,
    decision_revision: str,
    instrument_state_snapshot_id: str,
    as_of: datetime,
    input_lineage: Iterable[InputLineage] = (),
    target: str | None = None,
    horizon: str | None = None,
    direction: str | None = None,
    forecast_value: float | None = None,
    forecast_range: NumericRange | Mapping[str, Any] | None = None,
    forecast_distribution: Mapping[str, float] | None = None,
    probability_semantics: str | None = None,
    cohort_id: str | None = None,
    calibration_state: str | None = None,
    model_version: str | None = None,
    feature_version: str | None = None,
    evaluation_stage: str | None = None,
    oos_period_start: datetime | None = None,
    oos_period_end: datetime | None = None,
    cohort_path: Iterable[str] = (),
    fallback_parent: str | None = None,
    effective_sample_size: int | None = None,
    calibration_metrics: Mapping[str, float | int | None] | None = None,
    research_score: float | None = None,
    cost_model_version: str | None = None,
    promotion_stage: str | None = None,
    lower_confidence_net_utility_after_costs: float | None = None,
    availability_status: AvailabilityStatus | str = AvailabilityStatus.MISSING,
    strategy_key: str | None = None,
    strategy_revision_id: int | None = None,
    strategy_forecast_id: str | None = None,
    model_artifact_id: str | None = None,
    artifact_hash: str | None = None,
    input_hash: str | None = None,
    strategy_evaluation_id: str | None = None,
    artifact_published_at: datetime | None = None,
    evaluation_evaluated_at: datetime | None = None,
    evaluation_available_at: datetime | None = None,
    blockers: Iterable[str] = (),
) -> AlphaSignal:
    reference = _utc(as_of)
    lineage = tuple(input_lineage)
    range_value = forecast_range
    if isinstance(range_value, Mapping):
        range_value = NumericRange.model_validate(range_value)
    values = {
        "ticker": ticker.strip().upper(),
        "opportunity_episode_id": opportunity_episode_id,
        "decision_revision": decision_revision,
        "instrument_state_snapshot_id": instrument_state_snapshot_id,
        "target": target,
        "horizon": horizon,
        "direction": direction,
        "forecast_value": forecast_value,
        "forecast_range": _jsonable(range_value),
        "forecast_distribution": dict(forecast_distribution or {}) or None,
        "probability_semantics": probability_semantics,
        "cohort_id": cohort_id,
        "calibration_state": calibration_state,
        "availability_status": availability_status,
        "strategy_key": strategy_key,
        "strategy_revision_id": strategy_revision_id,
        "strategy_forecast_id": strategy_forecast_id,
        "model_artifact_id": model_artifact_id,
        "artifact_hash": artifact_hash,
        "input_hash": input_hash,
        "strategy_evaluation_id": strategy_evaluation_id,
        "artifact_published_at": artifact_published_at,
        "evaluation_evaluated_at": evaluation_evaluated_at,
        "evaluation_available_at": evaluation_available_at,
        "model_version": model_version,
        "feature_version": feature_version,
        "evaluation_stage": evaluation_stage,
        "oos_period_start": oos_period_start,
        "oos_period_end": oos_period_end,
        "cohort_path": list(cohort_path),
        "fallback_parent": fallback_parent,
        "effective_sample_size": effective_sample_size,
        "calibration_metrics": dict(calibration_metrics or {}),
        "research_score": research_score,
        "cost_model_version": cost_model_version,
        "promotion_stage": promotion_stage,
        "lower_confidence_net_utility_after_costs": lower_confidence_net_utility_after_costs,
        "as_of": reference.isoformat(),
        "input_cutoff": reference.isoformat(),
        "input_lineage": [_jsonable(item) for item in lineage],
        "blockers": list(blockers),
    }
    # A scalar/range in an unlinked legacy payload is not a forecast authority.
    # Callers must pass the ID of a persisted StrategyForecast.
    return AlphaSignal(
        signal_id=_content_id("alpha-signal", values),
        as_of=reference,
        input_cutoff=reference,
        input_lineage=lineage,
        **{key: value for key, value in values.items() if key not in {"as_of", "input_cutoff", "input_lineage"}},
    )


def calculate_trade_utility(
    *,
    lower_confidence_expected_gross_pnl: float | None = None,
    expected_transaction_costs: float | None = None,
    lower_confidence_expected_net_pnl: float | None = None,
    tail_risk_penalty: float | None = None,
    portfolio_overlap_penalty: float | None = None,
    diversification_benefit: float | None = None,
    capital_at_risk: float | None = None,
) -> TradeUtility:
    """Calculate utility once; a supplied net value is never costed twice."""

    gross = _finite(lower_confidence_expected_gross_pnl)
    costs = _finite(expected_transaction_costs)
    net = _finite(lower_confidence_expected_net_pnl)
    if gross is not None and costs is not None:
        calculated_net = gross - costs
        if net is not None and abs(net - calculated_net) > 1e-8:
            raise ValueError("lower-confidence net P&L does not match gross P&L less transaction costs")
        net = calculated_net
    tail = _finite(tail_risk_penalty)
    overlap = _finite(portfolio_overlap_penalty)
    diversification = _finite(diversification_benefit)
    capital = _finite(capital_at_risk)
    if any(value is not None and value < 0 for value in (costs, tail, overlap)):
        raise ValueError("transaction costs and risk penalties cannot be negative")
    utility = None
    if all(value is not None for value in (net, tail, overlap, diversification)) and capital is not None and capital > 0:
        utility = (net - tail - overlap + diversification) / capital
    return TradeUtility(
        lower_confidence_expected_gross_pnl=gross,
        expected_transaction_costs=costs,
        lower_confidence_expected_net_pnl=net,
        tail_risk_penalty=tail,
        portfolio_overlap_penalty=overlap,
        diversification_benefit=diversification,
        capital_at_risk=capital,
        trade_utility=utility,
    )


def rank_opportunities(
    candidates: Iterable[Mapping[str, Any]],
    *,
    evaluated_universe_complete: bool | None = None,
    eligible_universe: EligibleUniverseSnapshot | Mapping[str, Any] | None = None,
    ranking_version: str = TICKER_OPPORTUNITY_RANKING_VERSION,
) -> list[OpportunityRank]:
    """Return dense research ranks and fail-closed book trade ranks."""

    raw_rows = [dict(candidate) for candidate in candidates]
    universe = (
        EligibleUniverseSnapshot.model_validate(eligible_universe)
        if eligible_universe is not None
        else None
    )
    snapshot_complete = (
        universe is not None
        and not universe.systemic_failure
        and universe.coverage_ratio >= universe.threshold
    )
    universe_complete = snapshot_complete if universe is not None else bool(evaluated_universe_complete)
    rows: list[dict[str, Any]] = []
    for candidate in raw_rows:
        utility_inputs = dict(candidate.get("utility") or {})
        for name in (
            "lower_confidence_expected_gross_pnl", "expected_transaction_costs",
            "lower_confidence_expected_net_pnl", "tail_risk_penalty",
            "portfolio_overlap_penalty", "diversification_benefit", "capital_at_risk",
        ):
            if name not in utility_inputs and name in candidate:
                utility_inputs[name] = candidate[name]
        utility = calculate_trade_utility(**utility_inputs)
        reason = _unavailable_reason(candidate, utility, universe_complete)
        blockers = tuple(dict.fromkeys((
            *(str(item) for item in candidate.get("blockers") or () if str(item).strip()),
            *(str(item) for item in _model_dump(candidate.get("alpha_signal")).get("blockers") or () if str(item).strip()),
            *((reason,) if reason else ()),
        )))
        row = {
            **candidate,
            "utility": utility,
            "trade_rank_unavailable_reason": reason,
            "availability_status": availability_status_for_blockers(
                (reason,) if reason else (), available_when_empty=True,
            ),
            "primary_blocker": reason,
            "blockers": blockers,
            "evaluated_universe_complete": universe_complete,
            "ranking_universe_incomplete": not universe_complete,
            "eligible_universe": universe.model_dump(mode="json") if universe else None,
            "research_priority_score": _research_score(candidate),
            "ranking_version": ranking_version,
        }
        rows.append(row)

    ordered_research = sorted(rows, key=_research_key)
    for rank, row in enumerate(ordered_research, start=1):
        row["research_rank"] = rank
    eligible = [row for row in rows if row["trade_rank_unavailable_reason"] is None]
    eligible.sort(key=lambda row: (
        -(row["utility"].trade_utility or 0.0),
        int(row.get("research_rank") or 0),
        str(row.get("ticker") or ""),
        str(row.get("opportunity_episode_id") or ""),
        str(row.get("selected_expression_identity") or ""),
    ))
    for rank, row in enumerate(eligible, start=1):
        row["trade_rank"] = rank
    output: list[OpportunityRank] = []
    for row in rows:
        utility: TradeUtility = row["utility"]
        payload = {
            "ranking_version": ranking_version,
            "ticker": row.get("ticker"),
            "opportunity_episode_id": row.get("opportunity_episode_id"),
            "decision_revision": row.get("decision_revision"),
            "policy_version": row.get("policy_version") or row.get("risk_policy_version"),
            "selected_expression_identity": row.get("selected_expression_identity"),
            "selected_expression_kind": row.get("selected_expression_kind"),
            "portfolio_impact_id": row.get("portfolio_impact_id"),
            "risk_policy_version": row.get("risk_policy_version") or row.get("policy_version"),
            "alpha_signal_id": row.get("alpha_signal_id"),
            "strategy_forecast_id": row.get("strategy_forecast_id")
            or _model_dump(row.get("alpha_signal")).get("strategy_forecast_id"),
            "instrument_state_snapshot_id": row.get("instrument_state_snapshot_id"),
            "market_snapshot_id": row.get("market_snapshot_id"),
            "market_state_publication_id": row.get("market_state_publication_id"),
            "cutoff": _jsonable(row.get("cutoff")),
            "input_lineage": _jsonable(row.get("input_lineage") or ()),
            "research_priority_score": row.get("research_priority_score"),
            "research_rank": row.get("research_rank"),
            "trade_rank": row.get("trade_rank"),
            "trade_rank_unavailable_reason": row.get("trade_rank_unavailable_reason"),
            "availability_status": row.get("availability_status"),
            "primary_blocker": row.get("primary_blocker"),
            "utility": utility.model_dump(mode="json"),
            "evaluated_universe_complete": row["evaluated_universe_complete"],
            "ranking_universe_incomplete": row["ranking_universe_incomplete"],
            "eligible_universe": row.get("eligible_universe"),
            "blockers": list(row.get("blockers") or ()),
        }
        output.append(OpportunityRank(
            rank_id=_content_id("opportunity-rank", payload),
            input_cutoff=_timestamp(row["cutoff"]),
            lower_confidence_expected_net_pnl=utility.lower_confidence_expected_net_pnl,
            trade_utility=utility.trade_utility,
            **payload,
        ))
    return sorted(output, key=lambda row: (row.research_rank or 0, row.ticker, row.opportunity_episode_id))


def _unavailable_reason(candidate: Mapping[str, Any], utility: TradeUtility, universe_complete: bool) -> str | None:
    if not universe_complete:
        return "ranking_universe_incomplete"
    kind = str(candidate.get("selected_expression_kind") or "").upper()
    if kind == "CASH":
        return "cash_comparator"
    signal = _model_dump(candidate.get("alpha_signal"))
    if signal.get("availability_status") != AvailabilityStatus.AVAILABLE.value:
        blockers = [str(item) for item in signal.get("blockers") or () if str(item).strip()]
        return blockers[0] if blockers else "alpha_signal_unavailable"
    if not _signal_metadata_complete(signal):
        return "alpha_signal_metadata_incomplete"
    calibration = str(signal.get("calibration_state") or candidate.get("calibration_state") or "").lower()
    stage = str(signal.get("evaluation_stage") or candidate.get("evaluation_stage") or "").lower()
    qualified_calibration = "calibrat" in calibration and bool(signal.get("cohort_path"))
    out_of_sample = stage.replace("-", "_").replace(" ", "_") in {
        "out_of_sample",
        "out_of_sample_evaluation",
        "oos",
    }
    if not qualified_calibration or not out_of_sample:
        return "calibration_not_exact_out_of_sample"
    artifact_utility = _finite(signal.get("lower_confidence_net_utility_after_costs"))
    if artifact_utility is None or artifact_utility <= 0:
        return "alpha_lower_confidence_net_utility_not_positive"
    if utility.lower_confidence_expected_gross_pnl is None or utility.expected_transaction_costs is None:
        return "transaction_cost_model_missing"
    if candidate.get("requires_execution_grade_paper_evidence") and not candidate.get("walk_forward_paper_evidence"):
        return "paper_evidence_missing"
    if utility.lower_confidence_expected_net_pnl is None or utility.lower_confidence_expected_net_pnl <= 0:
        return "lower_confidence_expected_net_pnl_not_positive"
    if any(value is None for value in (
        utility.tail_risk_penalty,
        utility.portfolio_overlap_penalty,
        utility.diversification_benefit,
        utility.capital_at_risk,
    )) or utility.capital_at_risk <= 0:
        return "utility_input_missing"
    impact = _model_dump(candidate.get("portfolio_impact"))
    if not impact or impact.get("availability") != "available" or impact.get("blockers"):
        return "portfolio_impact_missing_or_stale"
    policy = _model_dump(candidate.get("risk_policy_snapshot"))
    if not policy or policy.get("blockers"):
        return "risk_policy_snapshot_missing_or_stale"
    if candidate.get("execution_feasible") is not True:
        return "execution_feasibility_incomplete"
    if not str(candidate.get("selected_expression_identity") or "").strip():
        return "selected_expression_identity_incomplete"
    if not _lineage_matches(candidate, impact, policy, signal):
        return "publication_lineage_mismatch"
    if utility.trade_utility is None or not isfinite(utility.trade_utility) or utility.trade_utility <= 0:
        return "trade_utility_not_positive"
    return None


def _signal_metadata_complete(signal: Mapping[str, Any]) -> bool:
    if not signal:
        return False
    if signal.get("contract_version") == ALPHA_SIGNAL_CONTRACT_VERSION and not str(signal.get("strategy_forecast_id") or "").strip():
        return False
    numerical = any(signal.get(key) is not None for key in ("forecast_value", "forecast_range", "forecast_distribution"))
    if not numerical:
        return False
    scalar = (
        "target", "horizon", "cohort_id", "calibration_state", "model_version",
        "feature_version", "oos_period_start", "oos_period_end", "cost_model_version",
        "promotion_stage",
    )
    if not all(str(signal.get(key) or "").strip() for key in scalar):
        return False
    if not signal.get("cohort_path") or not signal.get("calibration_metrics"):
        return False
    return (
        _finite(signal.get("effective_sample_size")) is not None
        and float(signal["effective_sample_size"]) > 0
        and _finite(signal.get("research_score")) is not None
        and _finite(signal.get("lower_confidence_net_utility_after_costs")) is not None
    )


def _lineage_matches(
    candidate: Mapping[str, Any],
    impact: Mapping[str, Any],
    policy: Mapping[str, Any],
    signal: Mapping[str, Any],
) -> bool:
    pairs = [
        (candidate.get("ticker"), signal.get("ticker")),
        (candidate.get("opportunity_episode_id"), signal.get("opportunity_episode_id")),
        (candidate.get("decision_revision"), signal.get("decision_revision")),
        (candidate.get("instrument_state_snapshot_id"), signal.get("instrument_state_snapshot_id")),
        (candidate.get("alpha_signal_id"), signal.get("signal_id")),
        (candidate.get("opportunity_episode_id"), impact.get("opportunity_episode_id")),
        (candidate.get("decision_revision"), impact.get("decision_revision")),
        (candidate.get("market_snapshot_id"), impact.get("market_snapshot_id")),
        (candidate.get("market_state_publication_id"), impact.get("market_state_publication_id")),
        (candidate.get("portfolio_impact_id"), impact.get("impact_id")),
        (candidate.get("policy_version"), policy.get("policy_version")),
        (candidate.get("policy_version"), impact.get("risk_policy_version")),
        (candidate.get("risk_policy_version"), policy.get("policy_version")),
        (candidate.get("risk_policy_version"), impact.get("risk_policy_version")),
    ]
    forecast_pair = (
        candidate.get("strategy_forecast_id") or signal.get("strategy_forecast_id"),
        signal.get("strategy_forecast_id"),
    )
    if any(forecast_pair):
        pairs.append(forecast_pair)
    if not all(_identity_equal(left, right) for left, right in pairs):
        return False

    for value in (candidate.get("cutoff"), signal.get("as_of"), signal.get("input_cutoff"), impact.get("cutoff")):
        if not _identity_present(value):
            return False
    if not _timestamps_equal(candidate["cutoff"], signal["as_of"], signal["input_cutoff"], impact["cutoff"]):
        return False
    if not _identity_equal(candidate.get("input_lineage"), signal.get("input_lineage")):
        return False
    if not _identity_equal(candidate.get("input_lineage"), impact.get("input_lineage")):
        return False

    expression = _model_dump(candidate.get("expression"))
    if not expression:
        return False
    if not _identity_equal(candidate.get("ticker"), expression.get("ticker")):
        return False
    if not _identity_equal(candidate.get("selected_expression_kind"), expression.get("kind")):
        return False
    if not _identity_equal(candidate.get("selected_expression_kind"), impact.get("expression_kind")):
        return False
    if not _identity_equal(candidate.get("selected_expression_identity"), impact.get("expression_identity")):
        return False
    try:
        return _identity_equal(candidate.get("selected_expression_identity"), trade_expression_identity(expression))
    except (TypeError, ValueError, KeyError):
        return False


def _research_score(candidate: Mapping[str, Any]) -> float | None:
    signal = _model_dump(candidate.get("alpha_signal"))
    return _finite(signal.get("research_score"))


def _research_key(row: Mapping[str, Any]) -> tuple[int, float, str, str, str]:
    score = _finite(row.get("research_priority_score"))
    return (
        0 if score is not None else 1,
        -(score or 0.0),
        str(row.get("ticker") or ""),
        str(row.get("opportunity_episode_id") or ""),
        str(row.get("selected_expression_identity") or ""),
    )


def _latest_row(
    tables: Mapping[str, Iterable[Mapping[str, Any]]],
    names: Iterable[str],
    ticker: str,
    cutoff: datetime,
) -> dict[str, Any] | None:
    reference = _aware_timestamp(cutoff)
    rows: list[tuple[datetime, tuple[str, str], dict[str, Any]]] = []
    for name in names:
        for raw in tables.get(name) or ():
            row = dict(raw)
            row_ticker = str(row.get("ticker") or row.get("symbol") or row.get("underlying") or "").strip().upper()
            if row_ticker and row_ticker != ticker:
                continue
            try:
                available_at = _aware_timestamp(row["available_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if available_at <= reference:
                rows.append((available_at, _row_identity(row), row))
    return max(rows, key=lambda item: (item[0], item[1]), default=None)[2] if rows else None


def _model_dump(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value) if isinstance(value, Mapping) else {}


def _content_id(prefix: str, value: Mapping[str, Any]) -> str:
    encoded = json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}:{hashlib.sha256(encoded.encode()).hexdigest()[:32]}"


def strategy_forecast_id_for_payload(value: Mapping[str, Any]) -> str:
    """Return the content address for the complete immutable forecast payload."""
    normalized = dict(value)
    normalized.pop("strategy_forecast_id", None)
    normalized.setdefault("contract_version", STRATEGY_FORECAST_CONTRACT_VERSION)
    for field in ("as_of", "input_cutoff", "generated_at", "available_at"):
        timestamp = normalized.get(field)
        if isinstance(timestamp, datetime):
            timestamp = timestamp.isoformat()
        if isinstance(timestamp, str):
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                    # PostgreSQL uses six fractional digits in its canonical
                    # forecast vector. Keep exact-second cutoffs identical in
                    # both implementations instead of relying on formatter
                    # defaults that omit trailing zeroes.
                    normalized[field] = parsed.astimezone(UTC).isoformat(timespec="microseconds")
            except ValueError:
                pass
    # Use a scalar canonical vector. PostgreSQL can recompute this exact
    # representation without depending on jsonb object key ordering.
    forecast_range = normalized.get("forecast_range") or {}
    distribution = normalized.get("forecast_distribution") or {}
    vector = [
        normalized.get("contract_version"), normalized.get("ticker"),
        normalized.get("opportunity_episode_id"), str(normalized.get("strategy_revision_id")),
        normalized.get("strategy_evaluation_id"), normalized.get("target"),
        normalized.get("horizon"), _canonical_scalar(normalized.get("forecast_value")),
        _canonical_scalar(forecast_range.get("low")), _canonical_scalar(forecast_range.get("high")),
        "|".join(f"{key}={_canonical_scalar(distribution[key])}" for key in sorted(distribution)),
        normalized.get("probability_semantics"), normalized.get("model_artifact_id"),
        normalized.get("artifact_hash"), normalized.get("input_hash"),
        normalized.get("as_of"), normalized.get("input_cutoff"),
        normalized.get("generated_at"), normalized.get("available_at"),
    ]
    encoded = json.dumps(vector, separators=(", ", ": "), ensure_ascii=True)
    return "forecast:strategy-forecast:" + hashlib.sha256(encoded.encode()).hexdigest()[:32]


def _canonical_scalar(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    if not number.is_finite():
        raise ValueError("forecast numeric payload must be finite")
    if number == 0:
        return "0"
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return value


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else str(value) if value is not None else None


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _aware_timestamp(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _identity_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, list, tuple, set)):
        return bool(value)
    return True


def _identity_equal(left: Any, right: Any) -> bool:
    if not _identity_present(left) or not _identity_present(right):
        return False
    if isinstance(left, (Mapping, list, tuple, set)) or isinstance(right, (Mapping, list, tuple, set)):
        return json.dumps(_jsonable(left), sort_keys=True, separators=(",", ":"), default=str) == json.dumps(
            _jsonable(right), sort_keys=True, separators=(",", ":"), default=str,
        )
    return str(left).strip() == str(right).strip()


def _timestamps_equal(*values: Any) -> bool:
    try:
        parsed = tuple(_aware_timestamp(value) for value in values)
    except (TypeError, ValueError):
        return False
    return bool(parsed) and len(set(parsed)) == 1


def _row_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    primary = str(
        row.get("id")
        or row.get("stable_key")
        or row.get("source_id")
        or row.get("source")
        or ""
    )
    encoded = json.dumps(_jsonable(row), sort_keys=True, separators=(",", ":"), default=str)
    return primary, encoded


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = [
    "ALPHA_SIGNAL_CONTRACT_VERSION",
    "INSTRUMENT_STATE_SNAPSHOT_CONTRACT_VERSION",
    "OPPORTUNITY_RANK_CONTRACT_VERSION",
    "TICKER_OPPORTUNITY_RANKING_VERSION",
    "AlphaSignal",
    "InstrumentStateSnapshot",
    "OpportunityRank",
    "TradeUtility",
    "build_alpha_signal",
    "build_instrument_state_snapshot",
    "build_strategy_forecast",
    "strategy_forecast_id_for_payload",
    "calculate_trade_utility",
    "rank_opportunities",
]
