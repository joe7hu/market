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
from investment_panel.core.decision.resolution import (
    DecisionResolutionV2,
    build_decision_resolution,
    resolution_from_legacy,
)


CONTRACT_VERSION = "ticker-decision.v1"
EXPERIMENT_ID = "ticker-first-v1"


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
    probability: float = Field(ge=0, le=1)
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
        if not math.isclose(sum(s.probability for s in self.scenarios), 1.0, abs_tol=1e-6):
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
    data_requests: list[DataRequest] = Field(default_factory=list)
    learning_history: list[dict[str, Any]] = Field(default_factory=list)
    input_manifest: InputManifest

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


def build_ticker_decision(
    ticker: str,
    tables: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    as_of: datetime | None = None,
    code_version: str = CONTRACT_VERSION,
    experiment_id: str = EXPERIMENT_ID,
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
                "data_requests": persisted.get("data_requests") or [],
                "learning_history": persisted.get("learning_history") or [],
                "input_manifest": persisted.get("input_manifest") or {},
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
    decision_revision = f"{CONTRACT_VERSION}:{manifest.input_hash[:16]}"
    selected_entry = selected.entry_range if selected is not None else None
    selected_invalidation = selected.invalidation if selected is not None else None
    selected_exit = selected.target_range if selected is not None else None
    resolution_blockers = [request.field for request in requests]
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
        blockers=resolution_blockers,
        entry=selected_entry or tactical.entry_range or fundamental.entry_range,
        size=selected.quantity if selected is not None else None,
        invalidation=selected_invalidation or tactical.invalidation or fundamental.invalidation,
        exit=selected_exit or tactical.target_range or fundamental.target_range,
        ttl=capital.expires_at or min(tactical.expiry_date, fundamental.expiry_date),
        portfolio_context={"status": "complete" if nav is not None else "missing", "nav": nav, "owned": owned},
        data_quality="COMPLETE" if not resolution_blockers else "INCOMPLETE",
        authorization_mode="ADVISORY",
        rationale=capital.rationale,
        owned=capital.owned,
        price_condition=capital.price_condition,
        catalyst=capital.catalyst,
        expires_at=capital.expires_at,
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
        data_requests=requests,
        learning_history=learning,
        input_manifest=manifest,
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
    result: list[ScenarioOutcome] = []
    for name in ("bear", "base", "bull"):
        item = values.get(name) if isinstance(values.get(name), Mapping) else {}
        probability = _number(_pick(item, "probability", "prob", "weight"))
        if probability is None:
            probability = 1 / 3
        elif probability > 1:
            probability /= 100
        result.append(ScenarioOutcome(
            name=name,
            probability=max(0, probability),
            description=str(_pick(item, "description", "outcome") or f"{name.title()} case for a {stance.value.lower()} ticker view; scenario range not loaded."),
            price_range=_price_range(item, "price") if item else target if name == "base" and target else None,
            return_range=_numeric_range(item, "return"),
        ))
    total = sum(item.probability for item in result)
    if total <= 0:
        total = 1
    for item in result:
        item.probability = item.probability / total
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
    "CONTRACT_VERSION", "CapitalAction", "CapitalActionType", "capital_action_from_resolution", "DataRequest",
    "EvidenceItem", "EvidencePolarity", "ExpressionDecision", "ExpressionKind",
    "Horizon", "HorizonDecision", "InputManifest", "Invalidation", "NumericRange",
    "PriceRange", "RiskPolicy", "ScenarioOutcome", "Stance", "TickerDecision",
    "SignalDeclaration", "SignalEvidenceState", "build_ticker_decision",
]
