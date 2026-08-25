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

from investment_panel.core.risk_policy import compile_risk_policy_snapshot
from investment_panel.core.risk_policy import RiskPolicySnapshot
from investment_panel.core.decision.resolution import (
    DecisionResolutionV2,
    build_decision_resolution,
    resolution_from_legacy,
)


CONTRACT_VERSION = "ticker-decision.v1"
EXPERIMENT_ID = "ticker-first-v1"
OPPORTUNITY_EPISODE_CONTRACT_VERSION = "opportunity-episode.v1"
TRADE_EXPRESSION_CONTRACT_VERSION = "trade-expression.v1"
_CONTEXT_UNSET = object()


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


class ExpressionKind(StrEnum):
    STOCK = "STOCK"
    CALL = "CALL"
    PUT = "PUT"
    DEBIT_SPREAD = "DEBIT_SPREAD"
    CASH_SECURED_PUT = "CASH_SECURED_PUT"
    CASH = "CASH"


class EvidencePolarity(StrEnum):
    FOR = "FOR"
    AGAINST = "AGAINST"
    FLIP = "FLIP"


class SignalEvidenceState(StrEnum):
    OBSERVED = "OBSERVED"
    DERIVED = "DERIVED"
    ESTIMATED = "ESTIMATED"
    HYPOTHESIS = "HYPOTHESIS"


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


class MarketDimensionState(BaseModel):
    """One typed market dimension at one point-in-time horizon."""

    model_config = ConfigDict(extra="allow", frozen=True)

    dimension: str = Field(min_length=1)
    horizon: str = Field(min_length=1)
    state: str | None = None
    change_drivers: tuple[str, ...] = ()
    evidence_status: str = "unavailable"
    uncertainty: str | None = None
    quality: str | None = None
    blockers: tuple[str, ...] = ()
    lineage: tuple[InputLineage, ...] = ()
    probability: float | None = Field(default=None, ge=0, le=1)
    probability_method: str | None = None
    probability_model_version: str | None = None

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
    blockers: tuple[str, ...] = ()

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
        if self.availability == "available":
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


class PortfolioImpact(BaseModel):
    """Frozen before/after portfolio impact for one exact expression."""

    model_config = ConfigDict(extra="allow", frozen=True)

    contract_version: str = "portfolio-impact.v1"
    impact_id: str = Field(min_length=1)
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
    marginal_risk: float | None = None
    diversification_benefit: float | None = None
    risk_budget_consumed: float | None = None
    positions_most_correlated: tuple[str, ...] = ()
    position_to_trim_or_replace: str | None = None
    scenario_pnl: dict[str, Any] | None = None
    factor_exposure: dict[str, Any] | None = None
    greeks: dict[str, Any] | None = None
    liquidity: dict[str, Any] | None = None
    availability: str = "unavailable"
    blockers: tuple[str, ...] = ()

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
        return result

    @model_validator(mode="after")
    def enforce_cutoff(self) -> "PortfolioImpact":
        if self.cutoff.tzinfo is None:
            raise ValueError("portfolio impact cutoff must be timezone-aware")
        cutoff = _utc(self.cutoff)
        for lineage in self.input_lineage:
            if _utc(lineage.available_at) > cutoff:
                raise ValueError("portfolio impact lineage cannot be newer than its cutoff")
        return self


def trade_expression_identity(value: Any) -> str:
    """Return the stable identity used to bind an impact to one expression."""

    expression = trade_expression_from_legacy(value)
    encoded = json.dumps(
        expression.model_dump(mode="json"),
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
    net_expected_value_per_loss_dollar: float | None = None
    lower_confidence_expectancy: float | None = None
    liquidity_score: float | None = Field(default=None, ge=0, le=1)
    spread_pct: float | None = Field(default=None, ge=0)
    fill_probability: float | None = Field(default=None, ge=0, le=1)
    horizon_fit: float | None = Field(default=None, ge=0, le=1)
    status: str = Field(pattern="^(eligible|blocked|unavailable|not_selected)$")
    selected: bool = False
    rationale: str
    data_requests: list[DataRequest] = Field(default_factory=list)


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


def opportunity_episode_id(ticker: str, decision_revision: str) -> str:
    encoded = json.dumps(
        {"ticker": ticker.strip().upper(), "decision_revision": decision_revision},
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
    canonical_episode_id = episode_id or opportunity_episode_id(ticker, decision_revision)
    canonical_lineage = []
    for item in input_lineage:
        lineage = item if isinstance(item, InputLineage) else InputLineage.model_validate(item)
        canonical_lineage.append(lineage.model_copy(update={
            "opportunity_episode_id": lineage.opportunity_episode_id or canonical_episode_id,
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
    portfolio_impacts: dict[ExpressionKind, PortfolioImpact] = Field(default_factory=dict)
    instrument_state_snapshot: dict[str, Any] | None = None
    alpha_signals: list[dict[str, Any]] = Field(default_factory=list)
    opportunity_rank: dict[str, Any] | None = None

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
        if _utc(snapshot.input_cutoff) != _utc(self.cutoff):
            raise ValueError("market snapshot cutoff must match the ticker decision")
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
            if impact.expression_identity != _expression_identity_for(expression, kind, self.ticker, self.decision_revision):
                raise ValueError("portfolio impact expression identity must match the expression")
            normalized[kind] = impact
        self.portfolio_impacts = normalized
        context_blockers = _context_blockers_for(
            snapshot=snapshot,
            policy=policy,
            impacts=normalized,
            expressions=expected,
        )
        if self.resolution is not None and self.resolution.is_actionable and context_blockers:
            self.resolution = build_decision_resolution(
                action="NO_TRADE",
                decision_revision=self.decision_revision,
                policy_version=self.policy_version,
                provenance=self.resolution.provenance,
                ticker=self.ticker,
                blockers=[context_blockers[0]],
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
        blockers: list[str] = []
        snapshot = self.market_state_snapshot
        if snapshot is None:
            blockers.append("market_state_missing")
        else:
            if snapshot.availability != "available":
                blockers.append("market_state_unavailable")
            blockers.extend(snapshot.blockers)
        if not self.market_state_publication_id:
            blockers.append("market_state_publication_missing")
        policy = self.risk_policy_snapshot
        if policy is None:
            blockers.append("risk_policy_snapshot_missing")
        else:
            blockers.extend(policy.blockers)
        expected = set(self.expressions) | {ExpressionKind.CASH}
        for kind in expected:
            impact = self.portfolio_impacts.get(kind)
            if impact is None:
                blockers.append(f"portfolio_impact_missing:{kind.value}")
                continue
            if impact.availability != "available":
                blockers.append(f"portfolio_impact_unavailable:{kind.value}")
            blockers.extend(impact.blockers)
        return tuple(dict.fromkeys(str(item) for item in blockers if str(item).strip()))


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
        )
        for horizon in MARKET_HORIZONS
        for dimension in MARKET_DIMENSIONS
    )
    return MarketStateSnapshot(
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
    before = dict(portfolio_replay or {"nav": nav, "owned": owned})
    result: dict[ExpressionKind, PortfolioImpact] = {}
    for kind, expression in expressions.items():
        planned_loss = _number(expression.planned_loss)
        identity = _expression_identity_for(expression, kind, episode.ticker, episode.decision_revision)
        impact = PortfolioImpact(
            impact_id=f"local-impact:{episode.episode_id}:{kind.value}",
            opportunity_episode_id=episode.episode_id,
            expression_kind=kind,
            expression_identity=identity,
            decision_revision=episode.decision_revision,
            risk_policy_version=policy.policy_version,
            market_snapshot_id=snapshot.snapshot_id,
            market_state_publication_id=snapshot.publication_id,
            cutoff=episode.cutoff,
            input_lineage=tuple(episode.input_lineage),
            portfolio_before=before,
            portfolio_after={**before, "expression_kind": kind.value},
            marginal_risk=planned_loss,
            risk_budget_consumed=planned_loss,
            scenario_pnl=None,
            factor_exposure=None,
            greeks=None,
            liquidity={"status": "unavailable"},
            availability="available" if kind is ExpressionKind.CASH or nav is not None else "unavailable",
            blockers=() if kind is ExpressionKind.CASH or nav is not None else ("portfolio_replay_required",),
        )
        result[kind] = impact
    return result


def _context_blockers_for(
    *,
    snapshot: MarketStateSnapshot | None,
    policy: RiskPolicySnapshot | None,
    impacts: Mapping[ExpressionKind, PortfolioImpact],
    expressions: Mapping[ExpressionKind, ExpressionDecision],
) -> list[str]:
    blockers: list[str] = []
    if snapshot is None:
        blockers.append("market_state_missing")
    else:
        if snapshot.availability != "available":
            blockers.append("market_state_unavailable")
        blockers.extend(snapshot.blockers)
        if not snapshot.publication_id:
            blockers.append("market_state_publication_missing")
    if policy is None:
        blockers.append("risk_policy_snapshot_missing")
    else:
        blockers.extend(policy.blockers)
    expected = set(expressions) | {ExpressionKind.CASH}
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
    persisted = _latest(usable, "ticker_decisions")
    if persisted:
        try:
            # Published revisions are immutable decision truth. The composed
            # fallback below is only for symbols that have not yet been
            # materialized by the ticker decision job.
            persisted_resolution = resolution_from_legacy({
                **persisted,
                "ticker": symbol,
                "resolution": persisted.get("resolution"),
            })
            return TickerDecision.model_validate({
                "decision_contract_version": persisted.get("contract_version") or CONTRACT_VERSION,
                "ticker": symbol,
                "as_of": persisted.get("as_of") or reference,
                "decision_revision": persisted.get("decision_revision"),
                "tactical": persisted.get("tactical"),
                "fundamental": persisted.get("fundamental"),
                "capital_action": capital_action_from_resolution(persisted_resolution),
                "resolution": persisted_resolution,
                "policy_version": persisted.get("policy_version") or (persisted.get("risk_policy") or {}).get("policy_version") or "risk-policy.v2:legacy",
                "risk_policy": persisted.get("risk_policy"),
                "expressions": persisted.get("expressions") or {},
                "selected_expression": persisted.get("selected_expression"),
                "opportunity_episode": persisted.get("opportunity_episode") or None,
                "data_requests": persisted.get("data_requests") or [],
                "learning_history": persisted.get("learning_history") or [],
                "input_manifest": persisted.get("input_manifest") or {},
                "risk_policy_snapshot": persisted.get("risk_policy_snapshot") or None,
                "market_state_publication_id": persisted.get("market_state_publication_id") or None,
                "market_state_snapshot": persisted.get("market_state_snapshot") or None,
                "portfolio_impacts": persisted.get("portfolio_impacts") or {},
                "instrument_state_snapshot": persisted.get("instrument_state_snapshot") or None,
                "alpha_signals": persisted.get("alpha_signals") or [],
                "opportunity_rank": persisted.get("opportunity_rank") or None,
            })
        except Exception:
            # A malformed persisted row is visible to source-health/learning
            # diagnostics, but it must not make the ticker route disappear.
            pass
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
    risk_policy = _risk_policy(decision_row, usable, nav)

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
    if risk_policy_snapshot is _CONTEXT_UNSET:
        policy_snapshot = compile_risk_policy_snapshot(
            sleeve_capital=nav,
            conviction_tier=risk_policy.conviction_tier,
            policy_kind="ticker",
        )
    elif risk_policy_snapshot is None:
        policy_snapshot = None
    else:
        policy_snapshot = (
            risk_policy_snapshot
            if isinstance(risk_policy_snapshot, RiskPolicySnapshot)
            else RiskPolicySnapshot.model_validate(risk_policy_snapshot)
        )
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
        else []
    )
    selected_entry = selected.entry_range if selected is not None else None
    selected_invalidation = selected.invalidation if selected is not None else None
    selected_exit = selected.target_range if selected is not None else None
    resolution_blockers = [request.field for request in requests]
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


def _risk_policy(row: Mapping[str, Any], tables: Mapping[str, list[dict[str, Any]]], nav: float | None) -> RiskPolicy:
    confidence = _number(_pick(row, "confidence", "confidence_score"))
    if confidence is not None and confidence > 1:
        confidence /= 100
    tier = _conviction(row, confidence, "EXPLORATORY")
    pct = {"EXPLORATORY": 0.005, "STANDARD": 0.01, "HIGH": 0.02}[tier]
    snapshot = compile_risk_policy_snapshot(
        sleeve_capital=nav,
        conviction_tier=tier,
        policy_kind="ticker",
    )
    return RiskPolicy(
        conviction_tier=tier,
        loss_budget_pct=pct,
        loss_budget=nav * pct if nav is not None else None,
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
    "TRADE_EXPRESSION_CONTRACT_VERSION", "CapitalAction", "CapitalActionType",
    "capital_action_from_resolution", "DataRequest", "EvidenceItem", "EvidencePolarity",
    "ExpressionDecision", "ExpressionKind", "TradeExpression", "trade_expression_from_legacy",
    "trade_expression_from_expression_decision", "expression_decision_from_trade_expression",
    "expression_decision_to_trade_expression", "Horizon", "HorizonDecision", "InputManifest",
    "InputLineage", "Invalidation", "NumericRange", "OpportunityEpisode",
    "opportunity_episode_id", "opportunity_episode_from_legacy", "build_opportunity_episode",
    "PriceRange", "RiskPolicy", "ScenarioOutcome", "Stance", "TickerDecision",
    "SignalDeclaration", "SignalEvidenceState", "build_ticker_decision",
]
