"""One immutable risk-policy snapshot shared by paper decision lanes."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from math import isfinite
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


RISK_POLICY_VERSION = "risk-policy.v2"
ASSIGNMENT_POLICY_VERSION = "portfolio-assignment-policy.v1"


class RiskPolicySnapshot(BaseModel):
    """Point-in-time policy facts used for sizing and paper authorization."""

    model_config = ConfigDict(extra="allow", frozen=True)

    policy_version: str
    policy_kind: str = "shared"
    sleeve_capital: float | None = None
    max_risk_per_trade_pct: float | None = None
    max_open_risk_pct: float | None = None
    max_symbol_risk_pct: float | None = None
    daily_loss_halt_pct: float | None = None
    max_open_positions: int = 0
    defined_trade_fraction: float = Field(default=0.0025, ge=0, le=1)
    defined_symbol_fraction: float = Field(default=0.005, ge=0, le=1)
    defined_total_fraction: float = Field(default=0.01, ge=0, le=1)
    csp_symbol_fraction: float = Field(default=0.05, ge=0, le=1)
    csp_total_fraction: float = Field(default=0.15, ge=0, le=1)
    ticker_loss_budget_pct: float | None = None
    ticker_max_loss_pct: float = Field(default=0.04, ge=0, le=1)
    ticker_total_open_loss_pct: float = Field(default=0.10, ge=0, le=1)
    ticker_position_limit_pct: float = Field(default=0.10, gt=0, le=1)
    broker_net_liquidation: float | None = None
    broker_available_capital: float | None = None
    cash_balance: float | None = None
    buying_power: float | None = None
    account_observed_at: datetime | None = None
    available_at: datetime | None = None
    blockers: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.blockers

    @property
    def per_trade_limit(self) -> float:
        return _dollars(
            self.sleeve_capital,
            self.max_risk_per_trade_pct if self.policy_kind == "recovery" else self.defined_trade_fraction,
        )

    @property
    def aggregate_open_risk_limit(self) -> float:
        return _dollars(
            self.sleeve_capital,
            self.max_open_risk_pct if self.policy_kind == "recovery" else self.defined_total_fraction,
        )

    @property
    def per_symbol_open_risk_limit(self) -> float:
        return _dollars(
            self.sleeve_capital,
            self.max_symbol_risk_pct if self.policy_kind == "recovery" else self.defined_symbol_fraction,
        )

    @property
    def daily_loss_halt(self) -> float:
        return _dollars(self.sleeve_capital, self.daily_loss_halt_pct)

    @property
    def csp_symbol_limit(self) -> float:
        return _dollars(self.sleeve_capital, self.csp_symbol_fraction)

    @property
    def csp_total_limit(self) -> float:
        return _dollars(self.sleeve_capital, self.csp_total_fraction)

    def snapshot(self) -> dict[str, Any]:
        """Return the JSON document stored with an immutable paper order."""

        result = self.model_dump(mode="json")
        result.update({
            "valid": self.valid,
            "per_trade_limit": _round(self.per_trade_limit),
            "aggregate_open_risk_limit": _round(self.aggregate_open_risk_limit),
            "per_symbol_open_risk_limit": _round(self.per_symbol_open_risk_limit),
            "daily_loss_halt": _round(self.daily_loss_halt),
            "csp_symbol_limit": _round(self.csp_symbol_limit),
            "csp_total_limit": _round(self.csp_total_limit),
        })
        return result

    def model_dump_snapshot(self) -> dict[str, Any]:
        """Compatibility name for callers that treated policies as snapshots."""

        return self.snapshot()


class PortfolioAssignmentPolicy(BaseModel):
    """Explicit, paper-only consent and account facts for CSP assignment."""

    model_config = ConfigDict(extra="allow", frozen=True)

    assignment_policy_version: str = ASSIGNMENT_POLICY_VERSION
    risk_policy_version: str | None = None
    risk_policy_blockers: tuple[str, ...] = ()
    paper_assignment_allowed: bool = False
    thesis_direction: str | None = None
    thesis_as_of: datetime | None = None
    thesis_preferred_structures: tuple[str, ...] = ()
    account_as_of: datetime | None = None
    account_source: str | None = None
    cash_balance: float | None = None
    buying_power: float | None = None
    required_cash: float | None = None
    open_symbol_collateral: float = 0.0
    open_total_collateral: float = 0.0
    symbol_limit: float | None = None
    aggregate_limit: float | None = None
    evaluated_at: datetime | None = None
    max_account_age_seconds: int = 300

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        aliases = {
            "paper_assignment_permission": "paper_assignment_allowed",
            "explicit_paper_permission": "paper_assignment_allowed",
            "assignment_permission": "paper_assignment_allowed",
            "thesis_available_at": "thesis_as_of",
            "account_observed_at": "account_as_of",
            "account_facts_source": "account_source",
            "cash": "cash_balance",
            "available_cash": "cash_balance",
            "buying_power_available": "buying_power",
            "required_collateral": "required_cash",
            "symbol_concentration_limit": "symbol_limit",
            "aggregate_concentration_limit": "aggregate_limit",
            "preferred_structures": "thesis_preferred_structures",
            "thesis_structures": "thesis_preferred_structures",
        }
        for old, new in aliases.items():
            if new not in result and old in result:
                result[new] = result[old]
        return result

    def blockers(self, *, as_of: datetime | None = None, required_cash: float | None = None,
                 thesis_direction: str | None = None) -> tuple[str, ...]:
        """Return deterministic blockers; no blocker means assignment is permitted in paper."""

        blockers: list[str] = []
        blockers.extend(str(item) for item in self.risk_policy_blockers if str(item).strip())
        if not self.paper_assignment_allowed:
            blockers.append("paper_assignment_permission_required")
        direction = str(thesis_direction or self.thesis_direction or "").strip().lower()
        if direction not in {"bullish", "neutral_bullish", "long", "up"}:
            blockers.append("assignment_thesis_direction_must_be_bullish")
        preferred = {str(item).strip() for item in self.thesis_preferred_structures if str(item).strip()}
        if not preferred:
            blockers.append("assignment_thesis_preferred_structures_required")
        elif "cash_secured_put" not in preferred:
            blockers.append("assignment_thesis_does_not_permit_cash_secured_put")
        reference = _utc(as_of or self.evaluated_at)
        if reference is None:
            blockers.append("assignment_evaluated_at_required")
        if self.thesis_as_of is None:
            blockers.append("point_in_time_thesis_required")
        elif reference is not None and _utc(self.thesis_as_of) > reference:
            blockers.append("future_thesis_revision_not_allowed")
        if self.account_as_of is None:
            blockers.append("fresh_postgres_account_facts_required")
        elif reference is not None:
            account_as_of = _utc(self.account_as_of)
            if account_as_of > reference:
                blockers.append("future_account_revision_not_allowed")
            elif (reference - account_as_of).total_seconds() > self.max_account_age_seconds:
                blockers.append("fresh_postgres_account_facts_required")
        if str(self.account_source or "").lower() not in {"postgresql", "postgres", "raw.broker_account_snapshot"}:
            blockers.append("postgresql_account_facts_required")
        cash = _finite_nonnegative(self.cash_balance)
        buying_power = _finite_nonnegative(self.buying_power)
        if cash is None or buying_power is None:
            blockers.append("fresh_postgres_account_facts_required")
        required = _positive(required_cash if required_cash is not None else self.required_cash)
        if required is None:
            blockers.append("assignment_collateral_required")
        elif cash is not None and buying_power is not None and min(cash, buying_power) < required:
            blockers.append("insufficient_cash_or_buying_power_for_assignment")
        symbol_limit = _finite_nonnegative(self.symbol_limit)
        aggregate_limit = _finite_nonnegative(self.aggregate_limit)
        if required is not None and symbol_limit is None:
            blockers.append("assignment_symbol_concentration_limit_required")
        elif required is not None and self.open_symbol_collateral + required > symbol_limit:
            blockers.append("assignment_symbol_concentration_limit_exceeded")
        if required is not None and aggregate_limit is None:
            blockers.append("assignment_aggregate_concentration_limit_required")
        elif required is not None and self.open_total_collateral + required > aggregate_limit:
            blockers.append("assignment_aggregate_concentration_limit_exceeded")
        return tuple(dict.fromkeys(blockers))

    @property
    def eligible(self) -> bool:
        return not self.blockers()

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.model_dump(mode="json"),
            "eligible": self.eligible,
            "blockers": list(self.blockers()),
        }


def compile_portfolio_assignment_policy(
    config: object | None = None,
    *,
    run_cutoff: datetime,
    thesis: Mapping[str, Any] | None,
    required_cash: float | None,
    account_facts: Mapping[str, Any] | None,
    sleeve_capital: float | None = None,
    open_symbol_collateral: float = 0.0,
    open_total_collateral: float = 0.0,
    risk_policy_snapshot: RiskPolicySnapshot | None = None,
    paper_assignment_allowed: bool | None = None,
) -> PortfolioAssignmentPolicy:
    """Compile the point-in-time CSP assignment policy from PostgreSQL inputs."""

    account = dict(account_facts or {})
    thesis_values = dict(thesis or {})
    risk_policy = risk_policy_snapshot or compile_risk_policy_snapshot(
        config,
        account,
        sleeve_capital=(
            sleeve_capital
            if sleeve_capital is not None
            else _number(_value(_settings(config), "options_risk_sleeve_capital"))
        ),
        policy_kind="standard",
    )
    permission = paper_assignment_allowed
    if permission is None:
        permission = _value(_settings(config), "csp_paper_assignment_allowed")
    preferred = thesis_values.get("thesis_preferred_structures")
    if preferred is None:
        preferred = thesis_values.get("preferred_structures")
    if isinstance(preferred, str):
        preferred = [preferred]
    return PortfolioAssignmentPolicy(
        assignment_policy_version=ASSIGNMENT_POLICY_VERSION,
        risk_policy_version=risk_policy.policy_version,
        risk_policy_blockers=risk_policy.blockers,
        paper_assignment_allowed=bool(permission) if permission is not None else False,
        thesis_direction=(
            thesis_values.get("direction")
            or thesis_values.get("stance")
            or thesis_values.get("thesis_direction")
        ),
        thesis_as_of=_timestamp(
            thesis_values.get("as_of")
            or thesis_values.get("updated_at")
            or thesis_values.get("created_at")
            or thesis_values.get("thesis_as_of")
        ),
        thesis_preferred_structures=tuple(str(item) for item in preferred or [] if str(item).strip()),
        account_as_of=_timestamp(
            account.get("account_as_of")
            or account.get("account_observed_at")
            or account.get("observed_at")
        ),
        account_source=str(
            account.get("account_source")
            or account.get("account_facts_source")
            or "postgresql"
        ),
        cash_balance=_number(account.get("cash_balance")),
        buying_power=_number(account.get("buying_power")),
        required_cash=required_cash,
        open_symbol_collateral=max(_number(open_symbol_collateral) or 0.0, 0.0),
        open_total_collateral=max(_number(open_total_collateral) or 0.0, 0.0),
        symbol_limit=risk_policy.csp_symbol_limit,
        aggregate_limit=risk_policy.csp_total_limit,
        evaluated_at=run_cutoff,
    )


def compile_risk_policy_snapshot(
    config: object | None = None,
    account_facts: Mapping[str, Any] | None = None,
    *,
    sleeve_capital: float | None = None,
    conviction_tier: str | None = None,
    policy_kind: str = "standard",
    additional_blockers: Iterable[str] = (),
) -> RiskPolicySnapshot:
    """Compile existing settings and account facts into one immutable document."""

    settings = _settings(config)
    sleeve = _number(
        sleeve_capital if sleeve_capital is not None else _value(settings, "options_risk_sleeve_capital")
    )
    trade_pct = _number(_value(settings, "max_risk_per_trade_pct"))
    open_pct = _number(_value(settings, "max_open_risk_pct"))
    symbol_pct = _number(_value(settings, "max_symbol_risk_pct"))
    halt_pct = _number(_value(settings, "daily_loss_halt_pct"))
    try:
        max_positions = int(_value(settings, "max_recovery_open_positions") or 0)
    except (TypeError, ValueError):
        max_positions = 0
    tier = str(conviction_tier or "").upper()
    ticker_pct = {"EXPLORATORY": 0.005, "STANDARD": 0.01, "HIGH": 0.02}.get(tier)
    account = dict(account_facts or {})
    blockers: list[str] = [str(item) for item in additional_blockers if str(item).strip()]
    if policy_kind == "recovery":
        if sleeve is None or sleeve <= 0:
            blockers.append("positive_recovery_sleeve_capital_required")
        for name, value in (
            ("max_risk_per_trade_pct", trade_pct),
            ("max_open_risk_pct", open_pct),
            ("max_symbol_risk_pct", symbol_pct),
            ("daily_loss_halt_pct", halt_pct),
        ):
            if value is None or not 0 <= value <= 1:
                blockers.append(f"{name}_must_be_between_zero_and_one")
        if trade_pct is not None and open_pct is not None and trade_pct > open_pct:
            blockers.append("per_trade_risk_cannot_exceed_aggregate_open_risk")
        if symbol_pct is not None and open_pct is not None and symbol_pct > open_pct:
            blockers.append("per_symbol_risk_cannot_exceed_aggregate_open_risk")
        if trade_pct is not None and symbol_pct is not None and trade_pct > symbol_pct:
            blockers.append("per_trade_risk_cannot_exceed_per_symbol_risk")
        if trade_pct is not None and open_pct is not None and max_positions > 0 and trade_pct * max_positions < open_pct:
            blockers.append("open_position_limit_cannot_cover_aggregate_open_risk")
        if max_positions <= 0:
            blockers.append("positive_recovery_open_position_limit_required")
    observed_at = account.get("account_observed_at")
    if observed_at is None:
        observed_at = account.get("observed_at")
    if observed_at is None:
        observed_at = account.get("available_at")
    available_at = account.get("available_at")
    if available_at is None:
        available_at = observed_at
    values: dict[str, Any] = {
        "policy_kind": policy_kind,
        "sleeve_capital": sleeve,
        "max_risk_per_trade_pct": trade_pct,
        "max_open_risk_pct": open_pct,
        "max_symbol_risk_pct": symbol_pct,
        "daily_loss_halt_pct": halt_pct,
        "max_open_positions": max_positions,
        "ticker_loss_budget_pct": ticker_pct,
        "broker_net_liquidation": _number(account.get("broker_net_liquidation", account.get("net_liquidation"))),
        "broker_available_capital": _number(account.get("broker_available_capital")),
        "cash_balance": _number(account.get("cash_balance")),
        "buying_power": _number(account.get("buying_power")),
        "account_observed_at": _timestamp(observed_at),
        "available_at": _timestamp(available_at),
        "blockers": tuple(dict.fromkeys(blockers)),
    }
    account_source = account.get("account_source") or account.get("account_facts_source")
    if account_source is not None:
        values["account_source"] = str(account_source)
    # Hash the normalized model so every carried material fact, including
    # broker observations and allowed source identity, participates once.
    snapshot = RiskPolicySnapshot(policy_version="pending", **values)
    canonical = snapshot.model_dump(mode="json")
    canonical.pop("policy_version", None)
    digest = sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    return snapshot.model_copy(update={"policy_version": f"{RISK_POLICY_VERSION}:{digest}"})


def coerce_portfolio_assignment_policy(value: Any = None, **defaults: Any) -> PortfolioAssignmentPolicy:
    """Coerce route/ticket input without ever defaulting assignment permission on."""

    if isinstance(value, PortfolioAssignmentPolicy):
        return value
    data = dict(defaults)
    if isinstance(value, Mapping):
        data.update(value)
    elif isinstance(value, bool):
        data["paper_assignment_allowed"] = value
    return PortfolioAssignmentPolicy.model_validate(data)


coerce_assignment_policy = coerce_portfolio_assignment_policy
AssignmentPolicy = PortfolioAssignmentPolicy


def missing_risk_policy_snapshot(*, policy_kind: str = "shared") -> RiskPolicySnapshot:
    return RiskPolicySnapshot(
        policy_version=f"{RISK_POLICY_VERSION}:missing",
        policy_kind=policy_kind,
        blockers=("risk_policy_required",),
    )


def _settings(config: object | None) -> Any:
    if isinstance(config, Mapping):
        settings = config.get("analysis", config)
        return settings.get("options_decision_system", settings) if isinstance(settings, Mapping) else settings
    settings = getattr(config, "analysis", config)
    return getattr(settings, "options_decision_system", settings)


def _value(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _number(value: Any) -> float | None:
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and isfinite(number) else None


def _finite_nonnegative(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _positive(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _dollars(capital: float | None, fraction: float | None) -> float:
    if capital is None or fraction is None or capital <= 0 or fraction < 0:
        return 0.0
    return capital * fraction


def _round(value: float) -> float:
    return round(value + 1e-9, 2)


PolicySnapshot = RiskPolicySnapshot
compile_risk_policy = compile_risk_policy_snapshot


__all__ = [
    "ASSIGNMENT_POLICY_VERSION",
    "AssignmentPolicy",
    "PortfolioAssignmentPolicy",
    "PolicySnapshot",
    "RISK_POLICY_VERSION",
    "RiskPolicySnapshot",
    "coerce_portfolio_assignment_policy",
    "coerce_assignment_policy",
    "compile_portfolio_assignment_policy",
    "compile_risk_policy",
    "compile_risk_policy_snapshot",
    "missing_risk_policy_snapshot",
]
