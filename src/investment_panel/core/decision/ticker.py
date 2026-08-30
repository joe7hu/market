"""Ticker-first decision contracts and deterministic composition rules.

This module is deliberately independent of PostgreSQL and providers.  It turns
already selected, point-in-time rows into one versioned ticker thesis.  The API
and database layers may add evidence or persistence, but they must not create a
second thesis for an option expression.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
import hashlib
import json
import math
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investment_panel.core.decision.constants import SYMBOL_RE
from investment_panel.core.instruments import DEFAULT_WATCHLIST, normalize_symbol
from investment_panel.core.risk_policy import compile_risk_policy_snapshot
from investment_panel.core.risk_policy import RiskPolicySnapshot
from investment_panel.core.decision.resolution import (
    DataQuality,
    DecisionResolutionV2,
    build_decision_resolution,
    next_action_for,
    resolution_from_legacy,
)


CONTRACT_VERSION = "ticker-decision.v1"
EXPERIMENT_ID = "ticker-first-v1"
OPPORTUNITY_EPISODE_CONTRACT_VERSION = "opportunity-episode.v1"
TRADE_EXPRESSION_CONTRACT_VERSION = "trade-expression.v1"
TRADE_PLAN_CONTRACT_VERSION = "trade-plan.v1"
OUTCOME_ATTRIBUTION_CONTRACT_VERSION = "outcome-attribution.v1"
OUTCOME_ATTRIBUTION_EVALUATION_VERSION = "ticker-outcome-attribution-v1"
PORTFOLIO_IMPACT_CONTRACT_VERSION = "portfolio-impact.v1"
_CONTEXT_UNSET = object()
_INSTRUMENT_IDENTITY_KEYS = ("ticker", "symbol", "instrument_symbol")


def _identity_aliases(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        return ()
    aliases: list[tuple[str, str]] = []
    for key in _INSTRUMENT_IDENTITY_KEYS:
        raw = value.get(key)
        if raw is None:
            continue
        rendered = str(raw).strip()
        if rendered:
            aliases.append((key, normalize_symbol(rendered)))
    return tuple(aliases)


def _target_identity_aliases(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        return ()
    aliases = list(_identity_aliases(value))
    aliases.extend(_identity_aliases(value.get("stock_impact")))
    return tuple(aliases)


def _all_portfolio_impact_identity_aliases(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        return ()
    aliases = list(_target_identity_aliases(value))
    for key in ("portfolio_before", "portfolio_after"):
        aliases.extend(_target_identity_aliases(value.get(key)))
    return tuple(aliases)


def _identity_aliases_conflict(aliases: Iterable[tuple[str, str]]) -> bool:
    return len({identity for _, identity in aliases}) > 1


class Stance(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class CapitalActionType(StrEnum):
    BUY = "BUY"
    ADD = "ADD"
    HOLD = "HOLD"
    TRIM = "TRIM"
    EXIT = "EXIT"
    HEDGE = "HEDGE"
    AVOID = "AVOID"
    WAIT_FOR_PRICE = "WAIT_FOR_PRICE"


class Horizon(StrEnum):
    TACTICAL = "TACTICAL"
    FUNDAMENTAL = "FUNDAMENTAL"


class OutcomeAttributionState(StrEnum):
    OBSERVING = "OBSERVING"
    RESOLVED = "RESOLVED"
    UNMEASURABLE = "UNMEASURABLE"
    QUARANTINED = "QUARANTINED"


class OutcomeEvidenceState(StrEnum):
    OBSERVED = "OBSERVED"
    DERIVED = "DERIVED"
    ESTIMATED = "ESTIMATED"
    UNMEASURABLE = "UNMEASURABLE"
    MISSING = "MISSING"


class ExpressionKind(StrEnum):
    STOCK = "STOCK"
    CALL = "CALL"
    PUT = "PUT"
    DEBIT_SPREAD = "DEBIT_SPREAD"
    CASH_SECURED_PUT = "CASH_SECURED_PUT"
    CRYPTO_SPOT = "CRYPTO_SPOT"
    CRYPTO_PERPETUAL = "CRYPTO_PERPETUAL"
    CASH = "CASH"


class OpportunityEpisodeStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    UNDERWRITING = "UNDERWRITING"
    SETUP = "SETUP"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class EvidencePolarity(StrEnum):
    FOR = "FOR"
    AGAINST = "AGAINST"
    FLIP = "FLIP"


class SignalEvidenceState(StrEnum):
    OBSERVED = "OBSERVED"
    DERIVED = "DERIVED"
    ESTIMATED = "ESTIMATED"
    HYPOTHESIS = "HYPOTHESIS"


class AvailabilityStatus(StrEnum):
    """Typed evidence availability; absence is never treated as available."""

    AVAILABLE = "available"
    UNSUPPORTED = "unsupported"
    MISSING = "missing"
    STALE = "stale"
    NOT_CALIBRATED = "not_calibrated"
    POLICY_BLOCKED = "policy_blocked"
    ERROR = "error"
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"


def availability_status_for_blockers(
    blockers: Iterable[Any], *, available_when_empty: bool = False,
) -> AvailabilityStatus:
    """Project one typed availability from the primary detailed blocker."""

    clean = [str(item).strip().lower() for item in blockers if str(item).strip()]
    if not clean:
        return AvailabilityStatus.AVAILABLE if available_when_empty else AvailabilityStatus.MISSING
    primary = clean[0]
    if primary == "alpha_evaluation_lineage_mismatch":
        return AvailabilityStatus.ERROR
    if primary == "alpha_oos_evaluation_not_passed":
        return AvailabilityStatus.POLICY_BLOCKED
    if "not_applicable" in primary or primary in {"cash_comparator", "cash_selected"}:
        return AvailabilityStatus.NOT_APPLICABLE
    if "unsupported" in primary or (
        "expression" in primary and "unavailable" in primary
    ):
        return AvailabilityStatus.UNSUPPORTED
    if "stale" in primary:
        return AvailabilityStatus.STALE
    if "calibrat" in primary or "evaluation" in primary or "oos" in primary:
        return AvailabilityStatus.NOT_CALIBRATED
    if any(token in primary for token in ("error", "invalid", "mismatch", "duplicat")):
        return AvailabilityStatus.ERROR
    if any(token in primary for token in ("missing", "incomplete", "required")):
        return AvailabilityStatus.MISSING
    if "pending" in primary or "collecting" in primary:
        return AvailabilityStatus.PENDING
    return AvailabilityStatus.POLICY_BLOCKED


class NumericRange(BaseModel):
    low: float
    high: float

    @model_validator(mode="after")
    def ordered(self) -> "NumericRange":
        if self.low > self.high:
            raise ValueError("range low must not exceed high")
        return self


class PriceRange(NumericRange):
    low: float = Field(ge=0)
    high: float = Field(ge=0)


class Invalidation(BaseModel):
    kind: str = Field(pattern="^(price|event|date)$")
    value: str | float | date
    statement: str


class EvidenceItem(BaseModel):
    statement: str
    polarity: EvidencePolarity = EvidencePolarity.FOR
    source: str | None = None
    reference: str | None = None
    event_at: datetime | date | None = None
    published_at: datetime | date | None = None
    available_at: datetime | date | None = None
    revision: str | None = None
    license: str | None = None


class ScenarioOutcome(BaseModel):
    name: str = Field(pattern="^(bear|base|bull)$")
    probability: float | None = Field(default=None, ge=0, le=1)
    description: str
    price_range: PriceRange | None = None
    return_range: NumericRange | None = None


class DataRequest(BaseModel):
    """A missing value that can change the current recommendation."""

    field: str
    ticker: str
    why_it_matters: str
    required_source: str
    max_age: str
    max_age_seconds: int | None = Field(default=None, ge=0)
    owner: str
    collect_now: str
    expected_completion: str
    decision_impact: str


class SignalDeclaration(BaseModel):
    """Provenance and behavior contract for one signal family."""

    name: str
    economic_mechanism: str
    applicable_horizon: str
    evidence_state: SignalEvidenceState
    source: str
    coverage: str
    freshness: str
    transformation: str
    missing_data_behavior: str
    incremental_predictive_value: str


class InputManifest(BaseModel):
    as_of: datetime
    input_hash: str = Field(pattern="^[0-9a-f]{64}$")
    code_version: str
    experiment_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    source_versions: dict[str, str] = Field(default_factory=dict)
    signal_declarations: list[SignalDeclaration] = Field(default_factory=list)


class InputLineage(BaseModel):
    """One immutable, point-in-time input used by an opportunity episode."""

    model_config = ConfigDict(extra="allow")

    field: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_version: str | None = None
    event_at: datetime | None = None
    published_at: datetime | None = None
    available_at: datetime
    received_at: datetime | None = None
    revision: str | None = None
    opportunity_episode_id: str | None = None
    decision_revision: str | None = None
    policy_version: str | None = None
    cutoff: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_lineage_aliases(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        aliases = {
            "source": "source_id",
            "version": "source_version",
            "episode_id": "opportunity_episode_id",
        }
        for old, new in aliases.items():
            if new not in result and old in result:
                result[new] = result[old]
        return result

    @model_validator(mode="after")
    def timestamps_are_timezone_aware(self) -> "InputLineage":
        for name in ("event_at", "published_at", "available_at", "received_at", "cutoff"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"input lineage {name} must be timezone-aware")
        return self


def _input_lineage_identity(lineage: InputLineage) -> tuple[Any, ...]:
    return (
        lineage.field,
        lineage.source_id,
        lineage.source_version,
        _utc(lineage.available_at),
        lineage.revision,
        _utc(lineage.cutoff) if lineage.cutoff is not None else None,
    )


MARKET_HORIZONS = (
    "intraday",
    "1-5 trading days",
    "2-8 weeks",
    "3-12 months",
)
MARKET_DIMENSIONS = (
    "growth/inflation",
    "monetary liquidity",
    "rates",
    "credit",
    "dollar/commodities",
    "equity internals",
    "volatility",
    "positioning",
    "corporate cycle",
    "crypto liquidity",
    "event risk",
    "microstructure",
)

MARKET_SOURCE_PRIORITY: dict[str, tuple[str, ...]] = {
    "growth/inflation": ("fred", "bea", "bls"),
    "monetary liquidity": ("fred", "treasury", "central-bank"),
    "rates": ("treasury", "fred", "sec"),
    "credit": ("fred", "ice-bofa", "sec"),
    "dollar/commodities": ("fred", "cme", "eia"),
    "breadth": ("confirmed_daily_prices",),
    "revisions": ("sec_companyfacts", "yfinance"),
    "equity internals": ("confirmed_daily_prices",),
    "volatility": ("confirmed_daily_prices", "cboe"),
    "positioning": ("sec", "short-interest"),
    "corporate cycle": ("sec_companyfacts", "estimates"),
    "crypto liquidity": ("daily-market-prices", "coingecko"),
    "event risk": ("official-event-calendar",),
    "microstructure": ("consolidated-quotes", "venue-depth"),
}

MARKET_HORIZON_FOR_DECISION = {
    "TACTICAL": "1-5 trading days",
    "FUNDAMENTAL": "3-12 months",
}

MARKET_REQUIRED_DIMENSIONS: dict[ExpressionKind, tuple[str, ...]] = {
    ExpressionKind.CASH: (),
    ExpressionKind.STOCK: ("equity internals",),
    ExpressionKind.CALL: ("equity internals", "volatility"),
    ExpressionKind.PUT: ("equity internals", "volatility"),
    ExpressionKind.DEBIT_SPREAD: ("equity internals", "volatility"),
    ExpressionKind.CASH_SECURED_PUT: ("equity internals", "volatility"),
    ExpressionKind.CRYPTO_SPOT: ("crypto liquidity",),
    ExpressionKind.CRYPTO_PERPETUAL: ("crypto liquidity", "volatility"),
}

MARKET_REQUIRED_DIMENSIONS_BY_HORIZON: dict[str, dict[ExpressionKind, tuple[str, ...]]] = {
    "TACTICAL": MARKET_REQUIRED_DIMENSIONS,
    "FUNDAMENTAL": {
        kind: dimensions + (("corporate cycle",) if kind is not ExpressionKind.CASH else ())
        for kind, dimensions in MARKET_REQUIRED_DIMENSIONS.items()
    },
}


class MarketEvidenceAssessment(BaseModel):
    """Decision-scoped market evidence; no global market-ready state."""

    model_config = ConfigDict(extra="allow", frozen=True)

    expression_kind: ExpressionKind
    horizon: str = Field(min_length=1)
    decision_horizon: str = Field(min_length=1)
    coverage_status: str = "unavailable"
    required_dimensions: tuple[str, ...] = ()
    advisory_dimensions: tuple[str, ...] = ()
    available_dimensions: tuple[str, ...] = ()
    blocking_dimensions: tuple[str, ...] = ()
    status: str = "advisory"
    blockers: tuple[str, ...] = ()

    @property
    def is_blocking(self) -> bool:
        return bool(self.blocking_dimensions)


class MarketDimensionState(BaseModel):
    """One typed market dimension at one point-in-time horizon."""

    model_config = ConfigDict(extra="allow", frozen=True)

    dimension: str = Field(min_length=1)
    horizon: str = Field(min_length=1)
    state: str | None = None
    change_drivers: tuple[str, ...] = ()
    evidence_status: str = "unavailable"
    availability_status: AvailabilityStatus = AvailabilityStatus.MISSING
    uncertainty: str | None = None
    quality: str | None = None
    blockers: tuple[str, ...] = ()
    lineage: tuple[InputLineage, ...] = ()
    probability: float | None = Field(default=None, ge=0, le=1)
    probability_method: str | None = None
    probability_model_version: str | None = None
    source_priority: tuple[str, ...] = ()
    selected_source: str | None = None
    regime_distribution: dict[str, float] = Field(default_factory=dict)
    regime_probability_method: str | None = None
    regime_model_version: str | None = None
    regime_sample_count: int | None = Field(default=None, ge=0)
    baseline_result: dict[str, Any] = Field(default_factory=dict)
    challenger_result: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def infer_availability_status(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "availability_status" not in value:
            result = dict(value)
            if str(result.get("evidence_status") or "").lower() == "available":
                result["availability_status"] = AvailabilityStatus.AVAILABLE
            return result
        return value

    @model_validator(mode="after")
    def probability_has_method(self) -> "MarketDimensionState":
        if self.probability is not None and not self.probability_method:
            raise ValueError("market probabilities require a named method")
        if self.probability is not None and not self.probability_model_version:
            raise ValueError("market probabilities require a model version")
        return self


class CoverageMatrixRow(BaseModel):
    """Backend-owned coverage evidence for one market dimension."""

    model_config = ConfigDict(extra="allow", frozen=True)

    dimension: str = Field(min_length=1)
    asset_class: str = Field(min_length=1)
    horizon: str = Field(min_length=1)
    provider: str | None = None
    history_start: date | datetime | None = None
    point_in_time_safe: bool = False
    freshness_slo: str | None = None
    current_status: str = "unavailable"
    decision_impact: str = "context"
    fallback_policy: str = "unavailable"
    input_cutoff: datetime | None = None
    input_lineage: tuple[InputLineage, ...] = ()
    source_priority: tuple[str, ...] = ()
    selected_source: str | None = None
    blockers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def enforce_lineage_cutoff(self) -> "CoverageMatrixRow":
        if self.input_cutoff is None:
            return self
        if self.input_cutoff.tzinfo is None:
            raise ValueError("coverage row input_cutoff must be timezone-aware")
        cutoff = _utc(self.input_cutoff)
        if any(_utc(item.available_at) > cutoff for item in self.input_lineage):
            raise ValueError("coverage row lineage cannot be newer than its cutoff")
        return self


class CoverageMatrix(BaseModel):
    """Frozen point-in-time coverage contract for market state inputs."""

    model_config = ConfigDict(extra="allow", frozen=True)

    contract_version: str = "coverage-matrix.v1"
    matrix_id: str = Field(min_length=1)
    as_of: datetime
    input_cutoff: datetime
    rows: tuple[CoverageMatrixRow, ...] = ()

    @model_validator(mode="after")
    def enforce_cutoff(self) -> "CoverageMatrix":
        if self.as_of.tzinfo is None or self.input_cutoff.tzinfo is None:
            raise ValueError("coverage matrix timestamps must be timezone-aware")
        if _utc(self.as_of) != _utc(self.input_cutoff):
            raise ValueError("coverage matrix as_of and input_cutoff must match")
        return self


class MarketStateSnapshot(BaseModel):
    """Frozen, versioned market state selected at one information cutoff."""

    model_config = ConfigDict(extra="allow", frozen=True)

    contract_version: str = "market-state-snapshot.v1"
    snapshot_id: str = Field(min_length=1)
    publication_id: str | None = None
    as_of: datetime
    input_cutoff: datetime
    horizons: dict[str, tuple[MarketDimensionState, ...]] = Field(default_factory=dict)
    coverage_matrix: CoverageMatrix | None = None
    input_lineage: tuple[InputLineage, ...] = ()
    availability: str = "unavailable"
    availability_status: AvailabilityStatus = AvailabilityStatus.MISSING
    blockers: tuple[str, ...] = ()
    decision_evidence: tuple[MarketEvidenceAssessment, ...] = ()
    regime_distributions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    baseline_challenger: dict[str, dict[str, Any]] = Field(default_factory=dict)
    source_priorities: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    selected_sources: dict[str, str | None] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def infer_availability_status(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "availability_status" not in value:
            result = dict(value)
            if str(result.get("availability") or "").lower() == "available":
                result["availability_status"] = AvailabilityStatus.AVAILABLE
            return result
        return value

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        for old, new in {
            "id": "snapshot_id",
            "cutoff": "input_cutoff",
            "coverage": "coverage_matrix",
            "lineage": "input_lineage",
        }.items():
            if new not in result and old in result:
                result[new] = result[old]
        if "as_of" not in result and "input_cutoff" in result:
            result["as_of"] = result["input_cutoff"]
        return result

    @model_validator(mode="after")
    def enforce_cutoff(self) -> "MarketStateSnapshot":
        if self.as_of.tzinfo is None or self.input_cutoff.tzinfo is None:
            raise ValueError("market snapshot timestamps must be timezone-aware")
        if _utc(self.as_of) != _utc(self.input_cutoff):
            raise ValueError("market snapshot as_of and input_cutoff must match")
        if self.contract_version == "market-state-snapshot.v1" and self.availability == "available":
            if set(self.horizons) != set(MARKET_HORIZONS):
                raise ValueError("available market snapshots require all market horizons")
            if any(
                {item.dimension for item in self.horizons[horizon]} != set(MARKET_DIMENSIONS)
                for horizon in MARKET_HORIZONS
            ):
                raise ValueError("available market snapshots require all market dimensions")
            if self.coverage_matrix is None:
                raise ValueError("available market snapshots require a coverage matrix")
        cutoff = _utc(self.input_cutoff)
        for lineage in self.input_lineage:
            if _utc(lineage.available_at) > cutoff:
                raise ValueError("market snapshot lineage cannot be newer than its cutoff")
        for horizon, dimensions in self.horizons.items():
            for dimension in dimensions:
                if dimension.horizon != horizon:
                    raise ValueError("market snapshot horizon key must match dimension horizon")
                if any(_utc(item.available_at) > cutoff for item in dimension.lineage):
                    raise ValueError("market dimension lineage cannot be newer than its cutoff")
        if self.coverage_matrix is not None and _utc(self.coverage_matrix.input_cutoff) != cutoff:
            raise ValueError("market coverage cutoff must match the market snapshot")
        if self.coverage_matrix is not None:
            for row in self.coverage_matrix.rows:
                if row.input_cutoff is not None and _utc(row.input_cutoff) != cutoff:
                    raise ValueError("coverage row cutoff must match the market snapshot")
        return self


def market_required_dimensions(
    expression_kind: ExpressionKind | str,
    horizon: Horizon | str | None = None,
) -> tuple[str, ...]:
    """Return the backend-owned required market dimensions for an expression."""

    kind = ExpressionKind(expression_kind)
    if horizon is None:
        return MARKET_REQUIRED_DIMENSIONS[kind]
    horizon_name = getattr(horizon, "value", str(horizon)).upper()
    return MARKET_REQUIRED_DIMENSIONS_BY_HORIZON.get(horizon_name, MARKET_REQUIRED_DIMENSIONS)[kind]


def _v2_dimension_evidence_valid(
    snapshot: MarketStateSnapshot,
    market_horizon: str,
    dimension: str,
) -> bool:
    """Validate the one state/coverage pair used by a V2 decision gate."""

    state_rows = [
        item for item in snapshot.horizons.get(market_horizon, ())
        if item.dimension == dimension and item.horizon == market_horizon
    ]
    coverage_rows = [
        item for item in snapshot.coverage_matrix.rows  # type: ignore[union-attr]
        if item.dimension == dimension and item.horizon == market_horizon
    ]
    if len(state_rows) != 1 or len(coverage_rows) != 1:
        return False
    state, coverage = state_rows[0], coverage_rows[0]
    if (
        str(state.evidence_status).lower() != "available"
        or str(getattr(state.availability_status, "value", state.availability_status)).lower() != "available"
        or not state.state
        or not state.uncertainty
        or state.blockers
        or not state.selected_source
        or not state.lineage
        or str(coverage.current_status).lower() != "available"
        or not coverage.point_in_time_safe
        or coverage.blockers
        or not coverage.selected_source
        or not coverage.input_lineage
        or not state.source_priority
        or not coverage.source_priority
    ):
        return False
    cutoff = _utc(snapshot.input_cutoff)
    if any(
        item.cutoff is None or _utc(item.cutoff) != cutoff
        for item in (*state.lineage, *coverage.input_lineage)
    ):
        return False
    state_sources = {item.source_id for item in state.lineage}
    coverage_sources = {item.source_id for item in coverage.input_lineage}
    return (
        state.selected_source == coverage.selected_source
        and state.selected_source in state_sources
        and coverage.selected_source in coverage_sources
        and state.selected_source in state.source_priority
        and coverage.selected_source in coverage.source_priority
        and tuple(_input_lineage_identity(item) for item in state.lineage)
        == tuple(_input_lineage_identity(item) for item in coverage.input_lineage)
        and all(_utc(item.available_at) <= cutoff for item in state.lineage)
        and all(_utc(item.available_at) <= cutoff for item in coverage.input_lineage)
        and coverage.input_cutoff is not None
        and _utc(coverage.input_cutoff) == cutoff
    )


def market_evidence_for_decision(
    snapshot: MarketStateSnapshot | None,
    expression_kind: ExpressionKind | str,
    horizon: Horizon | str,
) -> MarketEvidenceAssessment:
    """Project one expression+horizon assessment from the published snapshot."""

    kind = ExpressionKind(expression_kind)
    horizon_name = getattr(horizon, "value", str(horizon)).upper()
    market_horizon = MARKET_HORIZON_FOR_DECISION.get(horizon_name, str(horizon).lower())
    required = market_required_dimensions(kind, horizon_name)
    advisory = tuple(dimension for dimension in MARKET_DIMENSIONS if dimension not in required)
    if not required:
        return MarketEvidenceAssessment(
            expression_kind=kind,
            horizon=market_horizon,
            decision_horizon=horizon_name,
            coverage_status="not_required",
            required_dimensions=(),
            advisory_dimensions=advisory,
            status="not_applicable",
        )
    coverage_rows = () if snapshot is None or snapshot.coverage_matrix is None else snapshot.coverage_matrix.rows
    coverage_version_ok = bool(
        snapshot is not None
        and snapshot.contract_version == "market-state-snapshot.v2"
        and snapshot.coverage_matrix is not None
        and snapshot.coverage_matrix.contract_version == "coverage-matrix.v2"
        and _utc(snapshot.coverage_matrix.input_cutoff) == _utc(snapshot.input_cutoff)
    )
    coverage_by_dimension = [row for row in coverage_rows if row.horizon == market_horizon]
    coverage_counts = {dimension: sum(row.dimension == dimension for row in coverage_by_dimension) for dimension in required}
    valid_dimensions = tuple(
        dimension for dimension in MARKET_DIMENSIONS
        if snapshot is not None
        and snapshot.contract_version == "market-state-snapshot.v2"
        and coverage_version_ok
        and _v2_dimension_evidence_valid(snapshot, market_horizon, dimension)
    )
    coverage_valid = coverage_version_ok and all(
        coverage_counts[dimension] == 1 and dimension in valid_dimensions for dimension in required
    )
    coverage_status = "available" if coverage_valid else "invalid" if snapshot is not None else "missing"
    if snapshot is not None and snapshot.contract_version == "market-state-snapshot.v2":
        available = valid_dimensions
    else:
        rows = {
            item.dimension: item
            for item in (snapshot.horizons.get(market_horizon, ()) if snapshot is not None else ())
        }
        available = tuple(
            dimension for dimension in MARKET_DIMENSIONS
            if str(getattr(rows.get(dimension), "evidence_status", "")).lower() == "available"
        )
    blocking = tuple(dimension for dimension in required if dimension not in available)
    if snapshot is not None and snapshot.contract_version == "market-state-snapshot.v2" and not coverage_valid:
        blocking = required
    blockers = tuple(f"market_required_dimension_unavailable:{dimension}" for dimension in blocking)
    if snapshot is None:
        blockers = ("market_coverage_matrix_missing",) + blockers
    elif snapshot.contract_version == "market-state-snapshot.v2" and not coverage_valid:
        blockers = ("market_coverage_matrix_invalid",) + blockers
    status = "not_applicable" if not required else "blocking" if blocking else (
        "available" if all(dimension in available for dimension in MARKET_DIMENSIONS) else "advisory"
    )
    if snapshot is None and required:
        blockers = tuple(f"market_required_dimension_unavailable:{dimension}" for dimension in required)
        status = "blocking"
    return MarketEvidenceAssessment(
        expression_kind=kind,
        horizon=market_horizon,
        decision_horizon=horizon_name,
        coverage_status=coverage_status,
        required_dimensions=required,
        advisory_dimensions=advisory,
        available_dimensions=available,
        blocking_dimensions=blocking,
        status=status,
        blockers=blockers,
    )


class PortfolioImpact(BaseModel):
    """Frozen before/after portfolio impact for one exact expression."""

    model_config = ConfigDict(extra="allow", frozen=True)

    contract_version: str = PORTFOLIO_IMPACT_CONTRACT_VERSION
    impact_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    opportunity_episode_id: str = Field(min_length=1)
    expression_kind: ExpressionKind
    expression_identity: str = Field(min_length=1)
    decision_revision: str = Field(min_length=1)
    risk_policy_version: str = Field(min_length=1)
    market_snapshot_id: str = Field(min_length=1)
    market_state_publication_id: str | None = None
    cutoff: datetime
    input_lineage: tuple[InputLineage, ...] = ()
    portfolio_before: dict[str, Any] = Field(default_factory=dict)
    portfolio_after: dict[str, Any] = Field(default_factory=dict)
    position_weight_before: float | None = None
    position_weight_after: float | None = None
    gross_exposure_before: float | None = None
    gross_exposure_after: float | None = None
    net_exposure_before: float | None = None
    net_exposure_after: float | None = None
    symbol_concentration_delta: float | None = None
    sector_concentration_delta: float | None = None
    beta_delta: float | None = None
    correlation_cluster_delta: float | None = None
    planned_loss: float | None = Field(default=None, ge=0)
    adv_participation: float | None = Field(default=None, ge=0)
    days_to_exit: float | None = Field(default=None, ge=0)
    marginal_risk: float | None = None
    diversification_benefit: float | None = None
    expected_transaction_costs: float | None = Field(default=None, ge=0)
    tail_risk_penalty: float | None = Field(default=None, ge=0)
    portfolio_overlap_penalty: float | None = Field(default=None, ge=0)
    risk_budget_consumed: float | None = None
    positions_most_correlated: tuple[str, ...] = ()
    position_to_trim_or_replace: str | None = None
    scenario_pnl: dict[str, Any] | None = None
    factor_exposure: dict[str, Any] | None = None
    greeks: dict[str, Any] | None = None
    liquidity: dict[str, Any] | None = None
    cash_comparator: dict[str, Any] | None = None
    top_alternative: str | None = None
    funding_source_or_position_to_trim: str | None = None
    availability: str = "unavailable"
    availability_status: AvailabilityStatus = AvailabilityStatus.MISSING
    blockers: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def infer_availability_status(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "availability_status" not in value:
            result = dict(value)
            if str(result.get("availability") or "").lower() == "available":
                result["availability_status"] = AvailabilityStatus.AVAILABLE
            return result
        return value

    @classmethod
    def compose(cls, **kwargs: Any) -> "PortfolioImpact":
        return compose_portfolio_impact(**kwargs)

    @classmethod
    def from_legacy(cls, value: Any, *, ticker: str) -> "PortfolioImpact":
        return _portfolio_impact_from_legacy(value, ticker=ticker)

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        for old, new in {
            "expression": "expression_kind",
            "policy_version": "risk_policy_version",
            "snapshot_id": "market_snapshot_id",
            "lineage": "input_lineage",
        }.items():
            if new not in result and old in result:
                result[new] = result[old]
        if _identity_aliases_conflict(_target_identity_aliases(result)):
            raise ValueError("portfolio impact contains conflicting ticker/symbol/instrument_symbol aliases")
        if isinstance(result.get("ticker"), str):
            result["ticker"] = normalize_symbol(result["ticker"])
        return result

    @model_validator(mode="after")
    def enforce_cutoff(self) -> "PortfolioImpact":
        if self.cutoff.tzinfo is None:
            raise ValueError("portfolio impact cutoff must be timezone-aware")
        target_ticker = normalize_symbol(self.ticker)
        if SYMBOL_RE.fullmatch(target_ticker) is None:
            raise ValueError("portfolio impact ticker must be a valid target ticker")
        cutoff = _utc(self.cutoff)
        for lineage in self.input_lineage:
            if _utc(lineage.available_at) > cutoff:
                raise ValueError("portfolio impact lineage cannot be newer than its cutoff")
        before = self.portfolio_before
        after = self.portfolio_after
        container_aliases = tuple(
            alias
            for source in (before, after)
            for alias in _target_identity_aliases(source)
        )
        if _identity_aliases_conflict(container_aliases):
            raise ValueError("portfolio impact contains conflicting ticker/symbol/instrument_symbol aliases")
        if any(identity != target_ticker for _, identity in container_aliases):
            raise ValueError("portfolio impact requires a matching target ticker across containers")
        if self.availability not in {"available", "unavailable"}:
            raise ValueError("portfolio impact availability must be available or unavailable")
        if self.availability == "available" and self.availability_status is not AvailabilityStatus.AVAILABLE:
            raise ValueError("available portfolio impacts require available evidence status")
        if self.availability == "unavailable":
            if not self.blockers:
                raise ValueError("unavailable portfolio impacts require blockers")
            return self
        if self.blockers:
            raise ValueError("available portfolio impacts cannot have blockers")
        book_identity = str(before.get("book_identity") or "")
        if (
            not book_identity
            or after.get("book_identity") != book_identity
            or before.get("valuation_complete") is not True
            or before.get("missing_valuation_count") != 0
            or before.get("valued_position_count") != before.get("eligible_position_count")
        ):
            raise ValueError("available portfolio impacts require a complete cutoff book")
        if self.expression_kind is ExpressionKind.CASH:
            if (
                before != after
                or self.marginal_risk != 0
                or self.risk_budget_consumed != 0
                or self.scenario_pnl != {"status": "zero_impact", "pnl": 0.0}
                or self.liquidity != {"status": "not_applicable"}
            ):
                raise ValueError("CASH portfolio impact must be exact zero change")
        elif self.expression_kind in {
            ExpressionKind.STOCK,
            ExpressionKind.CRYPTO_SPOT,
            ExpressionKind.CRYPTO_PERPETUAL,
        }:
            required = (
                self.position_weight_before,
                self.position_weight_after,
                self.gross_exposure_before,
                self.gross_exposure_after,
                self.net_exposure_before,
                self.net_exposure_after,
                self.planned_loss,
                self.adv_participation,
                self.days_to_exit,
                self.sector_concentration_delta,
                self.beta_delta,
                self.correlation_cluster_delta,
            )
            if any(value is None for value in required):
                raise ValueError("available stock portfolio impacts require complete evidence")
            evidence = before.get("stock_evidence")
            if not isinstance(evidence, Mapping):
                raise ValueError("available stock portfolio impacts require stock evidence")
            positions = before.get("positions") or ()
            btc_required = _stock_btc_scenarios_required(
                evidence,
                positions,
                target_ticker,
                portfolio_before=before,
            )
            scenario_pnl = _stock_scenario_pnl(
                evidence,
                btc_required=btc_required,
                largest_holding=_stock_largest_holding(positions),
            )
            if scenario_pnl is None or self.scenario_pnl != scenario_pnl:
                raise ValueError("available stock portfolio impacts require complete stress scenarios")
            budget_available, budget_consumed = _stock_risk_budget(evidence)
            if (
                budget_available is None
                or budget_consumed is None
                or self.risk_budget_consumed is None
                or self.marginal_risk is None
                or not math.isclose(self.risk_budget_consumed, budget_consumed, rel_tol=1e-9, abs_tol=1e-6)
                or not math.isclose(self.marginal_risk, budget_consumed, rel_tol=1e-9, abs_tol=1e-6)
                or not math.isclose(self.planned_loss, budget_consumed, rel_tol=1e-9, abs_tol=1e-6)
                or budget_available < budget_consumed
            ):
                raise ValueError("available stock portfolio impacts require used risk-budget evidence")
            liquidity = self.liquidity
            liquidity_status = str(_pick(liquidity or {}, "status", "availability") or "").lower()
            adv = _number(_pick(liquidity or {}, "avg_dollar_volume", "average_dollar_volume", "adv"))
            participation_limit = _number(
                _pick(liquidity or {}, "adv_participation_limit", "max_adv_participation")
            )
            if (
                not isinstance(liquidity, Mapping)
                or liquidity_status != "available"
                or adv is None
                or adv <= 0
                or participation_limit is None
                or participation_limit <= 0
                or participation_limit > 1
                or self.adv_participation > participation_limit
            ):
                raise ValueError("available stock portfolio impacts require usable liquidity evidence")
            cash = self.cash_comparator
            cash_status = str(_pick(cash or {}, "status", "availability") or "").lower()
            if (
                not isinstance(cash, Mapping)
                or cash_status != "available"
                or not any(_number(cash.get(key)) is not None for key in _STOCK_CASH_COMPARISON_KEYS)
            ):
                raise ValueError("available stock portfolio impacts require a cash comparison")
            top_alternative = _stock_top_alternative(evidence, cash, before)
            if top_alternative is None or self.top_alternative != top_alternative:
                raise ValueError("available stock portfolio impacts require a top alternative")
            if _stock_evidence_label(self.funding_source_or_position_to_trim) is None:
                raise ValueError("available stock portfolio impacts require funding or trim evidence")
        else:
            raise ValueError("non-CASH portfolio impacts require unsupported institutional evidence")
        return self


def trade_expression_identity(value: Any) -> str:
    """Return the stable identity used to bind an impact to one expression."""

    expression = trade_expression_from_legacy(value)
    encoded = json.dumps(
        expression.model_dump(mode="json", exclude={"availability_status", "blockers"}),
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{expression.kind.value}:{hashlib.sha256(encoded.encode()).hexdigest()}"


class RiskPolicy(BaseModel):
    conviction_tier: str = Field(pattern="^(EXPLORATORY|STANDARD|HIGH)$")
    loss_budget_pct: float = Field(gt=0, le=0.02)
    loss_budget: float | None = Field(default=None, ge=0)
    max_ticker_loss_pct: float = 0.04
    max_total_open_planned_loss_pct: float = 0.10
    position_limit_pct: float = Field(default=0.10, gt=0, le=1)
    policy_version: str = "risk-policy.v2:legacy"


class ExpressionDecision(BaseModel):
    """One way to express the same ticker thesis."""

    model_config = ConfigDict(use_enum_values=False)

    kind: ExpressionKind
    ticker: str
    horizon: Horizon
    thesis_revision: str
    stance: Stance
    scenarios: list[ScenarioOutcome] = Field(default_factory=list)
    legs: list[dict[str, Any]] = Field(default_factory=list)
    entry_range: PriceRange | None = None
    target_range: PriceRange | None = None
    invalidation: Invalidation | None = None
    quantity: int | None = Field(default=None, ge=0)
    loss_budget: float | None = Field(default=None, ge=0)
    max_loss_per_unit: float | None = Field(default=None, ge=0)
    planned_loss: float | None = Field(default=None, ge=0)
    expected_transaction_costs: float | None = Field(default=None, ge=0)
    net_expected_value_per_loss_dollar: float | None = None
    lower_confidence_expectancy: float | None = None
    liquidity_score: float | None = Field(default=None, ge=0, le=1)
    spread_pct: float | None = Field(default=None, ge=0)
    fill_probability: float | None = Field(default=None, ge=0, le=1)
    horizon_fit: float | None = Field(default=None, ge=0, le=1)
    status: str = Field(pattern="^(eligible|blocked|unavailable|not_selected)$")
    availability_status: AvailabilityStatus = AvailabilityStatus.MISSING
    blockers: tuple[str, ...] = ()
    selected: bool = False
    rationale: str
    data_requests: list[DataRequest] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def typed_availability(cls, value: Any) -> Any:
        if not isinstance(value, Mapping) or "availability_status" in value:
            return value
        result = dict(value)
        status = str(result.get("status") or "")
        result["availability_status"] = (
            AvailabilityStatus.AVAILABLE
            if status in {"eligible", "not_selected"}
            else AvailabilityStatus.UNSUPPORTED
            if status == "unavailable"
            else AvailabilityStatus.MISSING
            if result.get("data_requests")
            else AvailabilityStatus.POLICY_BLOCKED
        )
        return result

    @model_validator(mode="after")
    def availability_matches_status(self) -> "ExpressionDecision":
        if self.status in {"eligible", "not_selected"} and self.availability_status is not AvailabilityStatus.AVAILABLE:
            raise ValueError("usable expressions require available status")
        if self.availability_status is AvailabilityStatus.AVAILABLE and self.blockers:
            raise ValueError("available expressions cannot have blockers")
        return self


# The existing expression model is the source of truth.  The aliases and
# adapters make the canonical name explicit without creating a second model or
# a second decision owner.
TradeExpression = ExpressionDecision


def trade_expression_from_legacy(value: Any) -> TradeExpression:
    if isinstance(value, ExpressionDecision):
        return value
    return TradeExpression.model_validate(value)


def trade_expression_from_expression_decision(value: Any) -> TradeExpression:
    return trade_expression_from_legacy(value)


def expression_decision_from_trade_expression(value: Any) -> ExpressionDecision:
    return trade_expression_from_legacy(value)


def expression_decision_to_trade_expression(value: Any) -> TradeExpression:
    return trade_expression_from_legacy(value)


class OpportunityEpisode(BaseModel):
    """One point-in-time ticker thesis with competing trade expressions."""

    model_config = ConfigDict(extra="allow", use_enum_values=False)

    contract_version: str = OPPORTUNITY_EPISODE_CONTRACT_VERSION
    episode_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    decision_revision: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    cutoff: datetime
    input_lineage: list[InputLineage] = Field(min_length=1)
    expressions: dict[ExpressionKind, TradeExpression] = Field(min_length=1)
    selected_expression: TradeExpression | None = None
    # Lifecycle fields are deliberately part of the existing episode boundary.
    # They are persisted in the existing JSONB envelope; decision_revision is
    # the revision of this observation, never the durable episode identity.
    thesis_identity: str = ""
    first_seen_at: datetime | None = None
    last_updated_at: datetime | None = None
    status: OpportunityEpisodeStatus = OpportunityEpisodeStatus.DISCOVERED
    horizon: Horizon | None = None
    catalyst_window: str | None = None
    current_revision: str = ""
    closed_reason: str | None = None
    superseded_by: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_episode_aliases(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        aliases = {
            "opportunity_episode_id": "episode_id",
            "as_of": "cutoff",
            "lineage": "input_lineage",
        }
        for old, new in aliases.items():
            if new not in result and old in result:
                result[new] = result[old]
        if not result.get("ticker") and result.get("symbol"):
            result["ticker"] = result["symbol"]
        if isinstance(result.get("ticker"), str):
            result["ticker"] = result["ticker"].strip().upper()
        if not result.get("thesis_identity") and result.get("ticker"):
            result["thesis_identity"] = f"ticker:{str(result['ticker']).strip().upper()}"
        if not result.get("current_revision") and result.get("decision_revision"):
            result["current_revision"] = result["decision_revision"]
        if result.get("first_seen_at") is None and result.get("cutoff") is not None:
            result["first_seen_at"] = result["cutoff"]
        if result.get("last_updated_at") is None and result.get("cutoff") is not None:
            result["last_updated_at"] = result["cutoff"]

        expressions = result.get("expressions")
        selected = result.get("selected_expression")
        if isinstance(expressions, Mapping) and isinstance(selected, str):
            for key, expression in expressions.items():
                if str(key).upper() == selected.upper():
                    result["selected_expression"] = expression
                    break
        elif isinstance(expressions, Mapping) and isinstance(selected, Mapping):
            selected_kind = str(selected.get("kind") or "").upper()
            if selected_kind and set(selected) <= {"kind"}:
                for key, expression in expressions.items():
                    if str(key).upper() == selected_kind:
                        result["selected_expression"] = expression
                        break
        return result

    @model_validator(mode="after")
    def enforce_point_in_time_identity(self) -> "OpportunityEpisode":
        if self.cutoff.tzinfo is None:
            raise ValueError("opportunity episode cutoff must be timezone-aware")
        cutoff = _utc(self.cutoff)
        self.thesis_identity = self.thesis_identity or f"ticker:{self.ticker}"
        self.current_revision = self.current_revision or self.decision_revision
        self.first_seen_at = _utc(self.first_seen_at or cutoff)
        self.last_updated_at = _utc(self.last_updated_at or cutoff)
        if self.last_updated_at < self.first_seen_at:
            raise ValueError("opportunity episode last_updated_at cannot precede first_seen_at")
        if self.status is OpportunityEpisodeStatus.CLOSED and not self.closed_reason:
            raise ValueError("closed opportunity episodes require a closed_reason")
        lineage_keys: set[tuple[Any, ...]] = set()
        for lineage in self.input_lineage:
            if _utc(lineage.available_at) > cutoff:
                raise ValueError("opportunity episode lineage cannot be newer than its cutoff")
            if lineage.opportunity_episode_id and lineage.opportunity_episode_id != self.episode_id:
                raise ValueError("opportunity episode lineage id must match the episode")
            if lineage.decision_revision and lineage.decision_revision != self.decision_revision:
                raise ValueError("opportunity episode lineage revision must match the episode")
            if lineage.policy_version and lineage.policy_version != self.policy_version:
                raise ValueError("opportunity episode lineage policy must match the episode")
            if lineage.cutoff and _utc(lineage.cutoff) != cutoff:
                raise ValueError("opportunity episode lineage cutoff must match the episode")
            key = _input_lineage_identity(lineage)
            if key in lineage_keys:
                raise ValueError(f"opportunity episode input lineage contains a duplicate: {key!r}")
            lineage_keys.add(key)

        for kind, expression in self.expressions.items():
            if expression.kind is not kind:
                raise ValueError("opportunity episode expression key must match its kind")
            if expression.ticker.strip().upper() != self.ticker:
                raise ValueError("opportunity episode expression ticker must match the episode")

        flagged = [expression for expression in self.expressions.values() if expression.selected]
        if len(flagged) > 1:
            raise ValueError("opportunity episode can select at most one expression")
        selected = self.selected_expression
        if selected is None and flagged:
            selected = flagged[0]
        if selected is not None:
            canonical = self.expressions.get(selected.kind)
            if canonical is None:
                raise ValueError("selected opportunity expression must be in the expression set")
            if canonical != selected:
                raise ValueError("selected opportunity expression must match the expression set")
            self.selected_expression = canonical
        for expression in self.expressions.values():
            expression.selected = selected is not None and expression.kind is selected.kind
        self.cutoff = cutoff
        return self

    @property
    def opportunity_episode_id(self) -> str:
        return self.episode_id

    @property
    def selected_expression_kind(self) -> ExpressionKind | None:
        return self.selected_expression.kind if self.selected_expression else None


class TradePlan(BaseModel):
    """The immutable, point-in-time paper terms for one ticker decision."""

    model_config = ConfigDict(extra="ignore", frozen=True, use_enum_values=False)

    contract_version: str = TRADE_PLAN_CONTRACT_VERSION
    trade_plan_id: str = Field(min_length=1)
    publication_id: str | None = None
    ticker: str = Field(min_length=1)
    opportunity_episode_id: str = Field(min_length=1)
    decision_revision: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    cutoff: datetime
    input_lineage: tuple[InputLineage, ...] = ()
    selected_expression_kind: ExpressionKind
    selected_expression_identity: str = Field(min_length=1)
    selected_expression: TradeExpression
    rank_id: str | None = None
    alpha_signal_id: str | None = None
    portfolio_impact_id: str | None = None
    market_snapshot_id: str | None = None
    market_state_publication_id: str | None = None
    action: str
    eligibility: str
    availability_status: AvailabilityStatus = AvailabilityStatus.MISSING
    authorization_mode: str
    data_quality: str = "UNKNOWN"
    rationale: str = ""
    primary_blocker: str | None = None
    blockers: tuple[str, ...] = ()
    next_action: str = "Refresh and recalculate the decision."
    entry: PriceRange | None = None
    entry_limit: float | None = None
    quantity: int | None = Field(default=None, ge=0)
    max_loss_per_unit: float | None = Field(default=None, ge=0)
    planned_loss: float | None = Field(default=None, ge=0)
    invalidation: Invalidation | None = None
    profit_exit: PriceRange | None = None
    expiry: date | datetime | None = None
    portfolio_impact: PortfolioImpact | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_contract(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        aliases = {
            "id": "trade_plan_id",
            "episode_id": "opportunity_episode_id",
            "expression_kind": "selected_expression_kind",
            "expression_identity": "selected_expression_identity",
            "impact_id": "portfolio_impact_id",
            "snapshot_id": "market_snapshot_id",
            "market_publication_id": "market_state_publication_id",
            "exit": "profit_exit",
            "time_exit": "expiry",
            "size": "quantity",
        }
        for old, new in aliases.items():
            if new not in result and old in result:
                result[new] = result[old]
        if isinstance(result.get("ticker"), str):
            result["ticker"] = result["ticker"].strip().upper()
        for key in ("action", "eligibility", "authorization_mode", "data_quality"):
            if isinstance(result.get(key), str):
                result[key] = result[key].upper().replace("-", "_")
        if "availability_status" not in result:
            result["availability_status"] = (
                AvailabilityStatus.AVAILABLE
                if result.get("eligibility") == "ACTIONABLE"
                else availability_status_for_blockers(
                    (
                        result.get("primary_blocker"),
                        *(result.get("blockers") or ()),
                    )
                )
            )
        return result

    @model_validator(mode="after")
    def enforce_invariants(self) -> "TradePlan":
        if self.contract_version != TRADE_PLAN_CONTRACT_VERSION:
            raise ValueError("unsupported trade plan contract version")
        if not self.input_lineage:
            raise ValueError("trade plan input lineage is required")
        if self.eligibility not in {"ACTIONABLE", "BLOCKED"}:
            raise ValueError("trade plan eligibility must be ACTIONABLE or BLOCKED")
        if self.authorization_mode not in {"NONE", "ADVISORY", "PAPER"}:
            raise ValueError("trade plan authorization mode is not paper-safe")
        if self.data_quality not in {quality.value for quality in DataQuality}:
            raise ValueError("trade plan data quality is invalid")
        if self.cutoff.tzinfo is None:
            raise ValueError("trade plan cutoff must be timezone-aware")
        cutoff = _utc(self.cutoff)
        if any(_utc(item.available_at) > cutoff for item in self.input_lineage):
            raise ValueError("trade plan lineage cannot be newer than its cutoff")
        expression = self.selected_expression
        if expression.ticker.strip().upper() != self.ticker:
            raise ValueError("trade plan expression ticker must match the plan")
        if expression.kind is not self.selected_expression_kind:
            raise ValueError("trade plan expression kind must match the plan")
        valid_identities = {trade_expression_identity(expression)}
        if expression.kind is ExpressionKind.CASH:
            valid_identities.add(_expression_identity_for(
                expression, expression.kind, self.ticker, self.decision_revision,
            ))
        if self.selected_expression_identity not in valid_identities:
            raise ValueError("trade plan expression identity must match the expression")
        if self.portfolio_impact is not None:
            if self.portfolio_impact_id != self.portfolio_impact.impact_id:
                raise ValueError("trade plan portfolio impact id must match the impact")
            if self.portfolio_impact.opportunity_episode_id != self.opportunity_episode_id:
                raise ValueError("trade plan portfolio impact episode must match the plan")
            if normalize_symbol(self.portfolio_impact.ticker) != normalize_symbol(self.ticker):
                raise ValueError("trade plan portfolio impact ticker must match the plan")
            if self.portfolio_impact.decision_revision != self.decision_revision:
                raise ValueError("trade plan portfolio impact revision must match the plan")
            if self.portfolio_impact.risk_policy_version != self.policy_version:
                raise ValueError("trade plan portfolio impact policy must match the plan")
            if self.market_snapshot_id and self.portfolio_impact.market_snapshot_id != self.market_snapshot_id:
                raise ValueError("trade plan portfolio impact snapshot must match the plan")
            if self.market_state_publication_id and self.portfolio_impact.market_state_publication_id != self.market_state_publication_id:
                raise ValueError("trade plan portfolio impact publication must match the plan")
        if self.eligibility == "BLOCKED":
            if self.availability_status is AvailabilityStatus.AVAILABLE:
                raise ValueError("blocked trade plan cannot be available")
            if self.availability_status is not availability_status_for_blockers(
                (self.primary_blocker, *self.blockers)
            ):
                raise ValueError("blocked trade plan availability must match its primary blocker")
            if self.selected_expression_kind is not ExpressionKind.CASH:
                raise ValueError("blocked trade plan must select CASH")
            if self.action != "NO_TRADE":
                raise ValueError("blocked trade plan must be NO_TRADE")
            if self.authorization_mode != "NONE":
                raise ValueError("blocked trade plan cannot be paper authorized")
            if not self.primary_blocker or self.primary_blocker not in self.blockers:
                raise ValueError("blocked trade plan must expose its primary blocker")
            if self.quantity is not None and self.quantity > 0:
                raise ValueError("blocked trade plan cannot contain a positive quantity")
        elif self.eligibility == "ACTIONABLE":
            if self.availability_status is not AvailabilityStatus.AVAILABLE:
                raise ValueError("actionable trade plan requires available status")
            if self.selected_expression_kind is ExpressionKind.CASH:
                raise ValueError("actionable trade plan cannot select CASH")
            if self.action in {"NO_TRADE", "AVOID"}:
                raise ValueError("actionable trade plan cannot be a no-trade action")
            if self.authorization_mode not in {"ADVISORY", "PAPER"}:
                raise ValueError("actionable trade plan requires a supported authorization mode")
            required = {
                "rank_id": self.rank_id,
                "alpha_signal_id": self.alpha_signal_id,
                "portfolio_impact_id": self.portfolio_impact_id,
                "market_snapshot_id": self.market_snapshot_id,
                "market_state_publication_id": self.market_state_publication_id,
                "entry": self.entry,
                "entry_limit": self.entry_limit,
                "quantity": self.quantity,
                "max_loss_per_unit": self.max_loss_per_unit,
                "planned_loss": self.planned_loss,
                "invalidation": self.invalidation,
                "profit_exit": self.profit_exit,
                "expiry": self.expiry,
                "portfolio_impact": self.portfolio_impact,
            }
            missing = [name for name, item in required.items() if item is None]
            if missing:
                raise ValueError("actionable trade plan requires: " + ", ".join(missing))
            if self.quantity <= 0:
                raise ValueError("actionable trade plan requires a positive quantity")
            if self.entry_limit <= 0 or not math.isfinite(self.entry_limit):
                raise ValueError("actionable trade plan requires a finite positive entry limit")
            if self.max_loss_per_unit <= 0 or not math.isfinite(self.max_loss_per_unit):
                raise ValueError("actionable trade plan requires a finite positive maximum loss")
            if self.planned_loss <= 0 or not math.isfinite(self.planned_loss):
                raise ValueError("actionable trade plan requires a finite positive planned loss")
            if self.primary_blocker or self.blockers:
                raise ValueError("actionable trade plan cannot have blockers")
        expected_id = _trade_plan_id(self.model_dump(
            mode="json", exclude={"trade_plan_id", "publication_id", "availability_status"},
        ))
        if self.trade_plan_id != expected_id:
            raise ValueError("trade plan id does not match its immutable terms")
        if self.eligibility == "BLOCKED" and any(
            value is not None for value in (
                self.entry, self.entry_limit, self.quantity, self.max_loss_per_unit,
                self.planned_loss, self.invalidation, self.profit_exit,
            )
        ):
            raise ValueError("blocked trade plan cannot contain executable terms")
        if self.eligibility == "ACTIONABLE":
            expression_terms = {
                "entry": expression.entry_range,
                "entry_limit": _midpoint(expression.entry_range),
                "quantity": expression.quantity,
                "max_loss_per_unit": expression.max_loss_per_unit,
                "planned_loss": expression.planned_loss,
                "invalidation": expression.invalidation,
                "profit_exit": expression.target_range,
            }
            plan_terms = {
                "entry": self.entry,
                "entry_limit": self.entry_limit,
                "quantity": self.quantity,
                "max_loss_per_unit": self.max_loss_per_unit,
                "planned_loss": self.planned_loss,
                "invalidation": self.invalidation,
                "profit_exit": self.profit_exit,
            }
            if _trade_plan_jsonable(plan_terms) != _trade_plan_jsonable(expression_terms):
                raise ValueError("trade plan economic terms must match the selected expression")
        object.__setattr__(self, "cutoff", cutoff)
        return self

    @property
    def resolution_action(self) -> str:
        return self.action

    @property
    def resolution_eligibility(self) -> str:
        return self.eligibility

    @property
    def time_exit(self) -> date | datetime | None:
        return self.expiry

    @property
    def selected_portfolio_impact(self) -> PortfolioImpact | None:
        return self.portfolio_impact


class OutcomeEvidence(BaseModel):
    """One immutable post-decision mark or counterfactual evidence item."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    evidence_id: str = ""
    kind: str = "outcome"
    source_id: str | None = None
    source_version: str | None = None
    observed_at: datetime | None = None
    observed_through: datetime | None = None
    available_at: datetime | None = None
    gross_return: float | None = None
    cost_adjusted_return: float | None = None
    cost_model_version: str | None = None
    evidence_state: str = OutcomeEvidenceState.MISSING.value
    status: str = "unmeasurable"
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        for old, new in {
            "id": "evidence_id",
            "quote_time": "observed_at",
            "mark_quote_time": "observed_at",
            "net_return": "cost_adjusted_return",
            "cost_adjusted_net_return": "cost_adjusted_return",
        }.items():
            if new not in result and old in result:
                result[new] = result[old]
        if isinstance(result.get("kind"), str):
            result["kind"] = result["kind"].upper()
        if isinstance(result.get("evidence_state"), str):
            result["evidence_state"] = result["evidence_state"].upper()
        return result

    @model_validator(mode="after")
    def timestamps_are_timezone_aware(self) -> "OutcomeEvidence":
        for name in ("observed_at", "observed_through", "available_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"outcome evidence {name} must be timezone-aware")
        if self.observed_through is None and self.observed_at is not None:
            object.__setattr__(self, "observed_through", self.observed_at)
        return self

    @property
    def net_return(self) -> float | None:
        return self.cost_adjusted_return

    @property
    def cost_adjusted_net_return(self) -> float | None:
        return self.cost_adjusted_return

    @property
    def quote_time(self) -> datetime | None:
        return self.observed_at


class PaperExecutionOutcome(BaseModel):
    """Immutable snapshot of the exact paper-order evidence available at evaluation."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    trade_plan_id: str = Field(min_length=1)
    paper_order_id: str | None = None
    status: str = "MISSING"
    evidence_state: str = OutcomeEvidenceState.MISSING.value
    paper_only: bool = True
    entry_filled_at: datetime | None = None
    exit_at: datetime | None = None
    entry_fill_price: float | None = None
    exit_price: float | None = None
    filled_quantity: float | None = Field(default=None, ge=0)
    exited_quantity: float | None = Field(default=None, ge=0)
    fees: float | None = Field(default=None, ge=0)
    entry_slippage: float | None = Field(default=None, ge=0)
    exit_slippage: float | None = Field(default=None, ge=0)
    contract_multiplier: float | None = Field(default=None, gt=0)
    entry_fill_count: int | None = Field(default=None, ge=0)
    exit_fill_count: int | None = Field(default=None, ge=0)
    realized_gross_return: float | None = None
    realized_net_return: float | None = None
    observed_through: datetime | None = None
    available_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        for old, new in {
            "order_id": "paper_order_id",
            "fill_at": "entry_filled_at",
            "filled_at": "entry_filled_at",
            "fill_price": "entry_fill_price",
            "entry_price": "entry_fill_price",
            "realized_return": "realized_net_return",
            "quantity": "filled_quantity",
            "slippage": "entry_slippage",
            "multiplier": "contract_multiplier",
            "entry_fills": "entry_fill_count",
            "exit_fills": "exit_fill_count",
        }.items():
            if new not in result and old in result:
                result[new] = result[old]
        if isinstance(result.get("status"), str):
            result["status"] = result["status"].upper()
        if isinstance(result.get("evidence_state"), str):
            result["evidence_state"] = result["evidence_state"].upper()
        return result

    @model_validator(mode="after")
    def enforce_paper_evidence(self) -> "PaperExecutionOutcome":
        if not self.paper_only:
            raise ValueError("outcome execution evidence must be paper-only")
        for name in ("entry_filled_at", "exit_at", "observed_through", "available_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"paper execution {name} must be timezone-aware")
        if self.entry_filled_at is None and self.exit_at is not None:
            raise ValueError("paper execution exit requires an entry fill")
        if (
            self.entry_filled_at is not None and self.exit_at is not None
            and self.exit_at < self.entry_filled_at
        ):
            raise ValueError("paper execution exit cannot precede the entry fill")
        if self.observed_through is None:
            object.__setattr__(self, "observed_through", self.exit_at or self.entry_filled_at)
        return self

    @property
    def actual_fill_price(self) -> float | None:
        return self.entry_fill_price

    @property
    def fill_price(self) -> float | None:
        return self.entry_fill_price

    @property
    def quantity(self) -> float | None:
        return self.filled_quantity

    @property
    def realized_return(self) -> float | None:
        return self.realized_net_return if self.realized_net_return is not None else self.realized_gross_return


class OutcomeAttribution(BaseModel):
    """Frozen, content-addressed outcome authority for one plan and horizon."""

    model_config = ConfigDict(extra="ignore", frozen=True, use_enum_values=False)

    contract_version: str = OUTCOME_ATTRIBUTION_CONTRACT_VERSION
    outcome_attribution_id: str = ""
    stable_unit_key: str = Field(min_length=1)
    publication_id: str | None = None
    evaluation_version: str = OUTCOME_ATTRIBUTION_EVALUATION_VERSION
    ticker: str = Field(min_length=1)
    trade_plan_id: str = Field(min_length=1)
    trade_plan_publication_id: str = Field(min_length=1)
    opportunity_episode_id: str = Field(min_length=1)
    decision_revision: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    selected_expression_kind: ExpressionKind
    selected_expression_identity: str = Field(min_length=1)
    rank_id: str | None = None
    alpha_signal_id: str | None = None
    portfolio_impact_id: str | None = None
    market_snapshot_id: str | None = None
    market_state_publication_id: str | None = None
    decision_cutoff: datetime
    evaluation_cutoff: datetime
    decision_input_lineage: tuple[InputLineage, ...] = ()
    horizon: Horizon
    horizon_sessions: int = Field(gt=0)
    state: OutcomeAttributionState = OutcomeAttributionState.UNMEASURABLE
    observed_through: datetime | None = None
    available_at: datetime | None = None
    outcome_evidence: tuple[OutcomeEvidence, ...] = ()
    selected_evidence: OutcomeEvidence | None = None
    selected_gross_return: float | None = None
    selected_net_return: float | None = None
    realized_gross_return: float | None = None
    realized_net_return: float | None = None
    counterfactuals: dict[str, OutcomeEvidence] = Field(default_factory=dict)
    all_expression_counterfactuals: dict[str, OutcomeEvidence] = Field(default_factory=dict)
    cost_model_version: str = "mixed-expression-cost-model-v1"
    evidence_state: str = OutcomeEvidenceState.MISSING.value
    paper_execution: PaperExecutionOutcome | None = None
    sample_eligible: bool = False
    promotion_eligible: bool = False
    primary_blocker: str | None = None
    next_action: str = "Collect exact post-decision evidence before using this outcome."
    mistake_classification: str | None = None
    mistake_card: dict[str, Any] = Field(default_factory=dict)
    learning_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        for old, new in {
            "id": "outcome_attribution_id",
            "stable_key": "stable_unit_key",
            "trade_plan_cutoff": "decision_cutoff",
            "cutoff": "decision_cutoff",
            "input_lineage": "decision_input_lineage",
            "evaluation_reference": "evaluation_cutoff",
            "evaluation_as_of": "evaluation_cutoff",
            "evidence": "outcome_evidence",
            "paper_execution_outcome": "paper_execution",
            "blocker": "primary_blocker",
            "learning": "learning_metadata",
            "selected_return": "selected_gross_return",
            "selected_cost_adjusted_return": "selected_net_return",
            "expression_outcomes": "all_expression_counterfactuals",
            "all_expression_outcomes": "all_expression_counterfactuals",
        }.items():
            if new not in result and old in result:
                result[new] = result[old]
        if isinstance(result.get("ticker"), str):
            result["ticker"] = result["ticker"].strip().upper()
        for key in ("selected_expression_kind", "horizon", "state", "evidence_state"):
            if isinstance(result.get(key), str):
                result[key] = result[key].upper()
        for key in ("counterfactuals", "all_expression_counterfactuals"):
            raw = result.get(key)
            if isinstance(raw, Mapping):
                result[key] = {
                    str(name).upper(): value if isinstance(value, (Mapping, BaseModel)) else {
                        "kind": str(name).upper(), "gross_return": value,
                        "evidence_state": OutcomeEvidenceState.DERIVED.value,
                    }
                    for name, value in raw.items()
                }
        return result

    @model_validator(mode="after")
    def enforce_invariants(self) -> "OutcomeAttribution":
        if self.contract_version != OUTCOME_ATTRIBUTION_CONTRACT_VERSION:
            raise ValueError("unsupported outcome attribution contract version")
        if self.evaluation_version != OUTCOME_ATTRIBUTION_EVALUATION_VERSION:
            raise ValueError("unsupported outcome attribution evaluation version")
        if self.decision_cutoff.tzinfo is None or self.evaluation_cutoff.tzinfo is None:
            raise ValueError("outcome attribution cutoffs must be timezone-aware")
        decision_cutoff = _utc(self.decision_cutoff)
        evaluation_cutoff = _utc(self.evaluation_cutoff)
        if evaluation_cutoff < decision_cutoff:
            raise ValueError("evaluation cutoff cannot precede the decision cutoff")
        if self.stable_unit_key != outcome_attribution_stable_key(
            self.trade_plan_id, self.horizon, self.horizon_sessions,
        ):
            raise ValueError("outcome attribution stable unit key is not plan-bound")
        evidence = list(self.outcome_evidence)
        if self.selected_evidence is not None and self.selected_evidence not in evidence:
            evidence.append(self.selected_evidence)
        evidence.extend(self.counterfactuals.values())
        evidence.extend(self.all_expression_counterfactuals.values())
        for item in evidence:
            if item.gross_return is not None and item.kind != "CASH" and (
                item.observed_at is None or item.available_at is None
            ):
                raise ValueError("return evidence requires observed and available-at timestamps")
            for name in ("observed_at", "observed_through", "available_at"):
                value = getattr(item, name)
                if value is not None:
                    normalized = _utc(value)
                    if normalized > evaluation_cutoff:
                        raise ValueError("outcome evidence is available after the evaluation cutoff")
                    if name != "available_at" and normalized <= decision_cutoff:
                        raise ValueError("outcome evidence must be observed after the decision cutoff")
            if item.available_at is not None and _utc(item.available_at) > evaluation_cutoff:
                raise ValueError("outcome evidence is available after the evaluation cutoff")
        if self.available_at is not None:
            if self.available_at.tzinfo is None:
                raise ValueError("outcome attribution available_at must be timezone-aware")
            if _utc(self.available_at) > evaluation_cutoff:
                raise ValueError("outcome attribution is available after the evaluation cutoff")
        if self.observed_through is not None:
            if self.observed_through.tzinfo is None:
                raise ValueError("outcome attribution observed_through must be timezone-aware")
            if _utc(self.observed_through) > evaluation_cutoff:
                raise ValueError("outcome attribution observed_through is after the evaluation cutoff")
        if self.paper_execution is not None:
            if self.paper_execution.trade_plan_id != self.trade_plan_id:
                raise ValueError("paper execution must reference the exact trade plan")
            if (
                self.paper_execution.entry_filled_at is not None
                and _utc(self.paper_execution.entry_filled_at) <= decision_cutoff
            ):
                raise ValueError("paper execution entry must be after the decision cutoff")
            for value in (self.paper_execution.observed_through, self.paper_execution.available_at):
                if value is not None and _utc(value) > evaluation_cutoff:
                    raise ValueError("paper execution evidence is available after the evaluation cutoff")
        if self.paper_execution is None and any(
            value is not None for value in (self.realized_gross_return, self.realized_net_return)
        ):
            raise ValueError("realized outcome requires exact paper execution evidence")
        if self.paper_execution is not None and self.paper_execution.status != "EXITED" and any(
            value is not None for value in (self.realized_gross_return, self.realized_net_return)
        ):
            raise ValueError("unfinished paper execution cannot provide realized outcome")
        if self.promotion_eligible and not self.sample_eligible:
            raise ValueError("promotion eligibility requires sample eligibility")
        if self.sample_eligible or self.promotion_eligible:
            execution = self.paper_execution
            if self.state is not OutcomeAttributionState.RESOLVED or execution is None:
                raise ValueError("eligible attribution requires a resolved paper execution")
            if (
                execution.status != "EXITED"
                or execution.entry_filled_at is None or execution.exit_at is None
                or execution.entry_fill_price is None or execution.exit_price is None
                or execution.entry_fill_count != 1 or execution.exit_fill_count != 1
                or not execution.filled_quantity or not execution.exited_quantity
                or execution.realized_gross_return is None or execution.realized_net_return is None
            ):
                raise ValueError("eligible attribution requires one provable paper fill and exit")
        object.__setattr__(self, "decision_cutoff", decision_cutoff)
        object.__setattr__(self, "evaluation_cutoff", evaluation_cutoff)
        expected_id = _outcome_attribution_id(
            self.model_dump(mode="python", exclude={"outcome_attribution_id", "publication_id"}),
        )
        if self.outcome_attribution_id and self.outcome_attribution_id != expected_id:
            raise ValueError("outcome attribution id does not match its immutable content")
        object.__setattr__(self, "outcome_attribution_id", expected_id)
        return self

    @property
    def cutoff(self) -> datetime:
        return self.decision_cutoff

    @property
    def trade_plan_cutoff(self) -> datetime:
        return self.decision_cutoff

    @property
    def evaluation_reference(self) -> datetime:
        return self.evaluation_cutoff

    @property
    def input_lineage(self) -> tuple[InputLineage, ...]:
        return self.decision_input_lineage

    @property
    def evidence(self) -> tuple[OutcomeEvidence, ...]:
        return self.outcome_evidence

    @property
    def selected_cost_adjusted_return(self) -> float | None:
        return self.selected_net_return


def outcome_attribution_stable_key(
    trade_plan_id: str, horizon: Horizon | str, horizon_sessions: int,
) -> str:
    return f"{trade_plan_id}:{getattr(horizon, 'value', horizon)}:{int(horizon_sessions)}"


def outcome_attribution_id(payload: Mapping[str, Any]) -> str:
    """Return the content identity while excluding publication envelope metadata."""

    return _outcome_attribution_id(payload)


def opportunity_episode_id(
    ticker: str,
    decision_revision: str | None = None,
    *,
    thesis_identity: str | None = None,
) -> str:
    """Return the durable economic-episode identity.

    ``decision_revision`` remains a compatibility argument for V1 callers. It
    is intentionally not included in the identity, so signal revisions and
    expression changes continue one episode. A caller may provide a stable
    thesis identity when more than one thesis exists for a ticker.
    """
    stable_thesis = thesis_identity or f"ticker:{ticker.strip().upper()}"
    encoded = json.dumps(
        {"ticker": ticker.strip().upper(), "thesis_identity": stable_thesis},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{OPPORTUNITY_EPISODE_CONTRACT_VERSION}:{hashlib.sha256(encoded.encode()).hexdigest()[:24]}"


def build_opportunity_episode(
    *,
    ticker: str,
    decision_revision: str,
    policy_version: str,
    cutoff: datetime,
    input_lineage: Iterable[InputLineage | Mapping[str, Any]],
    expressions: Mapping[ExpressionKind | str, TradeExpression | Mapping[str, Any]],
    selected_expression: TradeExpression | ExpressionKind | str | None = None,
    episode_id: str | None = None,
    thesis_identity: str | None = None,
    first_seen_at: datetime | None = None,
    last_updated_at: datetime | None = None,
    status: OpportunityEpisodeStatus = OpportunityEpisodeStatus.DISCOVERED,
    horizon: Horizon | None = None,
    catalyst_window: str | None = None,
    closed_reason: str | None = None,
    superseded_by: str | None = None,
) -> OpportunityEpisode:
    canonical_expressions = {
        ExpressionKind(kind): trade_expression_from_legacy(expression)
        for kind, expression in expressions.items()
    }
    selected_kind: ExpressionKind | None = None
    if selected_expression is not None:
        selected_kind = (
            selected_expression.kind
            if isinstance(selected_expression, ExpressionDecision)
            else ExpressionKind(selected_expression.get("kind"))
            if isinstance(selected_expression, Mapping)
            else ExpressionKind(selected_expression)
        )
    canonical_thesis = thesis_identity or f"ticker:{ticker.strip().upper()}"
    canonical_episode_id = episode_id or opportunity_episode_id(
        ticker, decision_revision, thesis_identity=canonical_thesis,
    )
    canonical_lineage = []
    for item in input_lineage:
        lineage = item if isinstance(item, InputLineage) else InputLineage.model_validate(item)
        canonical_lineage.append(lineage.model_copy(update={
            "opportunity_episode_id": canonical_episode_id,
            "decision_revision": lineage.decision_revision or decision_revision,
            "policy_version": lineage.policy_version or policy_version,
            "cutoff": lineage.cutoff or cutoff,
        }))
    return OpportunityEpisode(
        episode_id=canonical_episode_id,
        ticker=ticker,
        decision_revision=decision_revision,
        policy_version=policy_version,
        cutoff=cutoff,
        input_lineage=canonical_lineage,
        expressions=canonical_expressions,
        selected_expression=(
            canonical_expressions.get(selected_kind) if selected_kind is not None else None
        ),
        thesis_identity=canonical_thesis,
        first_seen_at=first_seen_at or cutoff,
        last_updated_at=last_updated_at or cutoff,
        status=status,
        horizon=horizon or (
            canonical_expressions.get(selected_kind).horizon
            if selected_kind is not None and canonical_expressions.get(selected_kind) is not None
            else None
        ),
        catalyst_window=catalyst_window,
        current_revision=decision_revision,
        closed_reason=closed_reason,
        superseded_by=superseded_by,
    )


def opportunity_episode_from_legacy(value: Any) -> OpportunityEpisode:
    """Adapt an old ticker decision into the canonical episode boundary."""

    if isinstance(value, OpportunityEpisode):
        return value
    raw = value.model_dump(mode="python") if isinstance(value, BaseModel) else dict(value or {})
    if "opportunity_episode" in raw and raw["opportunity_episode"] is not None:
        return OpportunityEpisode.model_validate(raw["opportunity_episode"])

    ticker = str(raw.get("ticker") or raw.get("symbol") or "").strip().upper()
    decision_revision = str(raw.get("decision_revision") or "").strip()
    if not ticker or not decision_revision:
        raise ValueError("legacy ticker decision is missing episode identity")
    resolution = raw.get("resolution") or {}
    risk_policy = raw.get("risk_policy") or {}
    policy_version = str(
        raw.get("policy_version")
        or (resolution.get("policy_version") if isinstance(resolution, Mapping) else None)
        or (risk_policy.get("policy_version") if isinstance(risk_policy, Mapping) else None)
        or "risk-policy.v2:legacy"
    )
    cutoff = _parse_datetime(raw.get("cutoff") or raw.get("as_of"))
    if cutoff is None:
        raise ValueError("legacy ticker decision is missing an episode cutoff")

    raw_expressions = raw.get("expressions")
    if not isinstance(raw_expressions, Mapping) or not raw_expressions:
        raise ValueError("legacy ticker decision is missing expressions")
    expressions: dict[ExpressionKind, TradeExpression] = {}
    for raw_kind, raw_expression in raw_expressions.items():
        expression = trade_expression_from_legacy(raw_expression)
        kind = ExpressionKind(raw_kind)
        if expression.kind is not kind:
            raise ValueError("legacy expression key does not match its kind")
        expressions[kind] = expression

    selected_raw = raw.get("selected_expression")
    selected_kind: ExpressionKind | None = None
    if isinstance(selected_raw, Mapping):
        selected_kind = ExpressionKind(str(selected_raw.get("kind") or ""))
    elif selected_raw:
        selected_kind = ExpressionKind(str(selected_raw))
    if selected_kind is None:
        flagged = [expression.kind for expression in expressions.values() if expression.selected]
        if len(flagged) > 1:
            raise ValueError("legacy ticker decision selects multiple expressions")
        selected_kind = flagged[0] if flagged else None

    raw_lineage = raw.get("input_lineage")
    if raw_lineage is None and "lineage" in raw:
        raw_lineage = raw.get("lineage")
    manifest = raw.get("input_manifest") or {}
    if raw_lineage is None and isinstance(manifest, Mapping) and "inputs" in manifest:
        raw_lineage = []
        source_versions = manifest.get("source_versions") or {}
        for field, values in (manifest.get("inputs") or {}).items():
            values = values if isinstance(values, list) else [values]
            for item in values:
                if not isinstance(item, Mapping):
                    raise ValueError("legacy ticker decision contains invalid input lineage")
                row = dict(item)
                source_id = str(
                    row.get("source_id") or row.get("source") or row.get("provider") or field
                )
                row.update({
                    "field": str(field),
                    "source_id": source_id,
                    "source_version": row.get("source_version")
                    or row.get("revision")
                    or row.get("version")
                    or source_versions.get(source_id)
                    or "unknown",
                    "opportunity_episode_id": raw.get("episode_id"),
                    "decision_revision": decision_revision,
                    "policy_version": policy_version,
                    "cutoff": cutoff,
                })
                raw_lineage.append(row)
    if raw_lineage is None:
        raise ValueError("legacy ticker decision is missing input lineage")
    if not raw_lineage:
        raise ValueError("legacy ticker decision is missing input lineage")

    return build_opportunity_episode(
        ticker=ticker,
        decision_revision=decision_revision,
        policy_version=policy_version,
        cutoff=cutoff,
        input_lineage=raw_lineage,
        expressions=expressions,
        selected_expression=selected_kind,
        episode_id=raw.get("episode_id") or raw.get("opportunity_episode_id"),
    )


class HorizonDecision(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    horizon: Horizon
    stance: Stance
    action: CapitalActionType
    current_price: float | None = Field(default=None, ge=0)
    entry_range: PriceRange | None = None
    target_range: PriceRange | None = None
    expiry_date: date
    scenarios: list[ScenarioOutcome]
    invalidation: Invalidation | None = None
    conviction_tier: str = Field(pattern="^(EXPLORATORY|STANDARD|HIGH)$")
    confidence: float | None = Field(default=None, ge=0, le=1)
    confidence_basis: list[str] = Field(default_factory=list)
    expected_return_range: NumericRange | None = None
    evidence_for: list[EvidenceItem] = Field(default_factory=list)
    evidence_against: list[EvidenceItem] = Field(default_factory=list)
    fact_that_would_flip: EvidenceItem
    selected_instrument: ExpressionKind
    alternate_expression: ExpressionKind

    @model_validator(mode="after")
    def complete_scenarios(self) -> "HorizonDecision":
        names = {scenario.name for scenario in self.scenarios}
        if names != {"bear", "base", "bull"}:
            raise ValueError("scenarios must contain bear, base, and bull")
        probabilities = [scenario.probability for scenario in self.scenarios]
        if any(value is not None for value in probabilities) and (
            any(value is None for value in probabilities)
            or not math.isclose(sum(value or 0.0 for value in probabilities), 1.0, abs_tol=1e-6)
        ):
            raise ValueError("scenario probabilities must total 100 percent")
        return self


class CapitalAction(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    ticker: str
    action: CapitalActionType
    owned: bool
    rationale: str
    price_condition: str | None = None
    catalyst: str | None = None
    expires_at: date | None = None

    @model_validator(mode="after")
    def waiting_action_is_exact(self) -> "CapitalAction":
        if self.action is CapitalActionType.WAIT_FOR_PRICE:
            if not self.price_condition or not self.catalyst or self.expires_at is None:
                raise ValueError("WAIT_FOR_PRICE requires a price, catalyst, and expiry")
        if self.action.value == "Watch" or self.action.value.lower() == "watch":
            raise ValueError("Watch is not a capital action")
        return self


def capital_action_from_resolution(resolution: DecisionResolutionV2) -> CapitalAction:
    """Project the old CapitalAction envelope from the canonical resolution."""

    action_name = "AVOID" if resolution.is_blocked else resolution.action
    try:
        action = CapitalActionType(action_name)
    except ValueError:
        action = CapitalActionType.AVOID
    expires_at = resolution.expires_at
    if isinstance(expires_at, datetime):
        expires_at = expires_at.date()
    if action is CapitalActionType.WAIT_FOR_PRICE:
        return CapitalAction(
            ticker=str(resolution.ticker or ""), action=action, owned=resolution.owned,
            rationale=resolution.rationale, price_condition=resolution.price_condition or "collect a confirmed price",
            catalyst=resolution.catalyst or "next decision catalyst or confirmed price update",
            expires_at=expires_at or date.today(),
        )
    return CapitalAction(
        ticker=str(resolution.ticker or ""), action=action, owned=resolution.owned,
        rationale=resolution.rationale, price_condition=resolution.price_condition,
        catalyst=resolution.catalyst, expires_at=expires_at,
    )


class TickerDecision(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    decision_contract_version: str = CONTRACT_VERSION
    ticker: str
    as_of: datetime
    decision_revision: str
    tactical: HorizonDecision
    fundamental: HorizonDecision
    capital_action: CapitalAction
    resolution: DecisionResolutionV2 | None = None
    policy_version: str = "risk-policy.v2:legacy"
    risk_policy: RiskPolicy
    expressions: dict[ExpressionKind, ExpressionDecision]
    selected_expression: ExpressionDecision | None = None
    opportunity_episode: OpportunityEpisode | None = None
    data_requests: list[DataRequest] = Field(default_factory=list)
    learning_history: list[dict[str, Any]] = Field(default_factory=list)
    input_manifest: InputManifest
    risk_policy_snapshot: RiskPolicySnapshot | None = None
    market_state_publication_id: str | None = None
    market_state_snapshot: MarketStateSnapshot | None = None
    market_evidence_assessment: MarketEvidenceAssessment | None = None
    portfolio_impacts: dict[ExpressionKind, PortfolioImpact] = Field(default_factory=dict)
    instrument_state_snapshot: dict[str, Any] | None = None
    alpha_signals: list[dict[str, Any]] = Field(default_factory=list)
    opportunity_rank: dict[str, Any] | None = None
    trade_plan: TradePlan | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_policy_version(cls, value: Any) -> Any:
        if not isinstance(value, Mapping) or "policy_version" in value:
            return value
        resolution = value.get("resolution") or {}
        risk_policy = value.get("risk_policy") or {}
        result = dict(value)
        resolution_version = (
            resolution.get("policy_version")
            if isinstance(resolution, Mapping)
            else getattr(resolution, "policy_version", None)
        )
        risk_policy_version = (
            risk_policy.get("policy_version")
            if isinstance(risk_policy, Mapping)
            else getattr(risk_policy, "policy_version", None)
        )
        result["policy_version"] = (
            resolution_version or risk_policy_version or "risk-policy.v2:legacy"
        )
        return result

    @model_validator(mode="after")
    def resolution_is_authority(self) -> "TickerDecision":
        if self.resolution is None:
            return self
        if self.resolution.decision_revision != self.decision_revision:
            raise ValueError("ticker resolution revision must match the ticker decision")
        if self.resolution.policy_version != self.policy_version:
            raise ValueError("ticker resolution policy must match the ticker decision")
        self.capital_action = capital_action_from_resolution(self.resolution)
        return self

    @model_validator(mode="after")
    def opportunity_episode_is_authority(self) -> "TickerDecision":
        if self.opportunity_episode is None:
            self.opportunity_episode = opportunity_episode_from_legacy(self)
        else:
            episode = self.opportunity_episode
            if episode.ticker != self.ticker.strip().upper():
                raise ValueError("opportunity episode ticker must match the ticker decision")
            if episode.decision_revision != self.decision_revision:
                raise ValueError("opportunity episode revision must match the ticker decision")
            if episode.policy_version != self.policy_version:
                raise ValueError("opportunity episode policy must match the ticker decision")
            if _utc(episode.cutoff) != _utc(self.as_of):
                raise ValueError("opportunity episode cutoff must match the ticker decision")
            if set(episode.expressions) != set(self.expressions):
                raise ValueError("opportunity episode expressions must match the ticker decision")
            for kind in self.expressions:
                if episode.expressions[kind] != self.expressions[kind]:
                    raise ValueError("opportunity episode expressions must be the ticker decision expressions")
            selected_kind = self.selected_expression.kind if self.selected_expression else None
            episode_kind = episode.selected_expression.kind if episode.selected_expression else None
            if selected_kind is not episode_kind:
                raise ValueError("opportunity episode selected expression must match the ticker decision")
            self.expressions = dict(self.opportunity_episode.expressions)
        self.selected_expression = self.opportunity_episode.selected_expression
        return self

    @model_validator(mode="after")
    def portfolio_context_is_authority(self) -> "TickerDecision":
        snapshot = self.market_state_snapshot or _missing_market_snapshot(self.cutoff)
        if _utc(snapshot.input_cutoff) > _utc(self.cutoff):
            raise ValueError("market snapshot cutoff cannot be newer than the ticker decision")
        self.market_state_snapshot = snapshot
        self.market_state_publication_id = self.market_state_publication_id or snapshot.publication_id

        policy = self.risk_policy_snapshot
        if policy is None:
            policy = RiskPolicySnapshot(
                policy_version=self.policy_version,
                ticker_loss_budget_pct=self.risk_policy.loss_budget_pct,
                ticker_max_loss_pct=self.risk_policy.max_ticker_loss_pct,
                ticker_total_open_loss_pct=self.risk_policy.max_total_open_planned_loss_pct,
                ticker_position_limit_pct=self.risk_policy.position_limit_pct,
                blockers=("risk_policy_snapshot_missing",),
            )
        if policy.policy_version != self.policy_version:
            raise ValueError("risk policy snapshot version must match the ticker decision")
        self.risk_policy_snapshot = policy

        expected = dict(self.expressions)
        if ExpressionKind.CASH not in expected:
            expected[ExpressionKind.CASH] = _cash_expression(self.ticker, self.as_of, self.input_manifest.input_hash)
        normalized: dict[ExpressionKind, PortfolioImpact] = {}
        for raw_kind, raw_impact in self.portfolio_impacts.items():
            kind = ExpressionKind(raw_kind)
            if kind in normalized:
                raise ValueError("ticker decision contains duplicate portfolio impact keys")
            normalized[kind] = raw_impact if isinstance(raw_impact, PortfolioImpact) else PortfolioImpact.model_validate(raw_impact)
        unexpected = set(normalized) - set(expected)
        if unexpected:
            raise ValueError("ticker decision contains an impact for an unknown expression")
        safe_expressions: dict[ExpressionKind, ExpressionDecision] | None = None
        if policy.blockers:
            cash = _cash_expression(self.ticker, self.cutoff, self.input_manifest.input_hash).model_copy(
                update={"selected": True}
            )
            safe_expressions = {
                kind: (
                    cash
                    if kind is ExpressionKind.CASH
                    else expression.model_copy(update={
                        "entry_range": None,
                        "target_range": None,
                        "invalidation": None,
                        "quantity": None,
                        "loss_budget": None,
                        "max_loss_per_unit": None,
                        "planned_loss": None,
                        "legs": [],
                        "selected": False,
                        "status": "blocked",
                    })
                )
                for kind, expression in expected.items()
            }
        for kind, expression in expected.items():
            impact = normalized.get(kind)
            if impact is None:
                impact = _missing_portfolio_impact(
                    episode=self.opportunity_episode,
                    expression=expression,
                    snapshot=snapshot,
                    policy_version=self.policy_version,
                )
            if impact.opportunity_episode_id != self.opportunity_episode_id:
                raise ValueError("portfolio impact episode must match the ticker decision")
            if normalize_symbol(impact.ticker) != normalize_symbol(self.ticker):
                raise ValueError("portfolio impact ticker must match the ticker decision")
            if impact.expression_kind is not kind:
                raise ValueError("portfolio impact expression kind must match its key")
            if impact.decision_revision != self.decision_revision:
                raise ValueError("portfolio impact revision must match the ticker decision")
            if impact.risk_policy_version != self.policy_version:
                raise ValueError("portfolio impact policy must match the ticker decision")
            if impact.market_snapshot_id != snapshot.snapshot_id:
                raise ValueError("portfolio impact snapshot must match the ticker decision")
            if impact.market_state_publication_id != self.market_state_publication_id:
                raise ValueError("portfolio impact publication must match the ticker decision")
            if _utc(impact.cutoff) != _utc(self.cutoff):
                raise ValueError("portfolio impact cutoff must match the ticker decision")
            if tuple(impact.input_lineage) != tuple(self.input_lineage):
                raise ValueError("portfolio impact lineage must match the ticker decision")
            expected_identity = _expression_identity_for(expression, kind, self.ticker, self.decision_revision)
            if impact.expression_identity != expected_identity:
                safe_expression = safe_expressions.get(kind) if safe_expressions is not None else None
                if (
                    safe_expression is None
                    or impact.expression_identity
                    != _expression_identity_for(safe_expression, kind, self.ticker, self.decision_revision)
                ):
                    raise ValueError("portfolio impact expression identity must match the expression")
            normalized[kind] = impact
        self.portfolio_impacts = normalized
        if policy.blockers:
            assert safe_expressions is not None
            self.expressions = safe_expressions
            self.selected_expression = cash
            self.opportunity_episode = build_opportunity_episode(
                ticker=self.ticker,
                decision_revision=self.decision_revision,
                policy_version=self.policy_version,
                cutoff=self.cutoff,
                input_lineage=self.input_lineage,
                expressions=safe_expressions,
                selected_expression=ExpressionKind.CASH,
                episode_id=self.opportunity_episode_id,
            )
            normalized = {
                kind: impact.model_copy(update={
                    "expression_identity": _expression_identity_for(
                        expression, kind, self.ticker, self.decision_revision
                    ),
                })
                for kind, impact in normalized.items()
                for expression in [safe_expressions[kind]]
            }
            normalized = {
                kind: impact.model_copy(update={"impact_id": _portfolio_impact_id(impact)})
                for kind, impact in normalized.items()
            }
            self.portfolio_impacts = normalized
            expected = dict(safe_expressions)
        context_blockers = _context_blockers_for(
            snapshot=snapshot,
            policy=policy,
            impacts=normalized,
            expressions=expected,
        )
        if policy.blockers:
            existing = self.resolution
            safe_trade_plan = self.trade_plan
            if safe_trade_plan is not None and (
                safe_trade_plan.action != "NO_TRADE"
                or safe_trade_plan.eligibility != "BLOCKED"
                or safe_trade_plan.selected_expression_kind is not ExpressionKind.CASH
                or any(
                    value is not None
                    for value in (
                        safe_trade_plan.entry,
                        safe_trade_plan.entry_limit,
                        safe_trade_plan.quantity,
                        safe_trade_plan.max_loss_per_unit,
                        safe_trade_plan.planned_loss,
                        safe_trade_plan.invalidation,
                        safe_trade_plan.profit_exit,
                    )
                )
            ):
                safe_trade_plan = None
            if existing is not None:
                blocker = existing.primary_blocker or context_blockers[0]
                self.resolution = build_decision_resolution(
                    action="NO_TRADE",
                    decision_revision=self.decision_revision,
                    policy_version=self.policy_version,
                    trade_plan_id=safe_trade_plan.trade_plan_id if safe_trade_plan else None,
                    provenance=existing.provenance,
                    ticker=self.ticker,
                    blockers=tuple(dict.fromkeys((*existing.blockers, *context_blockers))),
                    ttl=existing.ttl,
                    portfolio_context=normalized[ExpressionKind.CASH].model_dump(mode="json"),
                    data_quality="INCOMPLETE",
                    authorization_mode="NONE",
                    rationale=existing.rationale,
                    owned=existing.owned,
                    catalyst=existing.catalyst,
                    expires_at=existing.expires_at,
                    blocked=True,
                )
            else:
                self.resolution = build_decision_resolution(
                    action="NO_TRADE",
                    decision_revision=self.decision_revision,
                    policy_version=self.policy_version,
                    ticker=self.ticker,
                    blockers=context_blockers,
                    provenance={},
                    portfolio_context=normalized[ExpressionKind.CASH].model_dump(mode="json"),
                    data_quality="INCOMPLETE",
                    authorization_mode="NONE",
                    rationale=self.capital_action.rationale,
                    owned=self.capital_action.owned,
                    price_condition=self.capital_action.price_condition,
                    catalyst=self.capital_action.catalyst,
                    expires_at=self.capital_action.expires_at,
                    blocked=True,
                )
            self.capital_action = capital_action_from_resolution(self.resolution)
            self.trade_plan = safe_trade_plan
        elif self.resolution is not None and self.resolution.is_actionable and context_blockers:
            self.resolution = build_decision_resolution(
                action="NO_TRADE",
                decision_revision=self.decision_revision,
                policy_version=self.policy_version,
                trade_plan_id=self.resolution.trade_plan_id,
                provenance=self.resolution.provenance,
                ticker=self.ticker,
                blockers=tuple(dict.fromkeys((*self.resolution.blockers, *context_blockers))),
                data_quality="INCOMPLETE",
                authorization_mode="NONE",
                rationale=self.resolution.rationale,
                owned=self.resolution.owned,
                price_condition=self.resolution.price_condition,
                catalyst=self.resolution.catalyst,
                expires_at=self.resolution.expires_at,
                blocked=True,
            )
            self.capital_action = capital_action_from_resolution(self.resolution)
        return self

    @model_validator(mode="after")
    def bind_market_evidence_assessment(self) -> "TickerDecision":
        """Bind evidence after policy and portfolio gates finalize selection."""

        selected = self.selected_expression
        self.market_evidence_assessment = market_evidence_for_decision(
            self.market_state_snapshot,
            selected.kind if selected is not None else ExpressionKind.CASH,
            selected.horizon if selected is not None else Horizon.FUNDAMENTAL,
        )
        return self

    @model_validator(mode="after")
    def trade_plan_is_authority(self) -> "TickerDecision":
        plan = self.trade_plan
        if plan is None:
            return self
        if plan.ticker != self.ticker.strip().upper():
            raise ValueError("trade plan ticker must match the ticker decision")
        if plan.opportunity_episode_id != self.opportunity_episode_id:
            raise ValueError("trade plan episode must match the ticker decision")
        if plan.decision_revision != self.decision_revision:
            raise ValueError("trade plan revision must match the ticker decision")
        if plan.policy_version != self.policy_version:
            raise ValueError("trade plan policy must match the ticker decision")
        if _utc(plan.cutoff) != _utc(self.cutoff):
            raise ValueError("trade plan cutoff must match the ticker decision")
        if plan.input_lineage != tuple(self.input_lineage):
            raise ValueError("trade plan lineage must match the ticker decision")
        selected = self.selected_expression
        if selected is None or plan.selected_expression.model_dump(mode="json") != selected.model_dump(mode="json"):
            raise ValueError("trade plan expression must match the ticker decision")
        resolution = self.resolution
        if resolution is None or resolution.trade_plan_id != plan.trade_plan_id:
            raise ValueError("ticker resolution must reference the trade plan")
        resolution_terms = {
            "action": getattr(resolution.action, "value", resolution.action),
            "eligibility": getattr(resolution.eligibility, "value", resolution.eligibility),
            "authorization_mode": getattr(resolution.authorization_mode, "value", resolution.authorization_mode),
            "data_quality": getattr(resolution.data_quality, "value", resolution.data_quality),
            "rationale": resolution.rationale,
            "primary_blocker": resolution.primary_blocker,
            "blockers": resolution.blockers,
            "next_action": resolution.next_action,
            "entry": resolution.entry,
            "size": resolution.size,
            "invalidation": resolution.invalidation,
            "exit": resolution.exit,
            "ttl": resolution.ttl,
            "expires_at": resolution.expires_at,
            "portfolio_context": resolution.portfolio_context,
        }
        plan_terms = {
            "action": plan.action,
            "eligibility": plan.eligibility,
            "authorization_mode": plan.authorization_mode,
            "data_quality": plan.data_quality,
            "rationale": plan.rationale,
            "primary_blocker": plan.primary_blocker,
            "blockers": plan.blockers,
            "next_action": plan.next_action,
            "entry": plan.entry,
            "size": plan.quantity,
            "invalidation": plan.invalidation,
            "exit": plan.profit_exit,
            "ttl": plan.expiry,
            "expires_at": plan.expiry,
            "portfolio_context": plan.portfolio_impact.model_dump(mode="json") if plan.portfolio_impact else None,
        }
        if _trade_plan_jsonable(resolution_terms) != _trade_plan_jsonable(plan_terms):
            raise ValueError("ticker resolution terms must match the trade plan")
        impact = self.portfolio_impacts.get(plan.selected_expression_kind)
        if impact is None or plan.portfolio_impact_id != impact.impact_id:
            raise ValueError("trade plan impact must match the ticker decision")
        if self.market_state_snapshot is not None:
            if plan.market_snapshot_id and plan.market_snapshot_id != self.market_state_snapshot.snapshot_id:
                raise ValueError("trade plan market snapshot must match the ticker decision")
            if plan.market_state_publication_id and plan.market_state_publication_id != self.market_state_publication_id:
                raise ValueError("trade plan market publication must match the ticker decision")
        return self

    @property
    def opportunity_episode_id(self) -> str:
        return self.opportunity_episode.episode_id if self.opportunity_episode else ""

    @property
    def cutoff(self) -> datetime:
        return self.opportunity_episode.cutoff if self.opportunity_episode else _utc(self.as_of)

    @property
    def input_lineage(self) -> list[InputLineage]:
        return list(self.opportunity_episode.input_lineage) if self.opportunity_episode else []

    @property
    def context_blockers(self) -> tuple[str, ...]:
        return tuple(_context_blockers_for(
            snapshot=self.market_state_snapshot,
            policy=self.risk_policy_snapshot,
            impacts=self.portfolio_impacts,
            expressions=self.expressions,
        ))


def apply_opportunity_rank_safety(
    decision: TickerDecision,
    rank: Mapping[str, Any] | None,
) -> TickerDecision:
    """Make cash the only selected expression when book rank is not current."""

    payload = dict(rank or {})
    trade_rank = payload.get("trade_rank")
    utility = payload.get("trade_utility")
    if isinstance(utility, Mapping):
        utility = utility.get("trade_utility")
    reason = str(payload.get("trade_rank_unavailable_reason") or "").strip()
    try:
        trade_rank_value = float(trade_rank)
        utility_value = float(utility)
    except (TypeError, ValueError, OverflowError):
        trade_rank_value = utility_value = None
    if (
        not reason
        and trade_rank_value is not None
        and math.isfinite(trade_rank_value)
        and trade_rank_value > 0
        and utility_value is not None
        and math.isfinite(utility_value)
        and utility_value > 0
    ):
        return decision

    expressions = dict(decision.expressions)
    cash = expressions.get(ExpressionKind.CASH) or _cash_expression(
        decision.ticker, decision.cutoff, decision.input_manifest.input_hash
    )
    cash = cash.model_copy(update={"selected": True})
    expressions[ExpressionKind.CASH] = cash
    expressions = {
        kind: expression.model_copy(update={"selected": kind is ExpressionKind.CASH})
        for kind, expression in expressions.items()
    }
    tactical = decision.tactical.model_copy(update={
        "selected_instrument": ExpressionKind.CASH,
        "alternate_expression": ExpressionKind.STOCK,
    })
    fundamental = decision.fundamental.model_copy(update={
        "selected_instrument": ExpressionKind.CASH,
        "alternate_expression": ExpressionKind.STOCK,
    })
    episode = build_opportunity_episode(
        ticker=decision.ticker,
        decision_revision=decision.decision_revision,
        policy_version=decision.policy_version,
        cutoff=decision.cutoff,
        input_lineage=decision.input_lineage,
        expressions=expressions,
        selected_expression=ExpressionKind.CASH,
        episode_id=decision.opportunity_episode_id,
    )
    impacts = {
        kind: impact.model_copy(update={
            "expression_identity": _expression_identity_for(
                expression, kind, decision.ticker, decision.decision_revision
            ),
        })
        for kind, expression in expressions.items()
        for impact in [decision.portfolio_impacts.get(kind)]
        if impact is not None
    }
    blocker = reason or "opportunity_rank_missing"
    resolution = build_decision_resolution(
        action="NO_TRADE",
        decision_revision=decision.decision_revision,
        policy_version=decision.policy_version,
        provenance=decision.resolution.provenance if decision.resolution else {
            "as_of": decision.as_of,
            "input_hash": decision.input_manifest.input_hash,
        },
        ticker=decision.ticker,
        blockers=[blocker],
        ttl=decision.resolution.expires_at if decision.resolution else None,
        portfolio_context={
            "status": "cash_comparator",
            "trade_rank": None,
            "trade_utility": 0.0,
            "blocker": blocker,
        },
        data_quality="INCOMPLETE",
        authorization_mode="NONE",
        rationale=f"Cash is selected because the current opportunity rank is unavailable: {blocker}.",
        owned=decision.capital_action.owned,
        catalyst=decision.capital_action.catalyst,
        expires_at=decision.capital_action.expires_at,
        blocked=True,
    )
    return decision.model_copy(update={
        "tactical": tactical,
        "fundamental": fundamental,
        "expressions": expressions,
        "selected_expression": cash,
        "opportunity_episode": episode,
        "portfolio_impacts": impacts,
        "resolution": resolution,
        "capital_action": capital_action_from_resolution(resolution),
    })


def _trade_plan_id(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(_trade_plan_jsonable(payload), sort_keys=True, separators=(",", ":"))
    return f"{TRADE_PLAN_CONTRACT_VERSION}:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _outcome_attribution_id(payload: Mapping[str, Any]) -> str:
    content = dict(payload)
    content.pop("outcome_attribution_id", None)
    content.pop("publication_id", None)
    encoded = json.dumps(_outcome_attribution_jsonable(content), sort_keys=True, separators=(",", ":"))
    return f"{OUTCOME_ATTRIBUTION_CONTRACT_VERSION}:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _outcome_attribution_jsonable(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return {
            str(name): _outcome_attribution_jsonable(item, key=str(name))
            for name, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_outcome_attribution_jsonable(item, key=key) for item in value]
    if isinstance(value, datetime):
        normalized = _utc(value)
        return normalized.isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and key is not None and (
        key in {"as_of", "cutoff", "input_cutoff", "evaluation_reference", "trade_plan_cutoff"}
        or key.endswith("_at") or key.endswith("_through") or key.endswith("_cutoff")
    ):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        normalized = _utc(parsed)
        return normalized.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    return value


def _trade_plan_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _trade_plan_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_trade_plan_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        rendered = value.isoformat()
        return rendered.replace("+00:00", "Z") if isinstance(value, datetime) else rendered
    if isinstance(value, StrEnum):
        return value.value
    return value


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value) if isinstance(value, Mapping) else {}


def build_trade_plan(
    *,
    decision: TickerDecision,
    rank: Mapping[str, Any] | BaseModel | None,
    alpha_signal: Mapping[str, Any] | BaseModel | None = None,
    portfolio_impact: PortfolioImpact | Mapping[str, Any] | None = None,
    resolution: DecisionResolutionV2 | None = None,
    publication_id: str | None = None,
) -> TradePlan:
    """Freeze one exact paper plan after rank and CASH safety are final."""

    rank_payload = _json_mapping(rank)
    signal_payload = _json_mapping(alpha_signal)
    current_resolution = resolution or decision.resolution
    selected = decision.selected_expression
    reason = ""
    if current_resolution is not None and current_resolution.is_blocked:
        reason = current_resolution.primary_blocker or "decision_resolution_blocked"
    if selected is None:
        reason = reason or "selected_expression_missing"
    if not rank_payload:
        reason = reason or "opportunity_rank_missing"
    if rank_payload:
        if str(rank_payload.get("ticker") or rank_payload.get("symbol") or "").upper() not in {"", decision.ticker}:
            reason = reason or "opportunity_rank_identity_mismatch"
        if str(rank_payload.get("decision_revision") or "") != decision.decision_revision:
            reason = reason or "opportunity_rank_identity_mismatch"
        if str(rank_payload.get("opportunity_episode_id") or "") != decision.opportunity_episode_id:
            reason = reason or "opportunity_rank_identity_mismatch"
        rank_reason = str(rank_payload.get("trade_rank_unavailable_reason") or "").strip()
        if rank_reason:
            reason = reason or rank_reason
        if not bool(rank_payload.get("evaluated_universe_complete")):
            reason = reason or "ranking_universe_incomplete"
        try:
            if int(rank_payload.get("trade_rank")) <= 0 or not math.isfinite(float(rank_payload.get("trade_utility"))):
                reason = reason or "opportunity_rank_unavailable"
            elif float(rank_payload.get("trade_utility")) <= 0:
                reason = reason or "opportunity_rank_unavailable"
        except (TypeError, ValueError, OverflowError):
            reason = reason or "opportunity_rank_unavailable"

    impact = (
        portfolio_impact
        if isinstance(portfolio_impact, PortfolioImpact)
        else PortfolioImpact.model_validate(portfolio_impact)
        if portfolio_impact is not None
        else decision.portfolio_impacts.get(selected.kind) if selected is not None else None
    )
    snapshot = decision.market_state_snapshot
    if snapshot is None or not decision.market_state_publication_id:
        reason = reason or "market_state_publication_missing"
    if decision.risk_policy_snapshot is None or decision.risk_policy_snapshot.blockers:
        reason = reason or "risk_policy_snapshot_missing"
    if impact is None:
        reason = reason or "portfolio_impact_missing"
    elif impact.availability != "available" or impact.blockers:
        reason = reason or str(impact.blockers[0] if impact.blockers else "portfolio_impact_unavailable")

    if selected is not None and rank_payload:
        if str(rank_payload.get("selected_expression_kind") or "") != selected.kind.value:
            reason = reason or "opportunity_rank_identity_mismatch"
        if str(rank_payload.get("selected_expression_identity") or "") != trade_expression_identity(selected):
            reason = reason or "opportunity_rank_identity_mismatch"
        if impact is not None and rank_payload.get("portfolio_impact_id") and str(rank_payload["portfolio_impact_id"]) != impact.impact_id:
            reason = reason or "opportunity_rank_identity_mismatch"
    if signal_payload and rank_payload.get("alpha_signal_id") and str(signal_payload.get("signal_id") or "") != str(rank_payload["alpha_signal_id"]):
        reason = reason or "alpha_signal_identity_mismatch"
    if rank_payload.get("alpha_signal_id") is None:
        reason = reason or "alpha_signal_missing"
    if not signal_payload and alpha_signal is not None:
        reason = reason or "alpha_signal_missing"
    if current_resolution is None or not current_resolution.is_actionable:
        reason = reason or "decision_resolution_not_actionable"

    actionable = not reason and selected is not None
    if actionable:
        expression = selected
        kind = expression.kind
        if kind is ExpressionKind.CASH:
            reason = "cash_selected"
        if expression.status != "eligible":
            reason = "selected_expression_unavailable"
        if expression.entry_range is None or _midpoint(expression.entry_range) is None or _midpoint(expression.entry_range) <= 0:
            reason = reason or "entry_limit_missing"
        if expression.quantity is None or expression.quantity <= 0:
            reason = reason or "quantity_missing"
        if expression.max_loss_per_unit is None or expression.max_loss_per_unit <= 0 or not math.isfinite(expression.max_loss_per_unit):
            reason = reason or "max_loss_missing"
        if expression.planned_loss is None or expression.planned_loss <= 0 or not math.isfinite(expression.planned_loss):
            reason = reason or "planned_loss_missing"
        if expression.invalidation is None:
            reason = reason or "invalidation_missing"
        if expression.target_range is None:
            reason = reason or "profit_exit_missing"
        if current_resolution is not None and current_resolution.primary_blocker:
            reason = reason or current_resolution.primary_blocker
    if reason:
        cash = decision.expressions.get(ExpressionKind.CASH) or _cash_expression(
            decision.ticker, decision.cutoff, decision.input_manifest.input_hash,
        )
        expression = cash.model_copy(update={"selected": True})
        kind = ExpressionKind.CASH
        selected_identity = (
            str(rank_payload.get("selected_expression_identity") or "")
            if str(rank_payload.get("selected_expression_kind") or "") == kind.value
            else trade_expression_identity(expression)
        )
        if not selected_identity:
            selected_identity = trade_expression_identity(expression)
        action = "NO_TRADE"
        eligibility = "BLOCKED"
        authorization = "NONE"
        data_quality = "INCOMPLETE"
        blockers = tuple(dict.fromkeys((*(current_resolution.blockers if current_resolution else ()), reason)))
        entry = None
        entry_limit = None
        quantity = None
        max_loss = None
        planned_loss = None
        invalidation = None
        profit_exit = None
        expiry = None
        if current_resolution is not None:
            expiry = current_resolution.expires_at
        next_action = current_resolution.next_action if current_resolution is not None else next_action_for(reason)
        if impact is None or impact.expression_kind is not ExpressionKind.CASH:
            impact = decision.portfolio_impacts.get(ExpressionKind.CASH)
    else:
        expression = selected
        kind = expression.kind
        selected_identity = str(rank_payload.get("selected_expression_identity") or trade_expression_identity(expression))
        action = getattr(current_resolution.action, "value", current_resolution.action)
        eligibility = getattr(current_resolution.eligibility, "value", current_resolution.eligibility)
        authorization = "PAPER"
        data_quality = getattr(current_resolution.data_quality, "value", current_resolution.data_quality)
        blockers = ()
        entry = expression.entry_range
        entry_limit = _midpoint(entry)
        quantity = expression.quantity
        max_loss = expression.max_loss_per_unit
        planned_loss = expression.planned_loss
        invalidation = expression.invalidation
        profit_exit = expression.target_range
        expiry = current_resolution.expires_at or min(decision.tactical.expiry_date, decision.fundamental.expiry_date)
        next_action = current_resolution.next_action

    primary_blocker = (
        current_resolution.primary_blocker
        if current_resolution is not None and current_resolution.primary_blocker in blockers
        else blockers[0] if blockers else None
    )
    values = {
        "contract_version": TRADE_PLAN_CONTRACT_VERSION,
        "publication_id": publication_id,
        "ticker": decision.ticker,
        "opportunity_episode_id": decision.opportunity_episode_id,
        "decision_revision": decision.decision_revision,
        "policy_version": decision.policy_version,
        "cutoff": decision.cutoff,
        "input_lineage": tuple(decision.input_lineage),
        "selected_expression_kind": kind,
        "selected_expression_identity": selected_identity,
        "selected_expression": expression,
        "rank_id": str(rank_payload.get("rank_id") or "") or None,
        "alpha_signal_id": str(rank_payload.get("alpha_signal_id") or signal_payload.get("signal_id") or "") or None,
        "portfolio_impact_id": impact.impact_id if impact is not None else None,
        "market_snapshot_id": snapshot.snapshot_id if snapshot is not None else None,
        "market_state_publication_id": decision.market_state_publication_id or (snapshot.publication_id if snapshot else None),
        "action": action,
        "eligibility": eligibility,
        "availability_status": (
            AvailabilityStatus.AVAILABLE
            if eligibility == "ACTIONABLE"
            else availability_status_for_blockers((primary_blocker, *blockers))
        ),
        "authorization_mode": authorization,
        "data_quality": data_quality,
        "rationale": current_resolution.rationale if current_resolution is not None else decision.capital_action.rationale,
        "primary_blocker": primary_blocker,
        "blockers": blockers,
        "next_action": next_action or next_action_for(blockers[0] if blockers else None),
        "entry": entry,
        "entry_limit": entry_limit,
        "quantity": quantity,
        "max_loss_per_unit": max_loss,
        "planned_loss": planned_loss,
        "invalidation": invalidation,
        "profit_exit": profit_exit,
        "expiry": expiry,
        "portfolio_impact": impact,
    }
    return TradePlan.model_validate({
        **values,
        "trade_plan_id": _trade_plan_id({
            key: value for key, value in values.items()
            if key not in {"trade_plan_id", "publication_id", "availability_status"}
        }),
    })


def bind_trade_plan(decision: TickerDecision, plan: TradePlan) -> TickerDecision:
    """Bind resolution compatibility fields to the frozen plan terms."""

    base = decision.resolution.model_dump(mode="json") if decision.resolution is not None else {
        "ticker": decision.ticker,
        "decision_revision": decision.decision_revision,
        "policy_version": decision.policy_version,
        "provenance": {"as_of": decision.as_of, "input_hash": decision.input_manifest.input_hash},
        "rationale": decision.capital_action.rationale,
        "owned": decision.capital_action.owned,
    }
    base.update({
        "trade_plan_id": plan.trade_plan_id,
        "action": plan.action,
        "eligibility": plan.eligibility,
        "authorization_mode": plan.authorization_mode,
        "data_quality": plan.data_quality,
        "rationale": plan.rationale,
        "primary_blocker": plan.primary_blocker,
        "blockers": list(plan.blockers),
        "next_action": plan.next_action,
        "entry": plan.entry,
        "size": plan.quantity,
        "invalidation": plan.invalidation,
        "exit": plan.profit_exit,
        "ttl": plan.expiry,
        "portfolio_context": plan.portfolio_impact.model_dump(mode="json") if plan.portfolio_impact else None,
        "expires_at": plan.expiry,
    })
    resolution = DecisionResolutionV2.model_validate(base)
    return decision.model_copy(update={
        "trade_plan": plan,
        "resolution": resolution,
        "capital_action": capital_action_from_resolution(resolution),
    })


def _cash_expression(ticker: str, cutoff: datetime, thesis_revision: str) -> ExpressionDecision:
    return ExpressionDecision(
        kind=ExpressionKind.CASH,
        ticker=ticker.strip().upper(),
        horizon=Horizon.FUNDAMENTAL,
        thesis_revision=thesis_revision,
        stance=Stance.NEUTRAL,
        status="not_selected",
        rationale="Cash is the explicit zero-impact fallback when no expression is actionable.",
    )


def _expression_identity_for(
    expression: ExpressionDecision,
    kind: ExpressionKind,
    ticker: str,
    decision_revision: str,
) -> str:
    if kind is ExpressionKind.CASH and expression.status == "not_selected":
        return f"CASH:{ticker.strip().upper()}:{decision_revision}"
    return trade_expression_identity(expression)


def _missing_market_snapshot(cutoff: datetime) -> MarketStateSnapshot:
    reference = _utc(cutoff)
    return MarketStateSnapshot(
        snapshot_id=f"missing-market:{reference.isoformat()}",
        as_of=reference,
        input_cutoff=reference,
        availability="unavailable",
        blockers=("market_state_missing",),
    )


def _missing_portfolio_impact(
    *,
    episode: OpportunityEpisode,
    expression: ExpressionDecision,
    snapshot: MarketStateSnapshot,
    policy_version: str,
) -> PortfolioImpact:
    kind = expression.kind
    identity = _expression_identity_for(expression, kind, episode.ticker, episode.decision_revision)
    return PortfolioImpact(
        impact_id=f"missing-impact:{episode.episode_id}:{kind.value}",
        ticker=episode.ticker,
        opportunity_episode_id=episode.episode_id,
        expression_kind=kind,
        expression_identity=identity,
        decision_revision=episode.decision_revision,
        risk_policy_version=policy_version,
        market_snapshot_id=snapshot.snapshot_id,
        market_state_publication_id=snapshot.publication_id,
        cutoff=episode.cutoff,
        input_lineage=tuple(episode.input_lineage),
        availability="unavailable",
        blockers=(f"portfolio_impact_missing:{kind.value}",),
    )


def _portfolio_impact_id(impact: PortfolioImpact) -> str:
    payload = {
        "book_identity": impact.portfolio_before.get("book_identity"),
        "ticker": impact.ticker,
        "opportunity_episode_id": impact.opportunity_episode_id,
        "expression_kind": impact.expression_kind.value,
        "expression_identity": impact.expression_identity,
        "decision_revision": impact.decision_revision,
        "risk_policy_version": impact.risk_policy_version,
        "market_snapshot_id": impact.market_snapshot_id,
        "market_state_publication_id": impact.market_state_publication_id,
        "cutoff": impact.cutoff,
        "portfolio_before": impact.portfolio_before,
        "portfolio_after": impact.portfolio_after,
        "values": {
            "position_weight_before": impact.position_weight_before,
            "position_weight_after": impact.position_weight_after,
            "gross_exposure_before": impact.gross_exposure_before,
            "gross_exposure_after": impact.gross_exposure_after,
            "net_exposure_before": impact.net_exposure_before,
            "net_exposure_after": impact.net_exposure_after,
            "symbol_concentration_delta": impact.symbol_concentration_delta,
            "sector_concentration_delta": impact.sector_concentration_delta,
            "beta_delta": impact.beta_delta,
            "correlation_cluster_delta": impact.correlation_cluster_delta,
            "planned_loss": impact.planned_loss,
            "adv_participation": impact.adv_participation,
            "days_to_exit": impact.days_to_exit,
            "marginal_risk": impact.marginal_risk,
            "risk_budget_consumed": impact.risk_budget_consumed,
            "scenario_pnl": impact.scenario_pnl,
            "factor_exposure": impact.factor_exposure,
            "greeks": impact.greeks,
            "liquidity": impact.liquidity,
            "cash_comparator": impact.cash_comparator,
            "top_alternative": impact.top_alternative,
            "position_to_trim_or_replace": impact.position_to_trim_or_replace,
            "funding_source_or_position_to_trim": impact.funding_source_or_position_to_trim,
        },
    }
    return f"portfolio-impact:{hashlib.sha256(json.dumps(_jsonable(payload), sort_keys=True, separators=(',', ':')).encode()).hexdigest()}"


def _portfolio_impact_from_legacy(value: Any, *, ticker: str) -> PortfolioImpact:
    """Adapt only an explicitly versioned persisted v1 row missing its target ticker."""

    if isinstance(value, PortfolioImpact):
        return value
    raw = value.model_dump(mode="python") if isinstance(value, BaseModel) else dict(value or {})
    if not isinstance(raw, Mapping):
        return PortfolioImpact.model_validate(raw)
    raw = dict(raw)
    if raw.get("ticker") is not None and str(raw["ticker"]).strip():
        return PortfolioImpact.model_validate(raw)
    if raw.get("contract_version") != PORTFOLIO_IMPACT_CONTRACT_VERSION:
        raise ValueError("legacy portfolio impact ticker inference requires portfolio-impact.v1")
    target_ticker = normalize_symbol(str(ticker))
    if SYMBOL_RE.fullmatch(target_ticker) is None:
        raise ValueError("legacy portfolio impact ticker inference requires a valid parent ticker")
    aliases = _all_portfolio_impact_identity_aliases(raw)
    if _identity_aliases_conflict(aliases):
        raise ValueError("portfolio impact contains conflicting ticker/symbol/instrument_symbol aliases")
    if any(identity != target_ticker for _, identity in aliases):
        raise ValueError("legacy portfolio impact aliases must match the parent ticker")
    raw["ticker"] = target_ticker
    return PortfolioImpact.model_validate(raw)


def portfolio_impact_from_persisted(value: Any, *, ticker: str) -> Any:
    if isinstance(value, Mapping) and (
        value.get("ticker") is None or not str(value.get("ticker")).strip()
    ):
        return PortfolioImpact.from_legacy(value, ticker=ticker)
    return value


def portfolio_impacts_from_persisted(value: Any, *, ticker: str) -> Any:
    if not isinstance(value, Mapping):
        return value
    return {
        kind: portfolio_impact_from_persisted(impact, ticker=ticker)
        for kind, impact in value.items()
    }


def trade_plan_from_persisted(value: Any, *, ticker: str) -> Any:
    if not isinstance(value, Mapping) or not isinstance(value.get("portfolio_impact"), Mapping):
        return value
    result = dict(value)
    result["portfolio_impact"] = portfolio_impact_from_persisted(
        result["portfolio_impact"], ticker=ticker,
    )
    return result


def _local_market_snapshot(cutoff: datetime, lineage: Iterable[InputLineage]) -> MarketStateSnapshot:
    reference = _utc(cutoff)
    encoded = json.dumps(
        {"cutoff": reference.isoformat(), "lineage": [item.model_dump(mode="json") for item in lineage]},
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot_id = f"local-market:{hashlib.sha256(encoded.encode()).hexdigest()[:24]}"
    dimensions = {
        horizon: tuple(
            MarketDimensionState(
                dimension=dimension,
                horizon=horizon,
                evidence_status="unavailable",
                uncertainty="market publication not supplied to the composer",
                blockers=("market_publication_required",),
                source_priority=MARKET_SOURCE_PRIORITY.get(dimension, ()),
            )
            for dimension in MARKET_DIMENSIONS
        )
        for horizon in MARKET_HORIZONS
    }
    rows = tuple(
        CoverageMatrixRow(
            dimension=dimension,
            asset_class="cross-asset",
            horizon=horizon,
            point_in_time_safe=False,
            current_status="unavailable",
            decision_impact="context",
            fallback_policy="unavailable",
            input_cutoff=reference,
            source_priority=MARKET_SOURCE_PRIORITY.get(dimension, ()),
            blockers=("market_publication_required",),
        )
        for horizon in MARKET_HORIZONS
        for dimension in MARKET_DIMENSIONS
    )
    snapshot = MarketStateSnapshot(
        # The local composer fallback is the readable V1 compatibility shape;
        # published V2 snapshots must carry a V2 coverage matrix.
        contract_version="market-state-snapshot.v1",
        snapshot_id=snapshot_id,
        publication_id=f"local-publication:{snapshot_id}",
        as_of=reference,
        input_cutoff=reference,
        horizons=dimensions,
        coverage_matrix=CoverageMatrix(
            matrix_id=f"coverage:{snapshot_id}",
            as_of=reference,
            input_cutoff=reference,
            rows=rows,
        ),
        input_lineage=tuple(lineage),
        availability="available",
    )
    return snapshot


def _portfolio_book_blockers(replay: Mapping[str, Any], cutoff: datetime) -> list[str]:
    blockers: list[str] = []
    if not str(replay.get("book_identity") or "").strip():
        blockers.append("portfolio_book_identity_missing")
    try:
        replay_cutoff = replay.get("cutoff")
        if not isinstance(replay_cutoff, datetime):
            replay_cutoff = datetime.fromisoformat(str(replay_cutoff).replace("Z", "+00:00"))
        if _utc(replay_cutoff) != _utc(cutoff):
            blockers.append("portfolio_book_cutoff_mismatch")
    except (TypeError, ValueError):
        blockers.append("portfolio_book_cutoff_invalid")
    positions = replay.get("positions")
    if not isinstance(positions, list):
        blockers.append("portfolio_positions_missing")
        positions = []
    lineage = replay.get("lineage")
    if not isinstance(lineage, list) or (replay.get("transaction_count") and not lineage):
        blockers.append("portfolio_transaction_lineage_missing")
        lineage = []
    transaction_ids = [str(item.get("transaction_id") or "") for item in lineage if isinstance(item, Mapping)]
    if len(transaction_ids) != len(lineage) or not all(transaction_ids) or len(set(transaction_ids)) != len(transaction_ids):
        blockers.append("portfolio_transaction_lineage_invalid")
    instrument_ids: set[str] = set()
    for position in positions:
        if not isinstance(position, Mapping):
            blockers.append("portfolio_position_invalid")
            continue
        instrument_id = str(position.get("instrument_id") or "")
        if not instrument_id or instrument_id in instrument_ids:
            blockers.append("portfolio_position_duplicate")
        instrument_ids.add(instrument_id)
        for name in ("quantity", "avg_cost", "price", "market_value"):
            value = _number(position.get(name))
            if value is None or not math.isfinite(value) or (name in {"quantity", "price"} and value <= 0):
                blockers.append(f"portfolio_{name}_invalid")
        for name in ("source_id", "currency", "source_kind", "trading_date", "observed_at", "available_at", "valuation_status"):
            if not position.get(name):
                blockers.append(f"portfolio_{name}_missing")
        for name in ("observed_at", "available_at"):
            try:
                value = position.get(name)
                if not isinstance(value, datetime):
                    value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if _utc(value) > _utc(cutoff):
                    blockers.append(f"portfolio_{name}_future")
            except (TypeError, ValueError):
                blockers.append(f"portfolio_{name}_invalid")
        if str(position.get("valuation_status") or "").lower() == "unavailable":
            blockers.append("portfolio_valuation_unavailable")
    expected_count = len(positions)
    if (
        replay.get("valuation_complete") is not True
        or replay.get("eligible_position_count") != expected_count
        or replay.get("valued_position_count") != expected_count
        or replay.get("missing_valuation_count") != 0
    ):
        blockers.append("portfolio_book_incomplete")
    value = _number(replay.get("portfolio_value"))
    if value is None or not math.isfinite(value) or value < 0:
        blockers.append("portfolio_value_incomplete")
    return list(dict.fromkeys(blockers))


_STOCK_SCENARIO_NAMES = ("spy", "qqq", "sector", "symbol", "earnings_gap", "liquidity")
_STOCK_BTC_SCENARIO_NAMES = ("btc",)
_STOCK_SCENARIO_SHOCKS = {
    "spy": (-5.0, -10.0),
    "qqq": (-5.0, -10.0),
    "sector": (-10.0,),
    "symbol": (-20.0, -30.0),
    "btc": (-15.0,),
}
_STOCK_BTC_IDENTITIES = frozenset({"BTC-USD", "BITCOIN", "BITCOIN-USD"})
_STOCK_CASH_COMPARISON_KEYS = (
    "expected_return", "expected_pnl", "cash_return", "cash_yield", "return",
    "pnl", "opportunity_cost", "expected_value",
)
_STOCK_PLACEHOLDER_LABELS = {"", "cash comparator", "cash_comparator", "none", "unknown", "tbd"}
_STOCK_TOP_ALTERNATIVE_PLACEHOLDERS = _STOCK_PLACEHOLDER_LABELS | {
    "cash", "usd", "n/a", "na", "not available", "not_applicable",
}


def _stock_scenario_key(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


# The built-in universe is the canonical configured catalog.  Other symbols
# must arrive from a catalog/position row carrying an instrument identity.
_STOCK_CATALOG_TICKERS = frozenset(
    normalize_symbol(str(item.get("symbol") or ""))
    for item in DEFAULT_WATCHLIST
    if item.get("symbol")
)
_STOCK_CRYPTO_SENSITIVE_SYMBOLS = frozenset(
    normalize_symbol(str(item.get("symbol") or ""))
    for item in DEFAULT_WATCHLIST
    if any(
        token in _stock_scenario_key(item.get(field))
        for field in ("asset_class", "category", "sector", "industry")
        for token in ("crypto", "bitcoin", "blockchain", "digital_asset")
    )
)


def _stock_crypto_signal(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if any(value.get(key) is True for key in ("crypto_sensitive", "crypto_exposure", "btc_exposure")):
        return True
    if any(_number(value.get(key), 0.0) > 0 for key in ("crypto_exposure", "btc_exposure")):
        return True
    for key in (
        "asset_class", "asset_type", "category", "sector", "industry", "theme",
        "classification", "instrument_type", "instrument_category", "tags",
    ):
        candidate = value.get(key)
        candidates = candidate if isinstance(candidate, (list, tuple, set, frozenset)) else (candidate,)
        if any(
            any(token in _stock_scenario_key(item) for token in ("crypto", "bitcoin", "blockchain", "digital_asset"))
            for item in candidates
            if item is not None
        ):
            return True
    return False


def _stock_btc_scenarios_required(
    evidence: Mapping[str, Any],
    positions: Iterable[Any] = (),
    ticker: str | None = None,
    *,
    portfolio_before: Mapping[str, Any] | None = None,
) -> bool:
    position_rows = tuple(positions)
    if any(evidence.get(key) is True for key in ("btc_scenarios_applicable", "btc_scenario_required", "btc_exposure")):
        return True
    if _number(evidence.get("btc_exposure"), 0.0) > 0:
        return True
    authoritative_sources = (portfolio_before, evidence)
    candidate_tickers: set[str] = {
        normalize_symbol(str(ticker))
    } if ticker else set()
    for source in authoritative_sources:
        if not isinstance(source, Mapping):
            continue
        candidate_tickers.update(identity for _, identity in _identity_aliases(source))
        for nested in ("instrument", "target_instrument"):
            candidate_tickers.update(
                identity for _, identity in _identity_aliases(source.get(nested))
            )
    if candidate_tickers & (_STOCK_BTC_IDENTITIES | _STOCK_CRYPTO_SENSITIVE_SYMBOLS):
        return True
    if _stock_crypto_signal(evidence) or _stock_crypto_signal(portfolio_before):
        return True
    return any(
        isinstance(position, Mapping)
        and (
            _stock_crypto_signal(position)
            or bool(_stock_position_identities(position) & _STOCK_BTC_IDENTITIES)
            or bool(_stock_position_identities(position) & _STOCK_CRYPTO_SENSITIVE_SYMBOLS)
        )
        for position in position_rows
    )


def _stock_position_identities(position: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(identity for _, identity in _identity_aliases(position))


def _stock_scenario_magnitudes(value: Mapping[str, Any]) -> tuple[float, ...] | None:
    raw = _pick(
        value,
        "shock_pct", "shock_percent", "shock_percentages", "shocks_pct", "shocks",
        "magnitude_pct", "magnitude_percent", "shock", "drawdown_pct", "drawdown",
    )
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        numeric_keys = [_number(key) for key in raw]
        raw = list(raw) if all(item is not None for item in numeric_keys) else list(raw.values())
    elif isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
        raw = (raw,)
    magnitudes: list[float] = []
    for item in raw:
        magnitude = _number(item)
        if magnitude is None:
            return None
        magnitudes.append(magnitude)
    return tuple(magnitudes)


def _stock_pnl_cases(value: Mapping[str, Any]) -> Mapping[Any, Any] | None:
    raw = _pick(value, "pnl_by_shock", "pnl_by_magnitude", "pnl_by_percent", "pnl_by_pct")
    if raw is None and isinstance(value.get("pnl"), Mapping):
        raw = value["pnl"]
    if raw is None:
        raw = _pick(value, "pnl_by_scenario", "values")
    return raw if isinstance(raw, Mapping) else None


def _stock_pnl_by_shock(value: Mapping[str, Any]) -> dict[float, float] | None:
    cases = _stock_pnl_cases(value)
    if cases is None:
        return None
    result: dict[float, float] = {}
    for raw_shock, raw_pnl in cases.items():
        shock = _number(raw_shock)
        pnl = _number(raw_pnl)
        if shock is None or pnl is None or any(
            math.isclose(shock, existing, rel_tol=1e-9, abs_tol=1e-9)
            for existing in result
        ):
            return None
        result[shock] = pnl
    return result


def _stock_numeric_keys_match(values: Mapping[float, Any] | None, expected: tuple[float, ...]) -> bool:
    return (
        values is not None
        and len(values) == len(expected)
        and all(
            any(math.isclose(actual, wanted, rel_tol=1e-9, abs_tol=1e-9) for actual in values)
            for wanted in expected
        )
    )


def _stock_matches_shocks(value: Mapping[str, Any], expected: tuple[float, ...]) -> bool:
    actual = _stock_scenario_magnitudes(value)
    if actual is not None:
        return (
            len(actual) == len(expected)
            and all(
                math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
                for left, right in zip(sorted(actual), sorted(expected))
            )
        )
    return _stock_numeric_keys_match(_stock_pnl_by_shock(value), expected)


def _stock_has_pnl(value: Any, expected: tuple[float, ...] | None = None) -> bool:
    if not isinstance(value, Mapping):
        return _number(value) is not None
    pnl = _pick(value, "pnl", "pnl_usd", "value")
    if expected is not None and len(expected) > 1:
        return _stock_numeric_keys_match(_stock_pnl_by_shock(value), expected)
    if _number(pnl) is not None:
        return True
    if expected is not None:
        return _stock_numeric_keys_match(_stock_pnl_by_shock(value), expected)
    pnl_cases = _stock_pnl_cases(value)
    return isinstance(pnl_cases, Mapping) and bool(pnl_cases) and all(
        _number(item) is not None for item in pnl_cases.values()
    )


def _stock_largest_holding(positions: Iterable[Any]) -> str | None:
    candidates: list[tuple[float, str]] = []
    for position in positions:
        if not isinstance(position, Mapping):
            continue
        symbol = normalize_symbol(str(_pick(position, "symbol", "ticker") or ""))
        market_value = _number(position.get("market_value"))
        if symbol and market_value is not None:
            candidates.append((abs(market_value), symbol))
    return max(candidates, key=lambda item: (item[0], item[1]))[1] if candidates else None


def _stock_earnings_gap_complete(value: Mapping[str, Any], largest_holding: str | None) -> bool:
    if largest_holding is None:
        return False
    holding = normalize_symbol(str(_pick(
        value,
        "largest_holding", "largest_holding_symbol", "largest_position", "holding", "position",
    ) or ""))
    if not holding or holding != largest_holding:
        return False
    marker = _pick(
        value,
        "earnings_gap", "earnings_gap_pct", "gap_pct", "gap", "event", "shock_type", "scenario_type",
    )
    if marker is None:
        return False
    if isinstance(marker, bool):
        return marker
    if isinstance(marker, Mapping):
        return marker.get("applied") is True or _number(_pick(marker, "pct", "percent", "value")) is not None
    if isinstance(marker, str):
        return _stock_scenario_key(marker) in {
            "earnings", "earnings_gap", "earnings_event", "earnings_gap_event",
        } or _number(marker) is not None
    return _number(marker) is not None


def _stock_has_key(sources: Iterable[Any], keys: tuple[str, ...]) -> bool:
    return any(isinstance(source, Mapping) and any(key in source for key in keys) for source in sources)


def _stock_consistent_number(sources: Iterable[Any], keys: tuple[str, ...]) -> float | None:
    values: list[float] = []
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key in keys:
            if key not in source or source.get(key) is None:
                continue
            value = _number(source.get(key))
            if value is None:
                return None
            values.append(value)
    if not values or not all(
        math.isclose(value, values[0], rel_tol=1e-9, abs_tol=1e-9) for value in values[1:]
    ):
        return None
    return values[0]


def _stock_adv_haircut_present(sources: Iterable[Any]) -> bool:
    found = False
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key in ("adv_multiplier", "adv_haircut_multiplier", "liquidity_multiplier", "adv_haircut_fraction"):
            if key not in source or source.get(key) is None:
                continue
            value = _number(source.get(key))
            if value is None or not 0 < value < 1:
                return False
            found = True
        for key in ("adv_haircut_pct", "liquidity_haircut_pct"):
            if key not in source or source.get(key) is None:
                continue
            value = _number(source.get(key))
            if value is None or not 0 < value < 100:
                return False
            found = True
        for key in ("adv_haircut", "liquidity_haircut"):
            if key not in source or source.get(key) is None:
                continue
            value = _number(source.get(key))
            if value is None or not 0 < value < 100:
                return False
            found = True
    return found


def _stock_liquidity_stress_complete(value: Mapping[str, Any], evidence: Mapping[str, Any]) -> bool:
    nested = [
        value.get("spread_slippage"), value.get("execution_cost"), value.get("execution_costs"),
        evidence.get("spread_slippage"), evidence.get("execution_cost"), evidence.get("execution_costs"),
        evidence.get("liquidity"),
    ]
    sources = (value, evidence, *nested)
    common_keys = (
        "spread_slippage_multiplier", "spread_and_slippage_multiplier", "execution_cost_multiplier",
        "cost_multiplier", "spread_slippage",
    )
    spread_keys = ("spread_multiplier", "spread_x", "spread_factor")
    slippage_keys = ("slippage_multiplier", "slippage_x", "slippage_factor")
    common = _stock_consistent_number(sources, common_keys)
    spread = _stock_consistent_number(sources, spread_keys)
    slippage = _stock_consistent_number(sources, slippage_keys)
    if (_stock_has_key(sources, common_keys) and common is None) or (
        _stock_has_key(sources, spread_keys) and spread is None
    ) or (_stock_has_key(sources, slippage_keys) and slippage is None):
        return False
    if spread is None:
        spread = common
    if slippage is None:
        slippage = common
    if common is not None and (
        (spread is not None and not math.isclose(spread, common, rel_tol=1e-9, abs_tol=1e-9))
        or (slippage is not None and not math.isclose(slippage, common, rel_tol=1e-9, abs_tol=1e-9))
    ):
        return False
    return (
        spread is not None
        and slippage is not None
        and math.isclose(spread, 2.0, rel_tol=1e-9, abs_tol=1e-9)
        and math.isclose(slippage, 2.0, rel_tol=1e-9, abs_tol=1e-9)
        and _stock_adv_haircut_present(sources)
    )


def _stock_scenario_pnl(
    evidence: Mapping[str, Any],
    *,
    btc_required: bool = False,
    largest_holding: str | None = None,
) -> dict[str, Any] | None:
    raw = evidence.get("stress_scenarios")
    if raw is None:
        raw = evidence.get("scenario_pnl")
    if not isinstance(raw, Mapping):
        return None
    required = (*_STOCK_SCENARIO_NAMES, *_STOCK_BTC_SCENARIO_NAMES) if btc_required else _STOCK_SCENARIO_NAMES
    keys: dict[str, Any] = {}
    for key in raw:
        normalized = _stock_scenario_key(key)
        if normalized in required:
            if normalized in keys:
                return None
            keys[normalized] = key
    for name in required:
        key = keys.get(name)
        if key is None:
            return None
        value = raw[key]
        expected_shocks = _STOCK_SCENARIO_SHOCKS.get(name)
        if not _stock_has_pnl(value, expected_shocks):
            return None
        if expected_shocks is not None and not isinstance(value, Mapping):
            return None
        if expected_shocks is not None and not _stock_matches_shocks(value, expected_shocks):
            return None
        if name == "earnings_gap":
            if not isinstance(value, Mapping) or not _stock_earnings_gap_complete(value, largest_holding):
                return None
        if name == "liquidity":
            if not isinstance(value, Mapping) or not _stock_liquidity_stress_complete(value, evidence):
                return None
    return dict(raw)


def _stock_risk_budget(evidence: Mapping[str, Any]) -> tuple[float | None, float | None]:
    raw = evidence.get("risk_budget")
    budget = raw if isinstance(raw, Mapping) else {}
    available = _number(_pick(budget, "available", "limit", "budget"))
    consumed = _number(_pick(budget, "consumed", "used", "risk_budget_consumed"))
    if available is None:
        available = _number(_pick(evidence, "risk_budget_available", "risk_budget_limit"))
    if consumed is None:
        consumed = _number(_pick(evidence, "risk_budget_consumed", "risk_budget_used"))
    return available, consumed


def _stock_evidence_label(value: Any) -> str | None:
    label = str(value or "").strip()
    return None if label.lower() in _STOCK_PLACEHOLDER_LABELS else label


def _stock_catalog_entry_ticker(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    if value.get("instrument_id") is None and value.get("catalog_instrument_id") is None:
        return None
    ticker = normalize_symbol(str(_pick(value, "ticker", "symbol") or ""))
    return (
        ticker
        if ticker
        and SYMBOL_RE.fullmatch(ticker) is not None
        and ticker.lower() not in _STOCK_TOP_ALTERNATIVE_PLACEHOLDERS
        else None
    )


def _stock_authoritative_catalog_tickers(portfolio_before: Mapping[str, Any] | None = None) -> set[str]:
    """Return only configured symbols and replay rows with instrument identity."""

    tickers = set(_STOCK_CATALOG_TICKERS)
    if isinstance(portfolio_before, Mapping):
        for position in portfolio_before.get("positions") or ():
            ticker = _stock_catalog_entry_ticker(position)
            if ticker is not None:
                tickers.add(ticker)
    return tickers


def _stock_verified_ticker(value: Any, authoritative_tickers: set[str]) -> str | None:
    candidate = _pick(value, "ticker", "symbol") if isinstance(value, Mapping) else value
    ticker = normalize_symbol(str(candidate or ""))
    if (
        not ticker
        or SYMBOL_RE.fullmatch(ticker) is None
        or ticker.lower() in _STOCK_TOP_ALTERNATIVE_PLACEHOLDERS
    ):
        return None
    return ticker if ticker in authoritative_tickers else None


def _stock_top_alternative(
    evidence: Mapping[str, Any],
    cash_comparator: Mapping[str, Any] | None,
    portfolio_before: Mapping[str, Any] | None = None,
) -> str | None:
    authoritative_tickers = _stock_authoritative_catalog_tickers(portfolio_before)
    if "top_alternative" in evidence:
        return _stock_verified_ticker(evidence.get("top_alternative"), authoritative_tickers)
    if isinstance(cash_comparator, Mapping):
        for key in ("top_alternative", "alternative"):
            if key in cash_comparator:
                return _stock_verified_ticker(cash_comparator.get(key), authoritative_tickers)
    return None


def _stock_funding_evidence(evidence: Mapping[str, Any]) -> tuple[str | None, str | None]:
    raw = evidence.get("funding_source_or_position_to_trim")
    trim = _stock_evidence_label(evidence.get("position_to_trim_or_replace"))
    if isinstance(raw, Mapping):
        source = _pick(raw, "source", "funding_source", "position_to_trim", "position_to_trim_or_replace", "id")
        if trim is None:
            trim = _stock_evidence_label(_pick(raw, "position_to_trim", "position_to_trim_or_replace"))
    else:
        source = raw
    if source is None:
        raw = evidence.get("funding")
        if isinstance(raw, Mapping):
            source = _pick(raw, "source", "funding_source", "position_to_trim", "position_to_trim_or_replace", "id")
            if trim is None:
                trim = _stock_evidence_label(_pick(raw, "position_to_trim", "position_to_trim_or_replace"))
        else:
            source = raw
    if source is None:
        source = _pick(evidence, "funding_source", "position_to_trim_or_replace")
    return _stock_evidence_label(source), trim


def _stock_impact_values(
    expression: ExpressionDecision,
    replay: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    """Calculate deterministic first-order stock impact from one cutoff book."""

    blockers: list[str] = []
    positions = [item for item in replay.get("positions", ()) if isinstance(item, Mapping)]
    nav = float(replay.get("portfolio_value") or 0)
    entry = expression.entry_range
    quantity = expression.quantity
    price = (entry.low + entry.high) / 2 if entry is not None else None
    if price is None or price <= 0:
        blockers.append("stock_entry_price_missing")
    if quantity is None or quantity <= 0:
        blockers.append("stock_quantity_missing")
    if nav <= 0:
        blockers.append("stock_nav_missing")
    added_value = float(price or 0) * int(quantity or 0)
    owned = next(
        (float(item.get("market_value") or 0) for item in positions
         if str(item.get("symbol") or "").upper() == expression.ticker.upper()),
        0.0,
    )
    before_gross = sum(abs(float(item.get("market_value") or 0)) for item in positions) / nav if nav else 0.0
    before_net = sum(float(item.get("market_value") or 0) for item in positions) / nav if nav else 0.0
    after_weight = (owned + added_value) / nav if nav else None
    after_gross = before_gross + added_value / nav if nav else None
    after_net = before_net + added_value / nav if nav else None
    planned_loss = expression.planned_loss
    if planned_loss is None and expression.max_loss_per_unit is not None and quantity:
        planned_loss = expression.max_loss_per_unit * quantity
    evidence = replay.get("stock_evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    before = {
        "ticker": expression.ticker,
        "position_weight": owned / nav if nav else None,
        "gross_exposure": before_gross,
        "net_exposure": before_net,
        "symbol_concentration": owned / nav if nav else None,
    }
    after = {
        "ticker": expression.ticker,
        "position_weight": after_weight,
        "gross_exposure": after_gross,
        "net_exposure": after_net,
        "symbol_concentration": after_weight,
    }
    sector = str(evidence.get("sector") or "").strip() or next(
        (str(item.get("sector") or "").strip() for item in positions
         if str(item.get("symbol") or "").upper() == expression.ticker.upper()),
        "",
    )
    sector_delta = None
    if sector and all(str(item.get("sector") or "").strip() for item in positions):
        before_sector = sum(
            float(item.get("market_value") or 0) for item in positions
            if str(item.get("sector") or "").strip() == sector
        ) / nav if nav else 0.0
        sector_delta = added_value / nav if nav else None
        after["sector_concentration"] = before_sector + (sector_delta or 0.0)
        before["sector_concentration"] = before_sector
    else:
        blockers.append("stock_sector_evidence_missing")
    beta = _number(evidence.get("beta"))
    beta_delta = beta * (added_value / nav) if beta is not None and nav else None
    if beta_delta is None:
        blockers.append("stock_beta_evidence_missing")
    btc_required = _stock_btc_scenarios_required(evidence, positions, expression.ticker)
    stress_scenarios = _stock_scenario_pnl(
        evidence,
        btc_required=btc_required,
        largest_holding=_stock_largest_holding(positions),
    )
    if stress_scenarios is None:
        blockers.append("stock_stress_scenarios_missing")
    budget_available, budget_consumed = _stock_risk_budget(evidence)
    if (
        budget_available is None
        or budget_consumed is None
        or budget_available < 0
        or budget_consumed < 0
    ):
        blockers.append("stock_risk_budget_evidence_missing")
    elif planned_loss is None or not math.isclose(budget_consumed, planned_loss, rel_tol=1e-9, abs_tol=1e-6):
        blockers.append("stock_risk_budget_use_mismatch")
    elif budget_available < budget_consumed:
        blockers.append("stock_risk_budget_exceeded")
    liquidity_evidence = evidence.get("liquidity")
    liquidity_evidence = liquidity_evidence if isinstance(liquidity_evidence, Mapping) else {}
    liquidity_status = str(_pick(liquidity_evidence, "status", "availability") or "").lower()
    if liquidity_status and liquidity_status != "available":
        blockers.append("stock_liquidity_unavailable")
    adv = _number(_pick(liquidity_evidence, "avg_dollar_volume", "average_dollar_volume", "adv"))
    if adv is None:
        adv = _number(_pick(evidence, "avg_dollar_volume", "average_dollar_volume", "adv"))
    adv_participation = added_value / adv if adv and adv > 0 else None
    if adv_participation is None:
        blockers.append("stock_adv_evidence_missing")
    participation_limit = _number(
        _pick(liquidity_evidence, "adv_participation_limit", "max_adv_participation")
    )
    if participation_limit is None:
        participation_limit = _number(
            _pick(evidence, "adv_participation_limit", "max_adv_participation")
        )
    if participation_limit is None or participation_limit <= 0 or participation_limit > 1:
        blockers.append("stock_adv_participation_limit_missing")
    if adv_participation is not None and participation_limit is not None and adv_participation > participation_limit:
        blockers.append("stock_adv_participation_exceeds_limit")
    liquidity = None
    days_to_exit = None
    if (
        adv is not None
        and adv > 0
        and participation_limit is not None
        and 0 < participation_limit <= 1
        and adv_participation is not None
        and adv_participation <= participation_limit
        and liquidity_status in {"", "available"}
    ):
        liquidity = {
            **dict(liquidity_evidence),
            "status": "available",
            "avg_dollar_volume": adv,
            "adv_participation_limit": participation_limit,
            "adv_participation": adv_participation,
        }
        days_to_exit = (owned + added_value) / (adv * participation_limit)
    if not liquidity_evidence and not any(
        evidence.get(key) is not None
        for key in ("avg_dollar_volume", "average_dollar_volume", "adv", "adv_participation_limit", "max_adv_participation")
    ):
        blockers.append("stock_liquidity_evidence_missing")
    correlation_delta = _number(evidence.get("correlation_cluster_delta"))
    if correlation_delta is None:
        blockers.append("stock_correlation_evidence_missing")
    cash_comparator = evidence.get("cash_comparator")
    cash_comparator = dict(cash_comparator) if isinstance(cash_comparator, Mapping) else None
    cash_status = str(_pick(cash_comparator or {}, "status", "availability") or "").lower()
    if (
        cash_comparator is None
        or cash_status != "available"
        or not any(_number(cash_comparator.get(key)) is not None for key in _STOCK_CASH_COMPARISON_KEYS)
    ):
        blockers.append("stock_cash_comparator_missing")
        cash_comparator = None
    funding, trim = _stock_funding_evidence(evidence)
    if funding is None:
        blockers.append("stock_funding_evidence_missing")
    top_alternative = _stock_top_alternative(evidence, cash_comparator, replay)
    if top_alternative is None:
        blockers.append("stock_top_alternative_missing")
    if funding is not None:
        after["funding_source_or_position_to_trim"] = funding
    if trim is not None:
        after["position_to_trim_or_replace"] = trim
    values = {
        "position_weight_before": before["position_weight"],
        "position_weight_after": after["position_weight"],
        "gross_exposure_before": before["gross_exposure"],
        "gross_exposure_after": after["gross_exposure"],
        "net_exposure_before": before["net_exposure"],
        "net_exposure_after": after["net_exposure"],
        "symbol_concentration_delta": (after_weight - before["position_weight"]) if after_weight is not None else None,
        "sector_concentration_delta": sector_delta,
        "beta_delta": beta_delta,
        "correlation_cluster_delta": correlation_delta,
        "planned_loss": planned_loss,
        "adv_participation": adv_participation,
        "days_to_exit": days_to_exit,
        "marginal_risk": budget_consumed,
        "risk_budget_consumed": budget_consumed,
        "expected_transaction_costs": _number(evidence.get("expected_transaction_costs")),
        "tail_risk_penalty": _number(evidence.get("tail_risk_penalty")),
        "portfolio_overlap_penalty": _number(evidence.get("portfolio_overlap_penalty")),
        "diversification_benefit": _number(evidence.get("diversification_benefit")),
        "scenario_pnl": stress_scenarios,
        "liquidity": liquidity,
        "cash_comparator": cash_comparator,
        "top_alternative": top_alternative,
        "position_to_trim_or_replace": trim,
        "funding_source_or_position_to_trim": funding,
        "impact_method": "stock_portfolio_impact.v1:first_order",
    }
    return before, after, values, list(dict.fromkeys(blockers))


def compose_portfolio_impact(
    *,
    episode: OpportunityEpisode,
    expression: ExpressionDecision,
    snapshot: MarketStateSnapshot,
    policy_version: str,
    portfolio_replay: Mapping[str, Any] | None,
) -> PortfolioImpact:
    """Build the one fail-closed impact shape shared by local and published paths."""

    before = dict(portfolio_replay or {})
    kind = expression.kind
    expression_identity = _expression_identity_for(expression, kind, episode.ticker, episode.decision_revision)
    blockers = _portfolio_book_blockers(before, episode.cutoff)
    if kind is ExpressionKind.CASH and not blockers:
        after = dict(before)
        availability = "available"
        values = {
            "marginal_risk": 0.0,
            "risk_budget_consumed": 0.0,
            "scenario_pnl": {"status": "zero_impact", "pnl": 0.0},
            "factor_exposure": None,
            "greeks": None,
            "liquidity": {"status": "not_applicable"},
        }
    elif kind in {
        ExpressionKind.STOCK,
        ExpressionKind.CRYPTO_SPOT,
        ExpressionKind.CRYPTO_PERPETUAL,
    } and not blockers:
        before_book = dict(before)
        before_metrics, after_metrics, stock_values, stock_blockers = _stock_impact_values(expression, before)
        after = {**before, "ticker": expression.ticker, "stock_impact": after_metrics}
        blockers.extend(stock_blockers)
        availability = "unavailable" if blockers else "available"
        values = {
            **{
                "marginal_risk": None,
                "risk_budget_consumed": None,
                "scenario_pnl": None,
                "factor_exposure": None,
                "greeks": None,
                "liquidity": None,
            },
            **stock_values,
        }
        before = {**before_book, "ticker": expression.ticker, "stock_impact": before_metrics}
    else:
        after = {}
        availability = "unavailable"
        if kind is not ExpressionKind.CASH:
            blockers.extend((
                "portfolio_marginal_risk_unsupported",
                "portfolio_risk_budget_unsupported",
                "portfolio_scenario_pnl_unsupported",
                "portfolio_factor_exposure_unsupported",
                "portfolio_greeks_unsupported",
                "portfolio_liquidity_unsupported",
                "portfolio_opportunity_cost_unsupported",
                "portfolio_overlap_unsupported",
                "portfolio_diversification_unsupported",
                "portfolio_capital_at_risk_unsupported",
                "portfolio_stress_evidence_unsupported",
            ))
        values = {
            "marginal_risk": None,
            "risk_budget_consumed": None,
            "scenario_pnl": None,
            "factor_exposure": None,
            "greeks": None,
            "liquidity": None,
        }
    impact = PortfolioImpact(
        impact_id="pending",
        ticker=episode.ticker,
        opportunity_episode_id=episode.episode_id,
        expression_kind=kind,
        expression_identity=expression_identity,
        decision_revision=episode.decision_revision,
        risk_policy_version=policy_version,
        market_snapshot_id=snapshot.snapshot_id,
        market_state_publication_id=snapshot.publication_id,
        cutoff=episode.cutoff,
        input_lineage=tuple(episode.input_lineage),
        portfolio_before=before,
        portfolio_after=after,
        availability=availability,
        blockers=tuple(dict.fromkeys(blockers)),
        **values,
    )
    return impact.model_copy(update={"impact_id": _portfolio_impact_id(impact)})


def _local_portfolio_impacts(
    *,
    episode: OpportunityEpisode,
    snapshot: MarketStateSnapshot,
    policy: RiskPolicySnapshot,
    expressions: Mapping[ExpressionKind, ExpressionDecision],
    nav: float | None,
    owned: bool,
    portfolio_replay: Mapping[str, Any] | None,
) -> dict[ExpressionKind, PortfolioImpact]:
    return {
        kind: compose_portfolio_impact(
            episode=episode,
            expression=expression,
            snapshot=snapshot,
            policy_version=policy.policy_version,
            portfolio_replay=portfolio_replay,
        )
        for kind, expression in expressions.items()
    }


def _context_blockers_for(
    *,
    snapshot: MarketStateSnapshot | None,
    policy: RiskPolicySnapshot | None,
    impacts: Mapping[ExpressionKind, PortfolioImpact],
    expressions: Mapping[ExpressionKind, ExpressionDecision],
) -> list[str]:
    blockers: list[str] = []
    selected = next((expression for expression in expressions.values() if expression.selected), None)
    selected_kind = selected.kind if selected is not None else ExpressionKind.CASH
    if snapshot is None:
        if selected_kind is not ExpressionKind.CASH:
            blockers.append("market_state_missing")
    else:
        if selected_kind is not ExpressionKind.CASH:
            if snapshot.contract_version == "market-state-snapshot.v1" and snapshot.availability != "available":
                blockers.append("market_state_unavailable")
            if snapshot.contract_version == "market-state-snapshot.v2":
                assessment = market_evidence_for_decision(snapshot, selected_kind, selected.horizon if selected else "FUNDAMENTAL")
                blockers.extend(assessment.blockers)
            if not snapshot.publication_id:
                blockers.append("market_state_publication_missing")
    if policy is None:
        blockers.append("risk_policy_snapshot_missing")
    else:
        blockers.extend(policy.blockers)
    expected = [selected_kind] if selected_kind is not ExpressionKind.CASH else []
    if selected_kind is not ExpressionKind.CASH:
        expected.append(ExpressionKind.CASH)
    for kind in expected:
        impact = impacts.get(kind)
        if impact is None:
            blockers.append(f"portfolio_impact_missing:{kind.value}")
        elif impact.availability != "available":
            blockers.append(f"portfolio_impact_unavailable:{kind.value}")
        elif impact.blockers:
            blockers.extend(impact.blockers)
    return list(dict.fromkeys(blockers))


def build_ticker_decision(
    ticker: str,
    tables: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    as_of: datetime | None = None,
    code_version: str = CONTRACT_VERSION,
    experiment_id: str = EXPERIMENT_ID,
    market_state_snapshot: MarketStateSnapshot | Mapping[str, Any] | None | object = _CONTEXT_UNSET,
    portfolio_impacts: Mapping[ExpressionKind | str, PortfolioImpact | Mapping[str, Any]] | None | object = _CONTEXT_UNSET,
    risk_policy_snapshot: RiskPolicySnapshot | Mapping[str, Any] | None | object = _CONTEXT_UNSET,
    portfolio_replay: Mapping[str, Any] | None = None,
) -> TickerDecision:
    """Build one deterministic ticker decision from point-in-time rows."""

    symbol = ticker.strip().upper()
    if not symbol:
        raise ValueError("ticker is required")
    reference = _utc(as_of or datetime.now(UTC))
    usable = {
        name: _usable_rows(rows, symbol, reference)
        for name, rows in tables.items()
    }
    if portfolio_replay is not None:
        replay = dict(portfolio_replay)
        evidence = dict(replay.get("stock_evidence") or {})
        for table_name in ("fundamentals", "technicals", "liquidity", "portfolio"):
            row = _latest(usable, table_name)
            for target, keys in {
                "sector": ("sector",),
                "beta": ("beta", "market_beta", "beta_1y"),
                "avg_dollar_volume": ("avg_dollar_volume", "average_dollar_volume"),
                "correlation_cluster_delta": ("correlation_cluster_delta",),
            }.items():
                if target not in evidence:
                    value = _pick(row, *keys)
                    if value is not None:
                        evidence[target] = value
        replay["stock_evidence"] = evidence
        portfolio_replay = replay
    persisted = _latest(usable, "ticker_decisions")
    manifest = _build_manifest(usable, reference, code_version, experiment_id)
    decision_row = _latest(usable, "symbol_decision_snapshot", "symbol_decision_snapshots", "decision_queue", "opportunities_ranked", "candidates")
    quote = _latest(usable, "quotes")
    current_price = _number(_pick(quote, "price", "close", "last", "latest_price"))
    price_age = _age_seconds(quote, reference)
    if price_age is not None and price_age > 900:
        current_price = None
    holding = _latest(usable, "portfolio", "broker_positions")
    portfolio = _latest(usable, "broker_accounts", "portfolio_summary", "broker_status")
    owned = _number(_pick(holding, "quantity", "shares"), default=0.0) > 0
    if not holding:
        owned = _number(_pick(portfolio, "quantity", "shares"), default=0.0) > 0
    if portfolio_replay is not None:
        owned = any(
            str(position.get("symbol") or "").strip().upper() == symbol
            and _number(position.get("quantity"), default=0.0) > 0
            for position in portfolio_replay.get("positions") or []
            if isinstance(position, Mapping)
        )
    nav, nav_age = _portfolio_nav(portfolio, reference)
    tactical_stance = _stance(decision_row, Horizon.TACTICAL, usable)
    fundamental_stance = _stance(decision_row, Horizon.FUNDAMENTAL, usable)
    confidence = _number(_pick(decision_row, "confidence", "confidence_score"))
    if confidence is not None and confidence > 1:
        confidence /= 100
    conviction_tier = _conviction(decision_row, confidence, "EXPLORATORY")
    account_facts = {
        "broker_net_liquidation": _pick(portfolio, "broker_net_liquidation", "net_liquidation"),
        "broker_available_capital": _pick(portfolio, "broker_available_capital"),
        "cash_balance": _pick(portfolio, "cash_balance"),
        "buying_power": _pick(portfolio, "buying_power"),
        "account_source": _pick(portfolio, "account_source", "account_facts_source", "provider", "source_id"),
    }
    account_observed_at = _pick(portfolio, "account_observed_at", "observed_at", "updated_at")
    available_at = _pick(portfolio, "available_at")
    if account_observed_at is None:
        account_observed_at = available_at
    if account_observed_at is not None:
        account_facts["account_observed_at"] = account_observed_at
    if available_at is not None:
        account_facts["available_at"] = available_at
    account_observed_at = _parse_datetime(_pick(account_facts, "account_observed_at", "available_at"))
    policy_blockers: list[str] = []
    if account_observed_at is None or nav is None:
        policy_blockers.append("fresh_postgres_account_facts_required")
    elif account_observed_at > reference:
        policy_blockers.append("future_account_revision_not_allowed")
    elif (reference - account_observed_at).total_seconds() > 1800:
        policy_blockers.append("fresh_postgres_account_facts_required")
    account_source = account_facts["account_source"]
    if account_source is not None and str(account_source).strip().lower() not in {
        "postgresql", "postgres", "raw.broker_account_snapshot",
    }:
        policy_blockers.append("postgresql_account_facts_required")
    canonical_policy_snapshot = compile_risk_policy_snapshot(
        account_facts=account_facts,
        sleeve_capital=nav,
        conviction_tier=conviction_tier,
        policy_kind="ticker",
        additional_blockers=policy_blockers,
    )
    risk_policy = _risk_policy(canonical_policy_snapshot)

    if persisted:
        try:
            # Published revisions remain immutable, but current account
            # authority must still gate their use.
            persisted_resolution = resolution_from_legacy({
                **persisted,
                "ticker": symbol,
                "resolution": persisted.get("resolution"),
            })
            persisted_policy_snapshot = None
            if persisted.get("risk_policy_snapshot") is not None:
                try:
                    persisted_policy_snapshot = (
                        persisted["risk_policy_snapshot"]
                        if isinstance(persisted["risk_policy_snapshot"], RiskPolicySnapshot)
                        else RiskPolicySnapshot.model_validate(persisted["risk_policy_snapshot"])
                    )
                except Exception:
                    pass
            authority_blockers = list(canonical_policy_snapshot.blockers)
            if (
                persisted_policy_snapshot is None
                or persisted_policy_snapshot.model_dump(mode="json")
                != canonical_policy_snapshot.model_dump(mode="json")
            ):
                authority_blockers.append("risk_policy_snapshot_mismatch")
            if risk_policy_snapshot is not _CONTEXT_UNSET:
                if risk_policy_snapshot is None:
                    authority_blockers.append("risk_policy_snapshot_missing")
                else:
                    try:
                        supplied_policy_snapshot = (
                            risk_policy_snapshot
                            if isinstance(risk_policy_snapshot, RiskPolicySnapshot)
                            else RiskPolicySnapshot.model_validate(risk_policy_snapshot)
                        )
                    except Exception:
                        supplied_policy_snapshot = None
                    if (
                        supplied_policy_snapshot is None
                        or supplied_policy_snapshot.model_dump(mode="json")
                        != canonical_policy_snapshot.model_dump(mode="json")
                    ):
                        authority_blockers.append("risk_policy_snapshot_mismatch")
            authority_blockers = list(dict.fromkeys(authority_blockers))
            stored_policy_version = (
                persisted.get("policy_version")
                or (persisted.get("risk_policy") or {}).get("policy_version")
                or "risk-policy.v2:legacy"
            )
            if persisted_policy_snapshot is None:
                persisted_policy_snapshot = RiskPolicySnapshot(
                    policy_version=stored_policy_version,
                    blockers=tuple(authority_blockers + ["risk_policy_snapshot_missing"]),
                )
            elif authority_blockers:
                persisted_policy_snapshot = persisted_policy_snapshot.model_copy(update={
                    "blockers": tuple(dict.fromkeys((*persisted_policy_snapshot.blockers, *authority_blockers))),
                })
            if authority_blockers:
                persisted_resolution = build_decision_resolution(
                    action="NO_TRADE",
                    decision_revision=persisted_resolution.decision_revision,
                    policy_version=stored_policy_version,
                    trade_plan_id=persisted_resolution.trade_plan_id,
                    provenance=persisted_resolution.provenance,
                    ticker=symbol,
                    blockers=[authority_blockers[0]],
                    data_quality="INCOMPLETE",
                    authorization_mode="NONE",
                    rationale=persisted_resolution.rationale,
                    owned=persisted_resolution.owned,
                    price_condition=persisted_resolution.price_condition,
                    catalyst=persisted_resolution.catalyst,
                    expires_at=persisted_resolution.expires_at,
                    blocked=True,
                )
            persisted_impacts = portfolio_impacts_from_persisted(
                persisted.get("portfolio_impacts") or {}, ticker=symbol,
            )
            persisted_trade_plan = None
            if not authority_blockers:
                raw_trade_plan = persisted.get("trade_plan") or (
                    persisted.get("input_manifest") or {}
                ).get("trade_plan")
                persisted_trade_plan = trade_plan_from_persisted(raw_trade_plan, ticker=symbol)
            return TickerDecision.model_validate({
                "decision_contract_version": persisted.get("contract_version") or CONTRACT_VERSION,
                "ticker": symbol,
                "as_of": persisted.get("as_of") or reference,
                "decision_revision": persisted.get("decision_revision"),
                "tactical": persisted.get("tactical"),
                "fundamental": persisted.get("fundamental"),
                "capital_action": capital_action_from_resolution(persisted_resolution),
                "resolution": persisted_resolution,
                "policy_version": stored_policy_version,
                "risk_policy": persisted.get("risk_policy"),
                "expressions": persisted.get("expressions") or {},
                "selected_expression": persisted.get("selected_expression"),
                "opportunity_episode": persisted.get("opportunity_episode") or None,
                "data_requests": persisted.get("data_requests") or [],
                "learning_history": persisted.get("learning_history") or [],
                "input_manifest": persisted.get("input_manifest") or {},
                "risk_policy_snapshot": persisted_policy_snapshot,
                "market_state_publication_id": persisted.get("market_state_publication_id") or None,
                "market_state_snapshot": persisted.get("market_state_snapshot") or None,
                "portfolio_impacts": persisted_impacts,
                "instrument_state_snapshot": persisted.get("instrument_state_snapshot") or None,
                "alpha_signals": persisted.get("alpha_signals") or [],
                "opportunity_rank": persisted.get("opportunity_rank") or None,
                "trade_plan": persisted_trade_plan,
            })
        except Exception:
            # A malformed persisted row is visible to source-health/learning
            # diagnostics, but it must not make the ticker route disappear.
            pass

    requests: list[DataRequest] = []
    if current_price is None:
        price_reason = "A current confirmed price is required for an exact entry range and stock sizing."
        if price_age is not None:
            price_reason = f"Confirmed price is {max(1, round(price_age / 60))} minutes old; run update_market_data before sizing."
        requests.append(_request(
            field="current_price", ticker=symbol,
            why=price_reason,
            source="confirmed quote selector", max_age="15m", max_age_seconds=900,
            owner="update_market_data", collect_now="update_market_data",
            expected="A confirmed quote with price and available_at <= as_of.",
            impact="The entry range and stock quantity become executable.",
        ))
    if nav is None:
        nav_reason = "NAV is required to calculate the configured ticker loss budget."
        if nav_age is not None:
            nav_reason = (
                f"Run update_broker_account; NAV is {max(1, round(nav_age / 60))} minutes old, "
                f"so the {risk_policy.loss_budget_pct:.1%} loss budget cannot be calculated."
            )
        requests.append(_request(
            field="portfolio_nav", ticker=symbol,
            why=nav_reason,
            source="broker account snapshot", max_age="30m", max_age_seconds=1800,
            owner="update_broker_account", collect_now="update_broker_account",
            expected="A fresh finite net_liquidation value.",
            impact="The loss budget and all expression quantities become numeric.",
        ))

    tactical = _build_view(
        Horizon.TACTICAL, tactical_stance, decision_row, usable, current_price,
        risk_policy, symbol, manifest.input_hash, reference,
    )
    fundamental = _build_view(
        Horizon.FUNDAMENTAL, fundamental_stance, decision_row, usable, current_price,
        risk_policy, symbol, manifest.input_hash, reference,
    )
    requests.extend(_view_requests(symbol, tactical, fundamental, usable))
    requests.extend(_signal_requests(symbol, usable, reference))
    requests = _dedupe_requests(requests)

    expressions = _build_expressions(
        symbol=symbol,
        horizon=_expression_horizon(tactical, fundamental),
        stance=_expression_stance(tactical, fundamental),
        entry_range=tactical.entry_range if tactical.horizon is Horizon.TACTICAL else fundamental.entry_range,
        target_range=tactical.target_range if tactical.horizon is Horizon.TACTICAL else fundamental.target_range,
        invalidation=tactical.invalidation if tactical.horizon is Horizon.TACTICAL else fundamental.invalidation,
        scenarios=(fundamental if fundamental.stance is not Stance.NEUTRAL else tactical).scenarios,
        expected_return_range=(fundamental if fundamental.stance is not Stance.NEUTRAL else tactical).expected_return_range,
        risk_policy=risk_policy,
        current_price=current_price,
        nav=nav,
        usable=usable,
        thesis_revision=manifest.input_hash,
        requests=requests,
    )
    tactical, fundamental, expressions = _select_expressions(tactical, fundamental, expressions)
    capital = _capital_action(
        symbol, tactical, fundamental, owned,
        expressions.get(ExpressionKind.CASH),
        _catalyst(decision_row, usable),
    )
    selected = _selected_expression(expressions, capital.action)
    for expression in expressions.values():
        expression.selected = selected is not None and expression.kind is selected.kind
    decision_revision = f"{CONTRACT_VERSION}:{manifest.input_hash[:16]}"
    input_lineage = _build_input_lineage(
        manifest,
        decision_revision=decision_revision,
        policy_version=risk_policy.policy_version,
        cutoff=reference,
    )
    episode = build_opportunity_episode(
        ticker=symbol,
        decision_revision=decision_revision,
        policy_version=risk_policy.policy_version,
        cutoff=reference,
        input_lineage=input_lineage,
        expressions=expressions,
        selected_expression=selected,
    )
    context_supplied = any(
        value is not _CONTEXT_UNSET
        for value in (market_state_snapshot, portfolio_impacts, risk_policy_snapshot)
    )
    if market_state_snapshot is _CONTEXT_UNSET:
        snapshot = _local_market_snapshot(reference, episode.input_lineage)
    elif market_state_snapshot is None:
        snapshot = None
    else:
        snapshot = (
            market_state_snapshot
            if isinstance(market_state_snapshot, MarketStateSnapshot)
            else MarketStateSnapshot.model_validate(market_state_snapshot)
        )
    policy_snapshot_mismatch = False
    if risk_policy_snapshot is _CONTEXT_UNSET:
        policy_snapshot = canonical_policy_snapshot
    elif risk_policy_snapshot is None:
        policy_snapshot = None
    else:
        try:
            supplied_policy_snapshot = (
                risk_policy_snapshot
                if isinstance(risk_policy_snapshot, RiskPolicySnapshot)
                else RiskPolicySnapshot.model_validate(risk_policy_snapshot)
            )
        except Exception:
            supplied_policy_snapshot = None
        policy_snapshot_mismatch = (
            supplied_policy_snapshot is None
            or supplied_policy_snapshot.model_dump(mode="json")
            != canonical_policy_snapshot.model_dump(mode="json")
        )
        policy_snapshot = canonical_policy_snapshot
        if policy_snapshot_mismatch:
            policy_snapshot = policy_snapshot.model_copy(update={
                "blockers": tuple(dict.fromkeys((*policy_snapshot.blockers, "risk_policy_snapshot_mismatch"))),
            })
    if portfolio_impacts is _CONTEXT_UNSET:
        impacts = _local_portfolio_impacts(
            episode=episode,
            snapshot=snapshot or _missing_market_snapshot(reference),
            policy=policy_snapshot or RiskPolicySnapshot(
                policy_version=risk_policy.policy_version,
                blockers=("risk_policy_snapshot_missing",),
            ),
            expressions=expressions,
            nav=nav,
            owned=owned,
            portfolio_replay=portfolio_replay,
        )
    elif portfolio_impacts is None:
        impacts = {}
    else:
        impacts = {
            ExpressionKind(kind): value if isinstance(value, PortfolioImpact) else PortfolioImpact.model_validate(value)
            for kind, value in portfolio_impacts.items()
        }
    context_blockers = (
        _context_blockers_for(
            snapshot=snapshot,
            policy=policy_snapshot,
            impacts=impacts,
            expressions=expressions,
        )
        if context_supplied
        else list(canonical_policy_snapshot.blockers)
    )
    selected_entry = selected.entry_range if selected is not None else None
    selected_invalidation = selected.invalidation if selected is not None else None
    selected_exit = selected.target_range if selected is not None else None
    resolution_blockers = [
        request.field
        for expression in (
            selected,
            expressions.get(ExpressionKind.CASH),
        )
        if expression is not None
        for request in expression.data_requests
    ]
    if selected is not None:
        resolution_blockers.extend(selected.blockers)
    resolution_blockers.extend(context_blockers)
    selected_impact = impacts.get(selected.kind) if selected is not None else impacts.get(ExpressionKind.CASH)
    resolution = build_decision_resolution(
        action=capital.action.value,
        decision_revision=decision_revision,
        policy_version=risk_policy.policy_version,
        provenance={
            "as_of": reference,
            "available_at": reference,
            "input_hash": manifest.input_hash,
            "source_versions": manifest.source_versions,
            "revisions": {"contract": CONTRACT_VERSION, "experiment": experiment_id},
        },
        ticker=symbol,
        blockers=context_blockers or resolution_blockers,
        entry=selected_entry or tactical.entry_range or fundamental.entry_range,
        size=selected.quantity if selected is not None else None,
        invalidation=selected_invalidation or tactical.invalidation or fundamental.invalidation,
        exit=selected_exit or tactical.target_range or fundamental.target_range,
        ttl=capital.expires_at or min(tactical.expiry_date, fundamental.expiry_date),
        portfolio_context=(
            selected_impact.model_dump(mode="json")
            if selected_impact is not None
            else {"status": "missing", "blockers": ["portfolio_impact_missing"]}
        ),
        data_quality="COMPLETE" if not resolution_blockers else "INCOMPLETE",
        authorization_mode="ADVISORY",
        rationale=capital.rationale,
        owned=capital.owned,
        price_condition=capital.price_condition,
        catalyst=capital.catalyst,
        expires_at=capital.expires_at,
        blocked=bool(context_blockers),
    )
    capital = capital_action_from_resolution(resolution)
    learning = [
        dict(row)
        for row in (
            usable.get("learning_history")
            or usable.get("ticker_outcomes")
            or usable.get("outcomes")
            or []
        )
    ]
    return TickerDecision(
        ticker=symbol,
        as_of=reference,
        decision_revision=decision_revision,
        tactical=tactical,
        fundamental=fundamental,
        capital_action=capital,
        resolution=resolution,
        policy_version=risk_policy.policy_version,
        risk_policy=risk_policy,
        expressions=expressions,
        selected_expression=selected,
        opportunity_episode=episode,
        data_requests=requests,
        learning_history=learning,
        input_manifest=manifest,
        risk_policy_snapshot=policy_snapshot,
        market_state_publication_id=snapshot.publication_id if snapshot is not None else None,
        market_state_snapshot=snapshot,
        portfolio_impacts=impacts,
    )


def _build_view(
    horizon: Horizon,
    stance: Stance,
    decision_row: Mapping[str, Any],
    tables: Mapping[str, list[dict[str, Any]]],
    current_price: float | None,
    risk_policy: RiskPolicy,
    symbol: str,
    thesis_revision: str,
    as_of: datetime,
) -> HorizonDecision:
    entry = _price_range(decision_row, "entry", current_price if stance is not Stance.NEUTRAL else None)
    target = _price_range(decision_row, "target")
    invalidation = _invalidation(decision_row, horizon)
    if invalidation is None:
        invalidation = _invalidation(_latest(tables, "theses", "thesis_monitor", "research_packets"), horizon)
    expiry = _expiry_date(decision_row, horizon, as_of)
    confidence = _confidence(decision_row, horizon, tables)
    tier = _conviction(decision_row, confidence, risk_policy.conviction_tier)
    expected = _numeric_range(decision_row, "expected_return")
    if expected is None:
        expected = _valuation_return(tables) if horizon is Horizon.FUNDAMENTAL else _numeric_range(_latest(tables, "technicals"), "expected_return")
    scenarios = _scenarios(decision_row, stance, current_price, target)
    evidence_for = _evidence(decision_row, EvidencePolarity.FOR, tables, horizon)
    evidence_against = _evidence(decision_row, EvidencePolarity.AGAINST, tables, horizon)
    flip = _flip_fact(decision_row, symbol, horizon, as_of)
    action = _view_action(stance)
    selected = _expression_preference(horizon, tables, stance)
    alternate = ExpressionKind.CASH if selected is not ExpressionKind.CASH else ExpressionKind.STOCK
    basis = _confidence_basis(decision_row, tables, horizon, invalidation, expected)
    if current_price is None:
        basis.append("confirmed current price is missing; no quantity is invented")
    if invalidation is None:
        basis.append("invalidation is missing; directional view remains published but sizing is blocked")
    return HorizonDecision(
        horizon=horizon,
        stance=stance,
        action=action,
        current_price=current_price,
        entry_range=entry,
        target_range=target,
        expiry_date=expiry,
        scenarios=scenarios,
        invalidation=invalidation,
        conviction_tier=tier,
        confidence=confidence,
        confidence_basis=basis,
        expected_return_range=expected,
        evidence_for=evidence_for,
        evidence_against=evidence_against,
        fact_that_would_flip=flip,
        selected_instrument=selected,
        alternate_expression=alternate,
    )


def _build_expressions(
    *,
    symbol: str,
    horizon: Horizon,
    stance: Stance,
    entry_range: PriceRange | None,
    target_range: PriceRange | None,
    invalidation: Invalidation | None,
    scenarios: list[ScenarioOutcome],
    expected_return_range: NumericRange | None,
    risk_policy: RiskPolicy,
    current_price: float | None,
    nav: float | None,
    usable: Mapping[str, list[dict[str, Any]]],
    thesis_revision: str,
    requests: list[DataRequest],
) -> dict[ExpressionKind, ExpressionDecision]:
    loss_budget = risk_policy.loss_budget
    entry_price = _midpoint(entry_range)
    invalidation_price = _number(invalidation.value) if invalidation and invalidation.kind == "price" else None
    per_share_loss = entry_price - invalidation_price if entry_price is not None and invalidation_price is not None else None
    stock_quantity = None
    stock_planned_loss = None
    if loss_budget is not None and per_share_loss is not None and per_share_loss > 0:
        stock_quantity = _bounded_quantity(loss_budget / per_share_loss, nav, entry_price, risk_policy.position_limit_pct)
        stock_planned_loss = stock_quantity * per_share_loss
    risk_fraction = per_share_loss / entry_price if per_share_loss is not None and entry_price and entry_price > 0 else None
    expected_mid = ((expected_return_range.low + expected_return_range.high) / 2) if expected_return_range else None
    expected_low = expected_return_range.low if expected_return_range else None
    stock_status = "eligible" if entry_price is not None and invalidation_price is not None else "blocked"
    stock_request = [request for request in requests if request.field in {"current_price", "invalidation", "portfolio_nav"}]
    stock = ExpressionDecision(
        kind=ExpressionKind.STOCK,
        ticker=symbol,
        horizon=horizon,
        thesis_revision=thesis_revision,
        stance=stance,
        scenarios=list(scenarios),
        entry_range=entry_range,
        target_range=target_range,
        invalidation=invalidation,
        quantity=stock_quantity,
        loss_budget=loss_budget,
        max_loss_per_unit=per_share_loss,
        planned_loss=stock_planned_loss,
        net_expected_value_per_loss_dollar=(expected_mid / risk_fraction if expected_mid is not None and risk_fraction and risk_fraction > 0 else None),
        lower_confidence_expectancy=(expected_low / risk_fraction if expected_low is not None and risk_fraction and risk_fraction > 0 else None),
        liquidity_score=1.0 if current_price is not None else None,
        spread_pct=None,
        fill_probability=1.0 if current_price is not None else None,
        horizon_fit=1.0 if horizon is Horizon.FUNDAMENTAL else 0.8,
        status=stock_status,
        rationale="Stock is the default expression for a slower ticker thesis with full upside participation." if horizon is Horizon.FUNDAMENTAL else "Stock preserves the shared ticker thesis without option decay.",
        data_requests=stock_request,
    )
    output: dict[ExpressionKind, ExpressionDecision] = {ExpressionKind.STOCK: stock}
    option_rows = usable.get("options_payoff_scenarios", []) or usable.get("options_ticker_signals", [])
    for row in option_rows:
        kind = _expression_kind(row)
        if kind is None:
            continue
        details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
        legs = _legs(row)
        max_loss = _number(_pick(row, "max_loss", "one_unit_max_loss", "maximum_loss"))
        if max_loss is None:
            premium = _number(_pick(row, "premium_mid", "entry_price", "limit_price"))
            max_loss = premium * 100 if premium is not None and kind is not ExpressionKind.CASH_SECURED_PUT else None
        quantity = None
        planned_loss = None
        if loss_budget is not None and max_loss is not None and max_loss > 0:
            quantity = max(0, math.floor(loss_budget / max_loss))
            planned_loss = quantity * max_loss
        executable_quote = bool(legs) and all(
            _number(_pick(leg, "bid")) is not None
            and _number(_pick(leg, "ask")) is not None
            and _number(_pick(leg, "bid_size")) is not None
            and _number(_pick(leg, "ask_size")) is not None
            and _pick(leg, "quote_time", "observed_at") is not None
            for leg in legs
        )
        status = "eligible" if max_loss is not None and executable_quote else "blocked"
        output[kind] = ExpressionDecision(
            kind=kind,
            ticker=symbol,
            horizon=horizon,
            thesis_revision=thesis_revision,
            stance=stance,
            scenarios=list(scenarios),
            legs=_legs(row),
            entry_range=_price_range(row, "entry"),
            target_range=target_range,
            invalidation=invalidation,
            quantity=quantity,
            loss_budget=loss_budget,
            max_loss_per_unit=max_loss,
            planned_loss=planned_loss,
            expected_transaction_costs=_number(_pick(row, "expected_transaction_costs", "transaction_costs", "estimated_fees") or _pick(details, "expected_transaction_costs", "transaction_costs", "estimated_fees")),
            net_expected_value_per_loss_dollar=_number(_pick(row, "net_expected_value_per_loss_dollar", "ev_per_loss_dollar", "expected_value") or _pick(details, "net_expected_value_per_loss_dollar", "ev_per_loss_dollar")),
            lower_confidence_expectancy=_number(_pick(row, "lower_confidence_expectancy", "lower_95_expected_value") or _pick(details, "lower_confidence_expectancy", "lower_95_expected_value")),
            liquidity_score=_bounded(_number(_pick(row, "liquidity_score", "liquidity")), 0, 1),
            spread_pct=_number(_pick(row, "spread_pct", "spread")),
            fill_probability=_bounded(_number(_pick(row, "fill_probability", "fill_prob")), 0, 1),
            horizon_fit=_bounded(_number(_pick(row, "horizon_fit")), 0, 1),
            status=status,
            rationale=(
                f"{kind.value.replace('_', ' ').title()} is compared against stock on the same "
                f"{horizon.value.lower()} ticker thesis."
                if executable_quote
                else "Option expression remains blocked until a complete executable bid/ask and size package is available."
            ),
            data_requests=[request for request in requests if request.field in {"option_quote", "portfolio_nav"}],
        )
    # Crypto expressions use only rows already present in the bounded input
    # snapshot. There is no fallback provider or synthetic quote. An incomplete
    # row is retained as unavailable evidence and therefore cannot authorize a
    # position or replace the canonical CASH comparator.
    crypto_rows = [
        *usable.get("crypto_spot", []),
        *usable.get("crypto_spot_quotes", []),
        *usable.get("crypto_perpetual", []),
        *usable.get("crypto_perpetual_quotes", []),
        *usable.get("crypto_expressions", []),
    ]
    for row in crypto_rows:
        structure = str(_pick(row, "expression_kind", "expression", "kind", "instrument_type", "market_type") or "").lower().replace("-", "_").replace(" ", "_")
        kind = (
            ExpressionKind.CRYPTO_SPOT
            if structure in {"crypto_spot", "spot", "crypto", "crypto_asset"}
            else ExpressionKind.CRYPTO_PERPETUAL
            if structure in {"crypto_perpetual", "perpetual", "perp", "crypto_perp"}
            else None
        )
        if kind is None:
            continue
        details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
        price = _number(_pick(row, "price", "mid", "mark", "last", "close") or _pick(details, "price", "mid", "mark"))
        bid = _number(_pick(row, "bid") or _pick(details, "bid"))
        ask = _number(_pick(row, "ask") or _pick(details, "ask"))
        observed_at = _pick(row, "quote_time", "observed_at", "available_at")
        executable = price is not None and price > 0 and observed_at is not None
        if kind is ExpressionKind.CRYPTO_PERPETUAL:
            executable = executable and bid is not None and ask is not None and ask >= bid
        blockers = () if executable else ("crypto_evidence_unavailable",)
        expected_value = _number(_pick(row, "net_utility", "net_expected_value_per_loss_dollar", "expected_value") or _pick(details, "net_utility", "expected_value"))
        costs = _number(_pick(row, "expected_transaction_costs", "transaction_costs", "fees") or _pick(details, "expected_transaction_costs", "transaction_costs", "fees"))
        if expected_value is not None and costs is not None:
            expected_value -= costs
        output[kind] = ExpressionDecision(
            kind=kind,
            ticker=symbol,
            horizon=horizon,
            thesis_revision=thesis_revision,
            stance=stance,
            scenarios=list(scenarios),
            entry_range=PriceRange(low=price, high=price) if price is not None and price >= 0 else None,
            target_range=target_range,
            invalidation=invalidation,
            quantity=1 if executable else None,
            loss_budget=loss_budget,
            max_loss_per_unit=_number(_pick(row, "max_loss", "max_loss_per_unit")),
            planned_loss=_number(_pick(row, "planned_loss")),
            expected_transaction_costs=costs,
            net_expected_value_per_loss_dollar=expected_value,
            lower_confidence_expectancy=_number(_pick(row, "lower_confidence_expectancy", "lower_95_expected_value")),
            liquidity_score=_bounded(_number(_pick(row, "liquidity_score", "liquidity")), 0, 1),
            spread_pct=_number(_pick(row, "spread_pct", "spread")),
            fill_probability=_bounded(_number(_pick(row, "fill_probability", "fill_prob")), 0, 1),
            horizon_fit=_bounded(_number(_pick(row, "horizon_fit")), 0, 1),
            status="eligible" if executable else "unavailable",
            blockers=blockers,
            rationale=(
                f"{kind.value.replace('_', ' ').title()} is compared against stock on the same ticker thesis."
                if executable else "Crypto expression remains unavailable until bounded executable evidence is present."
            ),
            data_requests=[request for request in requests if request.field in {"crypto_quote", "portfolio_nav"}],
        )
    output.setdefault(ExpressionKind.CASH, ExpressionDecision(
        kind=ExpressionKind.CASH,
        ticker=symbol,
        horizon=horizon,
        thesis_revision=thesis_revision,
        stance=stance,
        scenarios=list(scenarios),
        loss_budget=loss_budget,
        quantity=1,
        planned_loss=0,
        net_expected_value_per_loss_dollar=0,
        lower_confidence_expectancy=0,
        liquidity_score=1,
        fill_probability=1,
        horizon_fit=1,
        status="eligible",
        rationale="Cash is a real competing expression when the thesis or execution inputs do not justify risk.",
    ))
    return output


def _select_expressions(
    tactical: HorizonDecision,
    fundamental: HorizonDecision,
    expressions: dict[ExpressionKind, ExpressionDecision],
) -> tuple[HorizonDecision, HorizonDecision, dict[ExpressionKind, ExpressionDecision]]:
    option_kind = next((kind for kind in (ExpressionKind.CALL, ExpressionKind.PUT, ExpressionKind.DEBIT_SPREAD, ExpressionKind.CASH_SECURED_PUT) if kind in expressions and expressions[kind].status == "eligible"), None)
    for view in (tactical, fundamental):
        preferred = _best_expression(expressions, view.horizon, view.stance)
        if preferred not in expressions:
            preferred = ExpressionKind.STOCK
        # Keep a directional stock recommendation visible when its arithmetic
        # is blocked by missing price, invalidation, or NAV. Cash competes with
        # the thesis, but it must not hide the best current view.
        if expressions[preferred].status != "eligible" and view.stance is Stance.NEUTRAL:
            preferred = ExpressionKind.CASH
        alternate = option_kind if preferred is ExpressionKind.STOCK and option_kind else ExpressionKind.STOCK if preferred is not ExpressionKind.STOCK else ExpressionKind.CASH
        view.selected_instrument = preferred
        view.alternate_expression = alternate
    preferred = fundamental.selected_instrument if fundamental.horizon is Horizon.FUNDAMENTAL else tactical.selected_instrument
    for kind, expression in expressions.items():
        expression.selected = kind is preferred
    return tactical, fundamental, expressions


def _best_expression(
    expressions: Mapping[ExpressionKind, ExpressionDecision],
    horizon: Horizon,
    stance: Stance,
) -> ExpressionKind:
    if stance is Stance.NEUTRAL:
        return ExpressionKind.CASH
    eligible = [
        expression for expression in expressions.values()
        if expression.status == "eligible" and expression.kind is not ExpressionKind.CASH
    ]
    if not eligible:
        return ExpressionKind.STOCK

    def score(expression: ExpressionDecision) -> tuple[float, float, float, float, float, float]:
        lower = expression.lower_confidence_expectancy if expression.lower_confidence_expectancy is not None else -1.0
        net = expression.net_expected_value_per_loss_dollar if expression.net_expected_value_per_loss_dollar is not None else -1.0
        liquidity = expression.liquidity_score if expression.liquidity_score is not None else 0.0
        fill = expression.fill_probability if expression.fill_probability is not None else 0.0
        fit = expression.horizon_fit if expression.horizon_fit is not None else (1.0 if expression.kind is ExpressionKind.STOCK else 0.0)
        slow_thesis_bonus = 0.05 if horizon is Horizon.FUNDAMENTAL and expression.kind is ExpressionKind.STOCK else 0.0
        return lower, net, liquidity, fill, fit, slow_thesis_bonus

    return max(eligible, key=score).kind


def _capital_action(
    symbol: str,
    tactical: HorizonDecision,
    fundamental: HorizonDecision,
    owned: bool,
    cash: ExpressionDecision | None,
    catalyst: str | None,
) -> CapitalAction:
    f, t = fundamental.stance, tactical.stance
    if f is Stance.BULLISH and t is Stance.BEARISH:
        action = CapitalActionType.HOLD if owned else CapitalActionType.WAIT_FOR_PRICE
        rationale = "Fundamental upside is intact, but tactical weakness requires a price-defined entry or an owned-position hold."
    elif f is Stance.BEARISH and t is Stance.BULLISH:
        action = CapitalActionType.TRIM if owned else CapitalActionType.AVOID
        rationale = "Tactical strength does not overcome the bearish fundamental view; reduce owned risk or avoid new exposure."
    elif f is Stance.BULLISH and t is Stance.BULLISH:
        action = CapitalActionType.ADD if owned else CapitalActionType.BUY
        rationale = "Tactical and fundamental views are aligned bullish."
    elif f is Stance.BEARISH and t is Stance.BEARISH:
        action = CapitalActionType.EXIT if owned else CapitalActionType.AVOID
        rationale = "Tactical and fundamental views are aligned bearish."
    else:
        action = CapitalActionType.HOLD if owned else CapitalActionType.WAIT_FOR_PRICE
        rationale = "The current evidence is not aligned enough for a new directional allocation."
    if action is CapitalActionType.WAIT_FOR_PRICE:
        entry = tactical.entry_range or fundamental.entry_range
        condition = _range_text(entry) if entry else "collect a confirmed price before entry"
        expiry = min(tactical.expiry_date, fundamental.expiry_date)
        return CapitalAction(
            ticker=symbol,
            action=action,
            owned=owned,
            rationale=rationale,
            price_condition=condition,
            catalyst=catalyst or "next decision catalyst or confirmed price update",
            expires_at=expiry,
        )
    if action is CapitalActionType.TRIM:
        trim_range = tactical.target_range or fundamental.target_range or tactical.entry_range or fundamental.entry_range
        return CapitalAction(
            ticker=symbol,
            action=action,
            owned=owned,
            rationale=rationale,
            price_condition=_range_text(trim_range) if trim_range else "next executable confirmed price",
            catalyst=catalyst,
            expires_at=min(tactical.expiry_date, fundamental.expiry_date),
        )
    return CapitalAction(ticker=symbol, action=action, owned=owned, rationale=rationale)


def _selected_expression(expressions: Mapping[ExpressionKind, ExpressionDecision], action: CapitalActionType) -> ExpressionDecision | None:
    if action is CapitalActionType.AVOID:
        return expressions.get(ExpressionKind.CASH)
    if action is CapitalActionType.WAIT_FOR_PRICE:
        return next(
            (item for item in expressions.values() if item.selected and item.kind is not ExpressionKind.CASH),
            expressions.get(ExpressionKind.CASH),
        )
    if action in {CapitalActionType.HOLD, CapitalActionType.EXIT, CapitalActionType.TRIM}:
        return next((item for item in expressions.values() if item.selected), expressions.get(ExpressionKind.CASH))
    return next((item for item in expressions.values() if item.selected), expressions.get(ExpressionKind.CASH))


def _view_requests(symbol: str, tactical: HorizonDecision, fundamental: HorizonDecision, tables: Mapping[str, list[dict[str, Any]]]) -> list[DataRequest]:
    requests: list[DataRequest] = []
    if tactical.entry_range is None or fundamental.entry_range is None:
        requests.append(_request(
            field="entry_range", ticker=symbol,
            why="An exact entry range is required before a resting or immediate order can be priced.",
            source="confirmed price selector and ticker thesis", max_age="15m", max_age_seconds=900,
            owner="update_market_data", collect_now="update_market_data",
            expected="A price range for both horizon views with available_at <= as_of.",
            impact="The current capital action can use an executable entry condition.",
        ))
    if tactical.target_range is None or fundamental.target_range is None:
        requests.append(_request(
            field="target_range", ticker=symbol,
            why="A target range is required to compare expected return and exit timing.",
            source="point-in-time valuation and ticker thesis", max_age="1d", max_age_seconds=86400,
            owner="update_market_valuations", collect_now="update_market_valuations",
            expected="A target range for each active horizon with its source revision.",
            impact="Expected return and the exit condition become explicit.",
        ))
    if tactical.invalidation is None or fundamental.invalidation is None:
        requests.append(_request(
            field="invalidation", ticker=symbol,
            why="An exact invalidation is required to calculate loss per share and to resolve the call.",
            source="current thesis and technical support", max_age="1d", max_age_seconds=86400,
            owner="update_theses", collect_now="market-refresh-decision-models",
            expected="A price, event, or date invalidation tied to the current decision revision.",
            impact="A numeric stock quantity can be calculated and the call can be invalidated deterministically.",
        ))
    if fundamental.expected_return_range is None:
        requests.append(_request(
            field="valuation", ticker=symbol,
            why="Point-in-time valuation or expected return is required for the fundamental view.",
            source="point-in-time company financials and valuation", max_age="1d", max_age_seconds=86400,
            owner="update_market_valuations", collect_now="update_market_valuations",
            expected="A valuation range or expected return with publication and availability timestamps.",
            impact="The fundamental return range and conviction basis can be quantified.",
        ))
    raw_scenarios = _latest(
        tables,
        "symbol_decision_snapshot", "symbol_decision_snapshots", "decision_queue",
        "opportunities_ranked", "candidates",
    ).get("scenarios")
    if not isinstance(raw_scenarios, Mapping) or not {"bear", "base", "bull"}.issubset(raw_scenarios):
        requests.append(_request(
            field="scenario_probabilities", ticker=symbol,
            why="Scenario probabilities are required for calibrated expected return and lower-confidence expectancy.",
            source="ticker decision scenario model", max_age="1d", max_age_seconds=86400,
            owner="update_decision_models", collect_now="market-refresh-decision-models",
            expected="Bear, base, and bull probabilities and outcome ranges that sum to 100 percent.",
            impact="The recommendation keeps its direction but replaces the uninformative prior with calibrated odds.",
        ))
    if not (tables.get("options_payoff_scenarios") or tables.get("options_ticker_signals")):
        requests.append(_request(
            field="option_quote", ticker=symbol,
            why="Executable option expressions cannot be compared without a current quote package.",
            source="confirmed option quote and spread selector", max_age="15m", max_age_seconds=900,
            owner="update_ibkr_options", collect_now="update_ibkr_options",
            expected="A current bid, ask, expiration, and maximum-loss field for each candidate structure.",
            impact="An option expression can compete with stock without inventing max loss.",
        ))
    else:
        option_rows = tables.get("options_payoff_scenarios") or tables.get("options_ticker_signals") or []
        if not any(_has_executable_option_quote(row) for row in option_rows):
            requests.append(_request(
                field="option_quote", ticker=symbol,
                why="Executable option expressions cannot be compared without a complete current bid, ask, and displayed-size package.",
                source="confirmed option quote and spread selector", max_age="15m", max_age_seconds=900,
                owner="update_ibkr_options", collect_now="update_ibkr_options",
                expected="A current bid, ask, bid size, ask size, expiration, and maximum-loss field for each candidate structure.",
                impact="An option expression can compete with stock and paper fills can use later executable quotes.",
            ))
        if any(
            _number(_pick(row, "max_loss", "one_unit_max_loss", "maximum_loss")) is None
            and _number(_pick(row, "premium_mid", "entry_price", "limit_price")) is None
            for row in option_rows
        ):
            requests.append(_request(
                field="max_option_loss", ticker=symbol,
                why="Maximum contract loss is required before an option quantity can be calculated.",
                source="confirmed option quote and payoff selector", max_age="15m", max_age_seconds=900,
                owner="update_ibkr_options", collect_now="update_ibkr_options",
                expected="A finite maximum loss for every candidate option expression.",
                impact="The option quantity becomes numeric or remains explicitly blocked.",
            ))
    return requests


def _signal_requests(
    symbol: str,
    tables: Mapping[str, list[dict[str, Any]]],
    reference: datetime,
) -> list[DataRequest]:
    """Create runnable requests for absent signal families without hiding direction."""

    requests: list[DataRequest] = []
    sec_financial_rows = [
        row for row in tables.get("fundamentals") or []
        if str(_pick(row, "source", "source_id") or "").strip().lower() == "sec_companyfacts"
        and _fresh_at(row, reference, 86_400)
    ]
    if not sec_financial_rows:
        requests.append(_request(
            field="company_financials", ticker=symbol,
            why="Point-in-time financials are required to test cash flow, margins, return on capital, and valuation assumptions.",
            source="SEC EDGAR accepted filing and company-facts selectors", max_age="1d", max_age_seconds=86400,
            owner="update_company_financials", collect_now="update_company_financials",
            expected="A filing-vintage financial observation with acceptance, publication, and availability timestamps.",
            impact="The fundamental stance, target range, or fact that flips it may change.",
        ))
    if not (tables.get("earnings") or tables.get("analyst_estimates")):
        requests.append(_request(
            field="earnings_revisions", ticker=symbol,
            why="Actuals, guidance, and estimate revisions define the event and expectation-change risk.",
            source="issuer earnings release, SEC filing, and approved estimate vintage", max_age="1d", max_age_seconds=86400,
            owner="update_earnings_and_estimates", collect_now="update_earnings_and_estimates",
            expected="A point-in-time earnings event plus actual, guidance, and estimate-vintage fields.",
            impact="The tactical catalyst and scenario probabilities may change.",
        ))
    if not tables.get("ticker_benchmark_snapshot"):
        requests.append(_request(
            field="market_breadth", ticker=symbol,
            why="A frozen equity denominator is required to distinguish market breadth from the option candidate set.",
            source="frozen point-in-time equity and ETF benchmark membership", max_age="1d", max_age_seconds=86400,
            owner="publish_ticker_benchmark", collect_now="publish_ticker_benchmark",
            expected="Exact benchmark membership, membership hash, price coverage, and availability timestamp.",
            impact="Breadth context may change the tactical stance but cannot change ticker identity or option availability.",
        ))
    if not (tables.get("macro") or tables.get("market_environment_model")):
        requests.append(_request(
            field="macro_regime", ticker=symbol,
            why="Rates, inflation, growth, credit, dollar, and commodity vintages set the discount-rate and risk-appetite context.",
            source="FRED real-time vintage, Treasury, and official release calendar", max_age="1d", max_age_seconds=86400,
            owner="update_macro_series", collect_now="update_macro_series",
            expected="A macro regime row with release vintage, prior vintage, surprise, and availability timestamps.",
            impact="The scenario weights or expression horizon may change.",
        ))
    if not (tables.get("disclosures") or tables.get("ownership_consensus")):
        requests.append(_request(
            field="corporate_actions_and_flows", ticker=symbol,
            why="Buybacks, issuance, insider activity, index changes, and delayed 13F ownership context affect supply and expectations.",
            source="SEC disclosures, issuer reports, index notices, and ETF shares outstanding", max_age="1d", max_age_seconds=86400,
            owner="update_disclosures", collect_now="update_disclosures",
            expected="A dated issuer or ownership event with source, publication, receipt, and revision fields.",
            impact="The fundamental evidence and opportunity-cost comparison may change; 13F remains delayed context.",
        ))
    if not (tables.get("short_interest") or tables.get("borrow")):
        requests.append(_request(
            field="short_interest_and_borrow", ticker=symbol,
            why="Short interest and borrow cost are needed before treating squeeze, financing, or bearish-expression claims as observed.",
            source="official short-interest report and approved licensed borrow source", max_age="2d", max_age_seconds=172800,
            owner="update_short_interest_and_borrow", collect_now="update_short_interest_and_borrow",
            expected="A dated short-interest or borrow observation with coverage and license metadata.",
            impact="The bearish expression choice or risk range may change; daily short volume is not net shorting.",
        ))
    return requests


def _build_manifest(usable: Mapping[str, list[dict[str, Any]]], as_of: datetime, code_version: str, experiment_id: str) -> InputManifest:
    inputs = {
        name: [_manifest_row(name, row) for row in rows]
        for name, rows in sorted(usable.items())
        if rows
    }
    source_versions: dict[str, str] = {}
    for name, rows in usable.items():
        for row in rows:
            source = str(_pick(row, "source", "source_id", "provider") or "").strip()
            version = str(_pick(row, "source_version", "revision", "version") or "").strip()
            if source:
                source_versions[source] = version or source_versions.get(source, "unknown")
    encoded = json.dumps({"as_of": as_of.isoformat(), "inputs": inputs, "source_versions": source_versions, "code_version": code_version, "experiment_id": experiment_id}, sort_keys=True, separators=(",", ":"), default=str)
    return InputManifest(
        as_of=as_of,
        input_hash=hashlib.sha256(encoded.encode()).hexdigest(),
        code_version=code_version,
        experiment_id=experiment_id,
        inputs=inputs,
        source_versions=source_versions,
        signal_declarations=_signal_declarations(usable),
    )


def _build_input_lineage(
    manifest: InputManifest,
    *,
    decision_revision: str,
    policy_version: str,
    cutoff: datetime,
) -> list[InputLineage]:
    lineage: list[InputLineage] = []
    seen_identities: set[tuple[Any, ...]] = set()
    for field, values in manifest.inputs.items():
        rows = values if isinstance(values, list) else [values]
        for value in rows:
            if not isinstance(value, Mapping):
                raise ValueError("ticker decision input lineage row must be an object")
            source_id = str(
                value.get("source_id")
                or value.get("source")
                or value.get("provider")
                or field
            ).strip()
            available_at = _parse_datetime(
                _pick(value, "available_at", "received_at", "publication_published_at")
            )
            if not source_id or available_at is None:
                raise ValueError("ticker decision input lineage is incomplete")
            source_version = str(
                value.get("source_version")
                or value.get("revision")
                or value.get("version")
                or manifest.source_versions.get(source_id)
                or "unknown"
            )
            candidate = InputLineage(
                field=str(field),
                source_id=source_id,
                source_version=source_version,
                event_at=_parse_datetime(value.get("event_at") or value.get("event_time")),
                published_at=_parse_datetime(value.get("published_at") or value.get("publication_time")),
                available_at=available_at,
                received_at=_parse_datetime(value.get("received_at") or value.get("receipt_time")),
                revision=str(value.get("revision") or "") or None,
                decision_revision=decision_revision,
                policy_version=policy_version,
                cutoff=cutoff,
            )
            # Panel joins can repeat one source row. Deduplicate only the
            # canonical identity enforced by OpportunityEpisode; distinct
            # source versions, timestamps, or revisions remain separate.
            identity = _input_lineage_identity(candidate)
            if identity not in seen_identities:
                seen_identities.add(identity)
                lineage.append(candidate)
    if not lineage:
        # A fully empty source set is still a published, deterministic blocked
        # decision.  The composer record makes the missing-input boundary
        # explicit without inventing a provider observation.
        lineage.append(InputLineage(
            field="decision_composer",
            source_id="deterministic-composer",
            source_version=manifest.code_version,
            available_at=cutoff,
            revision=manifest.input_hash,
            decision_revision=decision_revision,
            policy_version=policy_version,
            cutoff=cutoff,
        ))
    return lineage


def _manifest_row(name: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """Keep large frozen membership in its own table, not each decision row."""

    value = dict(row)
    if name == "ticker_benchmark_snapshot" and value.get("exact_membership") is not None:
        value["exact_membership"] = {
            "membership_hash": value.get("membership_hash"),
            "member_count": value.get("member_count"),
        }
    return _jsonable(value)


def _signal_declarations(tables: Mapping[str, list[dict[str, Any]]]) -> list[SignalDeclaration]:
    families = (
        (
            "company_financials",
            "Issuer cash flow, financing, buybacks, issuance, and return on capital shape long-run cash generation.",
            "FUNDAMENTAL",
            SignalEvidenceState.OBSERVED,
            "SEC EDGAR company facts and accepted filings",
            ("fundamentals", "valuations", "disclosures"),
            "1d after publication; filing acceptance timestamp required",
            "Point-in-time period alignment; preserve original and revised values.",
            "Publish the directional view and request the missing financial field; do not invent valuation or size.",
            "Measure incremental value over price, sector, and trend baselines.",
        ),
        (
            "earnings_revisions",
            "Actuals, guidance, surprises, and estimate revisions move active expectations around catalysts.",
            "TACTICAL+FUNDAMENTAL",
            SignalEvidenceState.DERIVED,
            "SEC filings, issuer releases, and licensed consensus only when approved",
            ("earnings", "earnings_setups", "analyst_estimates"),
            "15m for event state; 1d for estimates; vintage required",
            "Normalize actual, estimate, guidance, and revision vintages without overwriting prior values.",
            "Keep the current view and create an earnings or estimate data request.",
            "Test against price, sector, trend, and catalyst-only baselines.",
        ),
        (
            "market_breadth",
            "Leadership, dispersion, correlation, and volatility-control flows change the payoff to beta and trend.",
            "TACTICAL",
            SignalEvidenceState.DERIVED,
            "Frozen point-in-time equity benchmark and confirmed price bars",
            ("ticker_benchmark_snapshot", "technicals", "quotes"),
            "1d; benchmark membership and coverage must be explicit",
            "Aggregate only the frozen denominator; options availability cannot change equity breadth.",
            "Report exact membership and coverage; do not suppress the ticker call.",
            "Compare with equal-weight price, sector, and simple trend baselines.",
        ),
        (
            "macro_regime",
            "Rates, inflation, growth, credit, funding, the dollar, and commodities change discount rates and risk appetite.",
            "TACTICAL+FUNDAMENTAL",
            SignalEvidenceState.OBSERVED,
            "FRED real-time vintages, Treasury, and official release calendars",
            ("macro", "market_environment_model"),
            "Release-vintage dependent; available_at must be <= as_of",
            "Retain vintage values and calculate surprises from the prior available vintage.",
            "Keep direction and request the exact macro series or vintage that matters.",
            "Measure over price, sector, and trend controls by regime slice.",
        ),
        (
            "corporate_actions_and_flows",
            "Buybacks, issuance, insider activity, index changes, and ETF shares alter supply and passive demand.",
            "FUNDAMENTAL",
            SignalEvidenceState.OBSERVED,
            "SEC filings, issuer reports, index notices, and ETF shares outstanding",
            ("disclosures", "ownership_consensus"),
            "Event and publication timestamp required; 13F is delayed context, not current flow",
            "Separate issuer actions, ownership snapshots, and ETF share changes; never infer fund flow from volume.",
            "Request the missing action or ownership vintage while retaining the recommendation.",
            "Compare with sector, market, and ownership-only baselines.",
        ),
        (
            "option_surface",
            "Dealer hedging, skew, term structure, and executable spread quality express convexity and forced-flow risk.",
            "TACTICAL",
            SignalEvidenceState.DERIVED,
            "Confirmed option quotes and OCC open-interest data; licensed flow only if approved",
            ("options_ticker_signals", "options_payoff_scenarios"),
            "15m for executable quotes; open interest is unsigned",
            "Compare net expected value per loss dollar using executable bid/ask and maximum loss.",
            "Block only the option quantity; stock and cash remain competing expressions.",
            "Compare with stock, cash, sector, and simple trend counterfactuals.",
        ),
        (
            "short_interest_and_borrow",
            "Short interest, borrow cost, and utilization affect squeeze risk, financing, and the cost of bearish expression.",
            "TACTICAL+FUNDAMENTAL",
            SignalEvidenceState.ESTIMATED,
            "Official short-interest reports; licensed borrow when approved",
            ("short_interest", "borrow"),
            "Publication schedule dependent; daily borrow freshness when available",
            "Treat daily short volume as unsigned activity and 13F as delayed ownership context.",
            "Keep the ticker direction and request the licensed field only if it can change the expression.",
            "Test after price, sector, and trend controls.",
        ),
        (
            "participant_option_flow",
            "Signed participant flow can reveal hedging or speculation only when the data identifies the participant side.",
            "TACTICAL",
            SignalEvidenceState.HYPOTHESIS,
            "Licensed participant-flow dataset; no automatic purchase",
            ("participant_option_flow",),
            "Provider-specific; source license and side confidence required",
            "Do not infer dealer side from open interest or volume; keep this signal advisory-only.",
            "Leave the field absent and identify the exact dataset, coverage, cost, and expected decision impact.",
            "Admit only after a source utility report shows incremental value.",
        ),
    )
    declarations: list[SignalDeclaration] = []
    for name, mechanism, horizon, state, source, tables_for_family, freshness, transform, missing, value in families:
        loaded = any(tables.get(table) for table in tables_for_family)
        declarations.append(SignalDeclaration(
            name=name,
            economic_mechanism=mechanism,
            applicable_horizon=horizon,
            evidence_state=state,
            source=source,
            coverage="loaded" if loaded else "missing",
            freshness=freshness,
            transformation=transform,
            missing_data_behavior=missing,
            incremental_predictive_value=value,
        ))
    return declarations


def _usable_rows(rows: Iterable[Mapping[str, Any]], symbol: str, as_of: datetime) -> list[dict[str, Any]]:
    output = []
    for raw in rows or []:
        row = dict(raw)
        row_symbol = str(_pick(row, "symbol", "ticker", "underlying") or "").strip().upper()
        if row_symbol and row_symbol != symbol:
            continue
        available = _parse_datetime(_pick(
            row,
            "available_at",
            "received_at",
            "availableAt",
            "publication_published_at",
        ))
        # A historical decision cannot use a row whose information-time is
        # unknown.  Production read models carry available_at from the
        # successful ingestion/publication run; fixtures and adapters must do
        # the same instead of silently treating receipt time as as_of.
        if available is None or available > as_of:
            continue
        if row.get("confirmed") is False or row.get("is_confirmed") is False:
            continue
        if str(row.get("quality_status") or "").lower() in {"invalid", "lookahead_blocked"}:
            continue
        output.append(row)
    return output


def _latest(tables: Mapping[str, list[dict[str, Any]]], *names: str) -> dict[str, Any]:
    rows = [row for name in names for row in tables.get(name, [])]
    if not rows:
        return {}
    return max(rows, key=lambda row: _sort_key(row))


def _sort_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(_pick(row, key) or "") for key in ("available_at", "published_at", "observed_at", "as_of", "date", "event_date", "updated_at", "created_at"))


def _stance(row: Mapping[str, Any], horizon: Horizon, tables: Mapping[str, list[dict[str, Any]]]) -> Stance:
    keys = (f"{horizon.value.lower()}_stance", f"{horizon.value.lower()}_direction", f"{horizon.value.lower()}_bias", "stance", "direction", "bias", "sentiment")
    nested = row.get(horizon.value.lower())
    if isinstance(nested, Mapping):
        value = _pick(nested, "stance", "direction", "bias", "action")
        parsed = _parse_stance(value)
        if parsed:
            return parsed
    for key in keys:
        parsed = _parse_stance(row.get(key))
        if parsed:
            return parsed
    parsed = _parse_stance(row.get("action") or row.get("action_grade") or row.get("decision"))
    if parsed:
        return parsed
    if horizon is Horizon.FUNDAMENTAL:
        valuation = _latest(tables, "valuations", "fundamentals")
        upside = _number(_pick(valuation, "upside_pct", "expected_upside", "upside"))
        if upside is not None:
            if abs(upside) > 1:
                upside /= 100
            if upside >= 0.10:
                return Stance.BULLISH
            if upside <= -0.10:
                return Stance.BEARISH
        thesis = _latest(tables, "theses", "thesis_monitor", "research_packets")
        parsed = _parse_stance(_pick(thesis, "stance", "direction", "bias", "conviction"))
        if parsed:
            return parsed
    technical = _latest(tables, "technicals")
    momentum = _number(_pick(technical, "momentum_20d", "return_20d", "trend_return"))
    if momentum is not None:
        if abs(momentum) > 1:
            momentum /= 100
        if momentum > 0.03:
            return Stance.BULLISH
        if momentum < -0.03:
            return Stance.BEARISH
    return Stance.NEUTRAL


def _parse_stance(value: Any) -> Stance | None:
    normalized = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if normalized in {"BULLISH", "BULL", "LONG", "BUY", "ADD", "ACCUMULATE", "OVERWEIGHT"}:
        return Stance.BULLISH
    if normalized in {"BEARISH", "BEAR", "SHORT", "SELL", "TRIM", "EXIT", "AVOID", "UNDERWEIGHT"}:
        return Stance.BEARISH
    if normalized in {"NEUTRAL", "HOLD", "WAIT", "WAIT_FOR_PRICE", "WATCH", "PASS", "NO_TRADE"}:
        return Stance.NEUTRAL
    return None


def _view_action(stance: Stance) -> CapitalActionType:
    return {
        Stance.BULLISH: CapitalActionType.BUY,
        Stance.BEARISH: CapitalActionType.AVOID,
        Stance.NEUTRAL: CapitalActionType.WAIT_FOR_PRICE,
    }[stance]


def _expression_preference(horizon: Horizon, tables: Mapping[str, list[dict[str, Any]]], stance: Stance) -> ExpressionKind:
    if stance is Stance.NEUTRAL:
        return ExpressionKind.CASH
    option_rows = tables.get("options_payoff_scenarios", []) or tables.get("options_ticker_signals", [])
    event = _latest(tables, "earnings", "earnings_setups", "catalysts")
    if horizon is Horizon.TACTICAL and option_rows and event:
        return _expression_kind(option_rows[0]) or ExpressionKind.STOCK
    return ExpressionKind.STOCK


def _expression_horizon(tactical: HorizonDecision, fundamental: HorizonDecision) -> Horizon:
    return fundamental.horizon if fundamental.stance is not Stance.NEUTRAL else tactical.horizon


def _expression_stance(tactical: HorizonDecision, fundamental: HorizonDecision) -> Stance:
    return fundamental.stance if fundamental.stance is not Stance.NEUTRAL else tactical.stance


def _expression_kind(row: Mapping[str, Any]) -> ExpressionKind | None:
    structure = str(_pick(row, "structure", "expression", "kind", "option_type") or "").lower().replace("-", "_").replace(" ", "_")
    if structure in {"crypto_spot", "spot", "crypto", "crypto_asset"}:
        return ExpressionKind.CRYPTO_SPOT
    if structure in {"crypto_perpetual", "perpetual", "perp", "crypto_perp"}:
        return ExpressionKind.CRYPTO_PERPETUAL
    if structure in {"call", "long_call", "call_option"}:
        return ExpressionKind.CALL
    if structure in {"put", "long_put", "put_option"}:
        return ExpressionKind.PUT
    if "spread" in structure:
        return ExpressionKind.DEBIT_SPREAD
    if structure in {"cash_secured_put", "csp"}:
        return ExpressionKind.CASH_SECURED_PUT
    return None


def _legs(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = row.get("legs") or row.get("synthetic_legs")
    if isinstance(value, list):
        legs: list[dict[str, Any]] = []
        for value_leg in value:
            if not isinstance(value_leg, Mapping):
                continue
            leg = dict(value_leg)
            if leg.get("expiration") is None and row.get("expiration") is not None:
                leg["expiration"] = row.get("expiration")
            if leg.get("quote_time") is None and row.get("quote_observed_at") is not None:
                leg["quote_time"] = row.get("quote_observed_at")
            legs.append(leg)
        return legs
    if row.get("contract_id") and row.get("bid") is not None and row.get("ask") is not None:
        return [{
            "contract_id": row.get("contract_id"),
            "option_type": row.get("option_type"),
            "side": "long",
            "strike": row.get("strike"),
            "bid": row.get("bid"),
            "ask": row.get("ask"),
            "bid_size": row.get("bid_size"),
            "ask_size": row.get("ask_size"),
            "quote_time": row.get("quote_observed_at"),
            "expiration": row.get("expiration"),
        }]
    return []


def _has_executable_option_quote(row: Mapping[str, Any]) -> bool:
    legs = _legs(row)
    return bool(legs) and all(
        _number(_pick(leg, "bid")) is not None
        and _number(_pick(leg, "ask")) is not None
        and _number(_pick(leg, "bid_size")) is not None
        and _number(_pick(leg, "ask_size")) is not None
        and _pick(leg, "quote_time", "observed_at") is not None
        for leg in legs
    )


def _invalidation(row: Mapping[str, Any], horizon: Horizon) -> Invalidation | None:
    nested = row.get(horizon.value.lower())
    source = nested if isinstance(nested, Mapping) else row
    price = _number(_pick(source, "invalidation_price", "invalid_price", "stop_price"))
    if price is not None:
        # The invalidation belongs to the ticker thesis, not to the horizon
        # label. Both views must be able to point at the same invalidation fact.
        return Invalidation(kind="price", value=price, statement=f"Invalidate the ticker call below {price:g}.")
    event = _pick(source, "invalidation_event", "invalidation")
    if isinstance(event, Mapping):
        if _number(event.get("price")) is not None:
            return Invalidation(kind="price", value=_number(event.get("price")), statement=str(event.get("statement") or event.get("reason") or "Price invalidation"))
        if event.get("date"):
            return Invalidation(kind="date", value=str(event["date"]), statement=str(event.get("statement") or "Date invalidation"))
        if event.get("event"):
            return Invalidation(kind="event", value=str(event["event"]), statement=str(event.get("statement") or event["event"]))
    if isinstance(event, (str, date)) and str(event).strip():
        return Invalidation(kind="event", value=str(event), statement=str(event))
    return None


def _price_range(row: Mapping[str, Any], prefix: str, fallback: float | None = None) -> PriceRange | None:
    value = row.get(f"{prefix}_range")
    if isinstance(value, Mapping):
        low = _number(_pick(value, "low", "min"))
        high = _number(_pick(value, "high", "max"))
        if low is not None and high is not None:
            return PriceRange(low=min(low, high), high=max(low, high))
    low = _number(_pick(row, f"{prefix}_low", f"{prefix}_min"))
    high = _number(_pick(row, f"{prefix}_high", f"{prefix}_max"))
    exact = _number(
        _pick(row, f"{prefix}_price", f"{prefix}")
        if prefix != "entry"
        else _pick(row, "entry_price", "limit_price", "premium_mid", "conservative_entry")
    )
    if low is None and high is None and exact is not None:
        low = high = exact
    if low is None and high is None and fallback is not None:
        low = high = fallback
    if low is None or high is None:
        return None
    return PriceRange(low=min(low, high), high=max(low, high))


def _numeric_range(row: Mapping[str, Any], prefix: str) -> NumericRange | None:
    if not row:
        return None
    value = row.get(f"{prefix}_range")
    if isinstance(value, Mapping):
        low = _number(_pick(value, "low", "min"))
        high = _number(_pick(value, "high", "max"))
    else:
        low = _number(_pick(row, f"{prefix}_low", f"{prefix}_min"))
        high = _number(_pick(row, f"{prefix}_high", f"{prefix}_max"))
    exact = _number(_pick(row, f"{prefix}", f"{prefix}_pct"))
    if low is None and high is None and exact is not None:
        low = high = exact
    if low is None or high is None:
        return None
    return NumericRange(low=min(low, high), high=max(low, high))


def _valuation_return(tables: Mapping[str, list[dict[str, Any]]]) -> NumericRange | None:
    row = _latest(tables, "valuations")
    upside = _number(_pick(row, "upside_pct", "expected_upside", "upside"))
    if upside is None:
        return None
    if abs(upside) > 1:
        upside /= 100
    return NumericRange(low=upside, high=upside)


def _scenarios(row: Mapping[str, Any], stance: Stance, current_price: float | None, target: PriceRange | None) -> list[ScenarioOutcome]:
    raw = row.get("scenarios")
    values: dict[str, Any] = raw if isinstance(raw, Mapping) else {}
    probabilities: dict[str, float | None] = {}
    result: list[ScenarioOutcome] = []
    for name in ("bear", "base", "bull"):
        item = values.get(name) if isinstance(values.get(name), Mapping) else {}
        probability = _number(_pick(item, "probability", "prob", "weight"))
        if probability is not None and probability > 1:
            probability /= 100
        probabilities[name] = probability if probability is not None and 0 <= probability <= 1 else None
        result.append(ScenarioOutcome(
            name=name,
            probability=probabilities[name],
            description=str(_pick(item, "description", "outcome") or f"{name.title()} case for a {stance.value.lower()} ticker view; scenario range not loaded."),
            price_range=_price_range(item, "price") if item else target if name == "base" and target else None,
            return_range=_numeric_range(item, "return"),
        ))
    if any(value is None for value in probabilities.values()) or not math.isclose(
        sum(value or 0.0 for value in probabilities.values()), 1.0, abs_tol=1e-6
    ):
        for item in result:
            item.probability = None
    return result


def _evidence(row: Mapping[str, Any], polarity: EvidencePolarity, tables: Mapping[str, list[dict[str, Any]]], horizon: Horizon) -> list[EvidenceItem]:
    key = "evidence_for" if polarity is EvidencePolarity.FOR else "evidence_against"
    raw = row.get(key) or row.get("bull_case" if polarity is EvidencePolarity.FOR else "bear_case")
    values = raw if isinstance(raw, list) else [raw] if raw else []
    output = [_evidence_item(item, polarity) for item in values]
    if output:
        return output
    source = _latest(tables, "fundamentals", "valuations" if polarity is EvidencePolarity.FOR else "technicals")
    statement = str(_pick(source, "summary", "reason", "signal") or "")
    return [EvidenceItem(statement=statement, polarity=polarity)] if statement else []


def _evidence_item(value: Any, polarity: EvidencePolarity) -> EvidenceItem:
    if isinstance(value, Mapping):
        return EvidenceItem(
            statement=str(_pick(value, "statement", "text", "reason") or "Evidence row"),
            polarity=polarity,
            source=str(_pick(value, "source", "source_id", "provider") or "") or None,
            reference=str(_pick(value, "reference", "url", "reference_url") or "") or None,
            event_at=_parse_datetime(_pick(value, "event_at", "event_time")) or _parse_date(_pick(value, "event_date")),
            published_at=_parse_datetime(_pick(value, "published_at", "publication_time")),
            available_at=_parse_datetime(_pick(value, "available_at", "receipt_time")),
            revision=str(_pick(value, "revision", "version") or "") or None,
            license=str(value.get("license") or "") or None,
        )
    return EvidenceItem(statement=str(value), polarity=polarity)


def _flip_fact(row: Mapping[str, Any], symbol: str, horizon: Horizon, as_of: datetime) -> EvidenceItem:
    value = row.get("fact_that_would_flip") or row.get("flip_fact")
    if isinstance(value, Mapping):
        return _evidence_item(value, EvidencePolarity.FLIP)
    statement = str(value or f"A confirmed {horizon.value.lower()} invalidation for {symbol} or a material revision to its core assumptions.")
    return EvidenceItem(statement=statement, polarity=EvidencePolarity.FLIP, available_at=as_of)


def _confidence(row: Mapping[str, Any], horizon: Horizon, tables: Mapping[str, list[dict[str, Any]]]) -> float | None:
    nested = row.get(horizon.value.lower())
    source = nested if isinstance(nested, Mapping) else row
    value = _number(_pick(source, "confidence", "probability_confidence", "confidence_score"))
    if value is None:
        value = _number(_pick(_latest(tables, "conviction_calibration"), "confidence", "calibration"))
    if value is None:
        return None
    if value > 1:
        value /= 100
    return max(0, min(1, value))


def _conviction(row: Mapping[str, Any], confidence: float | None, fallback: str) -> str:
    value = str(row.get("conviction_tier") or row.get("conviction") or "").upper().replace(" ", "_")
    if value in {"EXPLORATORY", "STANDARD", "HIGH"}:
        return value
    if confidence is not None and confidence >= 0.80:
        return "HIGH"
    if confidence is not None and confidence >= 0.60:
        return "STANDARD"
    return fallback


def _confidence_basis(row: Mapping[str, Any], tables: Mapping[str, list[dict[str, Any]]], horizon: Horizon, invalidation: Invalidation | None, expected: NumericRange | None) -> list[str]:
    basis = []
    if row:
        basis.append("ticker decision row")
    if _latest(tables, "valuations", "fundamentals"):
        basis.append("company or valuation evidence loaded")
    if _latest(tables, "technicals"):
        basis.append("market behavior evidence loaded")
    if expected is not None:
        basis.append("expected-return range loaded")
    if invalidation is not None:
        basis.append(f"{invalidation.kind} invalidation loaded")
    return basis or ["uninformative prior; no source-backed confidence row loaded"]


def _expiry_date(row: Mapping[str, Any], horizon: Horizon, as_of: datetime) -> date:
    value = _pick(row, "expiry_date", "horizon_date", "review_date", "valid_until")
    parsed = _parse_date(value)
    return parsed or _add_business_days(as_of.date(), 20 if horizon is Horizon.TACTICAL else 252)


def _add_business_days(start: date, count: int) -> date:
    current = start
    remaining = count
    while remaining:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def _risk_policy(snapshot: RiskPolicySnapshot) -> RiskPolicy:
    pct = snapshot.ticker_loss_budget_pct
    conviction_tier = {
        0.005: "EXPLORATORY",
        0.01: "STANDARD",
        0.02: "HIGH",
    }.get(pct, "EXPLORATORY")
    return RiskPolicy(
        conviction_tier=conviction_tier,
        loss_budget_pct=pct,
        loss_budget=snapshot.sleeve_capital * pct if snapshot.sleeve_capital is not None and pct is not None else None,
        max_ticker_loss_pct=snapshot.ticker_max_loss_pct,
        max_total_open_planned_loss_pct=snapshot.ticker_total_open_loss_pct,
        position_limit_pct=snapshot.ticker_position_limit_pct,
        policy_version=snapshot.policy_version,
    )


def _portfolio_nav(row: Mapping[str, Any], as_of: datetime) -> tuple[float | None, float | None]:
    value = _number(_pick(row, "net_liquidation", "nav", "portfolio_nav", "account_nav"))
    if value is None or value <= 0:
        return None, None
    age = _age_seconds(row, as_of)
    if age is not None and age > 1800:
        return None, age
    return value, age


def _age_seconds(row: Mapping[str, Any], as_of: datetime) -> float | None:
    timestamp = _parse_datetime(_pick(row, "available_at", "observed_at", "updated_at", "as_of", "received_at"))
    if timestamp is None:
        return None
    return max(0.0, (as_of - timestamp).total_seconds())


def _fresh_at(row: Mapping[str, Any], as_of: datetime, max_age_seconds: int) -> bool:
    timestamp = _parse_datetime(_pick(row, "available_at", "published_at", "received_at"))
    if timestamp is None or timestamp > as_of:
        return False
    return (as_of - timestamp).total_seconds() <= max_age_seconds


def _catalyst(row: Mapping[str, Any], tables: Mapping[str, list[dict[str, Any]]]) -> str | None:
    value = _pick(row, "catalyst", "catalyst_name", "event")
    if value:
        return str(value)
    event = _latest(tables, "earnings", "earnings_setups", "catalysts")
    return str(_pick(event, "title", "name", "event", "event_type") or "") or None


def _request(*, field: str, ticker: str, why: str, source: str, max_age: str, max_age_seconds: int, owner: str, collect_now: str, expected: str, impact: str) -> DataRequest:
    return DataRequest(field=field, ticker=ticker, why_it_matters=why, required_source=source, max_age=max_age, max_age_seconds=max_age_seconds, owner=owner, collect_now=collect_now, expected_completion=expected, decision_impact=impact)


def _dedupe_requests(requests: Iterable[DataRequest]) -> list[DataRequest]:
    output: dict[str, DataRequest] = {}
    for request in requests:
        output.setdefault(request.field, request)
    return list(output.values())


def _bounded_quantity(raw: float, nav: float | None, entry: float | None, position_limit_pct: float) -> int:
    quantity = max(0, math.floor(raw))
    if nav is not None and entry is not None and entry > 0:
        quantity = min(quantity, max(0, math.floor(nav * position_limit_pct / entry)))
    return quantity


def _range_text(value: PriceRange | None) -> str | None:
    if value is None:
        return None
    if value.low == value.high:
        return f"price {value.low:g}"
    return f"price {value.low:g}-{value.high:g}"


def _midpoint(value: PriceRange | None) -> float | None:
    return (value.low + value.high) / 2 if value else None


def _number(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    try:
        result = float(str(value).replace(",", "").replace("$", "").replace("%", ""))
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _bounded(value: float | None, low: float, high: float) -> float | None:
    return None if value is None else max(low, min(high, value))


def _pick(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return _utc(value).date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


__all__ = [
    "CONTRACT_VERSION", "EXPERIMENT_ID", "OPPORTUNITY_EPISODE_CONTRACT_VERSION",
    "TRADE_EXPRESSION_CONTRACT_VERSION", "TRADE_PLAN_CONTRACT_VERSION",
    "OUTCOME_ATTRIBUTION_CONTRACT_VERSION", "OUTCOME_ATTRIBUTION_EVALUATION_VERSION",
    "CapitalAction", "CapitalActionType",
    "capital_action_from_resolution", "DataRequest", "EvidenceItem", "EvidencePolarity",
    "ExpressionDecision", "ExpressionKind", "OpportunityEpisodeStatus", "TradeExpression", "trade_expression_from_legacy",
    "trade_expression_from_expression_decision", "expression_decision_from_trade_expression",
    "expression_decision_to_trade_expression", "Horizon", "HorizonDecision", "InputManifest",
    "InputLineage", "Invalidation", "NumericRange", "OpportunityEpisode",
    "opportunity_episode_id", "opportunity_episode_from_legacy", "build_opportunity_episode",
    "PriceRange", "RiskPolicy", "ScenarioOutcome", "Stance", "TickerDecision",
    "OutcomeAttributionState", "OutcomeEvidenceState", "OutcomeEvidence",
    "PaperExecutionOutcome", "OutcomeAttribution", "outcome_attribution_id",
    "outcome_attribution_stable_key",
    "SignalDeclaration", "SignalEvidenceState", "build_ticker_decision",
    "portfolio_impact_from_persisted", "portfolio_impacts_from_persisted",
    "trade_plan_from_persisted",
]
