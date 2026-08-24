"""Canonical calculated action resolution shared by ticker and option paper paths."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


RESOLUTION_CONTRACT_VERSION = "decision-resolution.v2"


class ResolutionLifecycle(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"


class ResolutionEligibility(StrEnum):
    BLOCKED = "BLOCKED"
    PENDING = "PENDING"
    ELIGIBLE = "ELIGIBLE"
    ACTIONABLE = "ACTIONABLE"


class AuthorizationMode(StrEnum):
    NONE = "NONE"
    ADVISORY = "ADVISORY"
    PAPER = "PAPER"
    LIVE = "LIVE"


class DataQuality(StrEnum):
    UNKNOWN = "UNKNOWN"
    COMPLETE = "COMPLETE"
    FRESH = "FRESH"
    INCOMPLETE = "INCOMPLETE"
    STALE = "STALE"
    CONFLICTED = "CONFLICTED"


class ResolutionAction(StrEnum):
    BUY = "BUY"
    ADD = "ADD"
    HOLD = "HOLD"
    TRIM = "TRIM"
    EXIT = "EXIT"
    HEDGE = "HEDGE"
    AVOID = "AVOID"
    WAIT_FOR_PRICE = "WAIT_FOR_PRICE"
    NO_TRADE = "NO_TRADE"


class ResolutionProvenance(BaseModel):
    model_config = ConfigDict(extra="allow")

    as_of: datetime | None = None
    available_at: datetime | None = None
    input_hash: str | None = None
    source_versions: dict[str, str] = Field(default_factory=dict)
    revisions: dict[str, Any] = Field(default_factory=dict)


class DecisionResolutionV2(BaseModel):
    """One calculated resolution; compatibility views must derive from it."""

    model_config = ConfigDict(extra="allow")

    contract_version: str = RESOLUTION_CONTRACT_VERSION
    lifecycle: ResolutionLifecycle = ResolutionLifecycle.PUBLISHED
    eligibility: ResolutionEligibility = ResolutionEligibility.PENDING
    authorization_mode: AuthorizationMode = AuthorizationMode.ADVISORY
    data_quality: DataQuality = DataQuality.UNKNOWN
    action: ResolutionAction
    primary_blocker: str | None = None
    blockers: list[str] = Field(default_factory=list)
    next_action: str = "Refresh and recalculate the decision."
    entry: Any = None
    size: Any = None
    invalidation: Any = None
    exit: Any = None
    ttl: Any = None
    portfolio_context: Any = None
    policy_version: str = "risk-policy.v2:missing"
    decision_revision: str
    provenance: ResolutionProvenance | dict[str, Any] = Field(default_factory=ResolutionProvenance)
    ticker: str | None = None
    rationale: str = ""
    owned: bool = False
    price_condition: str | None = None
    catalyst: str | None = None
    expires_at: date | datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_contract(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        aliases = {
            "status": "eligibility",
            "authorization": "authorization_mode",
            "authorization_state": "authorization_mode",
            "data_quality_status": "data_quality",
            "next_required_action": "next_action",
            "policy_revision": "policy_version",
            "revision": "decision_revision",
        }
        for old, new in aliases.items():
            if new not in result and old in result:
                result[new] = result[old]
        nested = result.get("trade_plan")
        if isinstance(nested, Mapping):
            for name in ("entry", "size", "invalidation", "exit", "ttl", "portfolio_context"):
                if result.get(name) is None and name in nested:
                    result[name] = nested[name]
        blockers = result.get("blockers")
        if isinstance(blockers, str):
            blockers = [blockers]
        elif blockers is None:
            blockers = []
        else:
            blockers = [str(item) for item in blockers if str(item).strip()]
        primary = str(result.get("primary_blocker") or "").strip() or None
        if primary is None and blockers:
            primary = _choose_blocker(blockers)
        if primary and primary not in blockers:
            blockers.append(primary)
        if blockers:
            primary = _choose_blocker(blockers)
        result["primary_blocker"] = primary
        result["blockers"] = blockers
        for key in ("lifecycle", "eligibility", "authorization_mode", "data_quality"):
            item = result.get(key)
            if isinstance(item, str):
                result[key] = item.upper().replace("-", "_")
        action = result.get("action")
        if isinstance(action, str):
            result["action"] = action.upper().replace("-", "_")
        return result

    @model_validator(mode="after")
    def enforce_invariants(self) -> "DecisionResolutionV2":
        if self.authorization_mode is AuthorizationMode.LIVE:
            raise ValueError("live authorization is not supported by DecisionResolutionV2")
        if self.eligibility is ResolutionEligibility.BLOCKED:
            if not self.primary_blocker:
                raise ValueError("blocked resolution requires exactly one primary blocker")
            if len(self.blockers) != 1 or self.blockers[0] != self.primary_blocker:
                raise ValueError("blocked resolution must expose exactly one primary blocker")
            if _is_order_action(self.action):
                raise ValueError("blocked resolution cannot contain an order action")
            if self.authorization_mode in {AuthorizationMode.PAPER, AuthorizationMode.LIVE}:
                raise ValueError("blocked resolution cannot be paper or live authorized")
        if self.eligibility is ResolutionEligibility.ACTIONABLE:
            missing = [
                name for name, item in (
                    ("entry", self.entry),
                    ("size", self.size),
                    ("invalidation", self.invalidation),
                    ("exit", self.exit),
                    ("ttl", self.ttl),
                    ("portfolio_context", self.portfolio_context),
                ) if _missing(item)
            ]
            if missing:
                raise ValueError(f"actionable resolution requires trade-plan fields: {', '.join(missing)}")
            if self.primary_blocker:
                raise ValueError("actionable resolution cannot have a primary blocker")
            if self.action in {ResolutionAction.NO_TRADE, ResolutionAction.AVOID}:
                raise ValueError("actionable resolution cannot be a no-trade action")
            if self.authorization_mode is AuthorizationMode.NONE:
                raise ValueError("actionable resolution requires an authorization mode")
            if self.data_quality not in {DataQuality.COMPLETE, DataQuality.FRESH}:
                raise ValueError("actionable resolution requires complete fresh data")
            if self.lifecycle in {
                ResolutionLifecycle.EXPIRED,
                ResolutionLifecycle.SUPERSEDED,
                ResolutionLifecycle.INVALIDATED,
            }:
                raise ValueError("terminal lifecycle cannot be actionable")
        if self.eligibility is ResolutionEligibility.ELIGIBLE and self.primary_blocker:
            raise ValueError("eligible resolution cannot have a primary blocker")
        if self.authorization_mode is AuthorizationMode.PAPER and self.eligibility is not ResolutionEligibility.ACTIONABLE:
            raise ValueError("paper authorization requires an actionable resolution")
        if self.primary_blocker and not self.blockers:
            raise ValueError("primary blocker must be represented by the blocker projection")
        if self.action == "WATCH":
            raise ValueError("WATCH is not a resolution action")
        return self

    @property
    def trade_plan(self) -> dict[str, Any]:
        return {
            "entry": self.entry,
            "size": self.size,
            "invalidation": self.invalidation,
            "exit": self.exit,
            "ttl": self.ttl,
            "portfolio_context": self.portfolio_context,
        }

    @property
    def is_blocked(self) -> bool:
        return self.eligibility is ResolutionEligibility.BLOCKED

    @property
    def is_actionable(self) -> bool:
        return self.eligibility is ResolutionEligibility.ACTIONABLE


def build_decision_resolution(
    *,
    action: str,
    decision_revision: str,
    policy_version: str,
    provenance: Mapping[str, Any] | ResolutionProvenance,
    ticker: str | None = None,
    blockers: list[str] | tuple[str, ...] = (),
    entry: Any = None,
    size: Any = None,
    invalidation: Any = None,
    exit: Any = None,
    ttl: Any = None,
    portfolio_context: Any = None,
    lifecycle: str = "PUBLISHED",
    data_quality: str = "UNKNOWN",
    authorization_mode: str = "ADVISORY",
    rationale: str = "",
    owned: bool = False,
    price_condition: str | None = None,
    catalyst: str | None = None,
    expires_at: date | datetime | None = None,
    blocked: bool = False,
) -> DecisionResolutionV2:
    clean_blockers = [str(item) for item in blockers if str(item).strip()]
    primary = _choose_blocker(clean_blockers) if clean_blockers else None
    complete_plan = all(not _missing(item) for item in (entry, size, invalidation, exit, ttl, portfolio_context))
    actionable_action = str(action).upper() not in {ResolutionAction.NO_TRADE.value, ResolutionAction.AVOID.value}
    actionable_quality = str(data_quality).upper() in {DataQuality.COMPLETE.value, DataQuality.FRESH.value}
    actionable_authorization = str(authorization_mode).upper() in {
        AuthorizationMode.ADVISORY.value,
        AuthorizationMode.PAPER.value,
    }
    eligibility = (
        ResolutionEligibility.BLOCKED
        if blocked
        else ResolutionEligibility.ACTIONABLE
        if not clean_blockers and complete_plan and actionable_action and actionable_quality and actionable_authorization
        else ResolutionEligibility.PENDING
    )
    if eligibility is ResolutionEligibility.BLOCKED:
        action = ResolutionAction.NO_TRADE.value
        clean_blockers = [primary] if primary else []
        authorization_mode = AuthorizationMode.NONE.value
    return DecisionResolutionV2(
        lifecycle=lifecycle,
        eligibility=eligibility,
        authorization_mode=authorization_mode,
        data_quality=data_quality,
        action=action,
        primary_blocker=primary,
        blockers=clean_blockers,
        next_action=next_action_for(primary),
        entry=entry,
        size=size,
        invalidation=invalidation,
        exit=exit,
        ttl=ttl,
        portfolio_context=portfolio_context,
        policy_version=policy_version,
        decision_revision=decision_revision,
        provenance=provenance,
        ticker=ticker,
        rationale=rationale,
        owned=owned,
        price_condition=price_condition,
        catalyst=catalyst,
        expires_at=expires_at,
    )


def resolution_from_legacy(payload: Mapping[str, Any]) -> DecisionResolutionV2:
    """Create a deterministic resolution for old immutable rows."""

    existing = payload.get("resolution")
    if isinstance(existing, Mapping) and existing.get("decision_revision"):
        return DecisionResolutionV2.model_validate(existing)
    capital = dict(payload.get("capital_action") or {})
    selected = dict(payload.get("selected_expression") or {})
    tactical = dict(payload.get("tactical") or {})
    fundamental = dict(payload.get("fundamental") or {})
    risk = dict(payload.get("risk_policy") or {})
    requests = [str(item.get("field")) for item in payload.get("data_requests") or [] if item.get("field")]
    invalidation = selected.get("invalidation") or tactical.get("invalidation") or fundamental.get("invalidation")
    entry = selected.get("entry_range") or tactical.get("entry_range") or fundamental.get("entry_range")
    exit_plan = selected.get("target_range") or tactical.get("target_range") or fundamental.get("target_range")
    ttl = capital.get("expires_at") or fundamental.get("expiry_date") or tactical.get("expiry_date")
    nav = None
    pct = risk.get("loss_budget_pct")
    if risk.get("loss_budget") is not None and pct:
        try:
            nav = float(risk["loss_budget"]) / float(pct)
        except (TypeError, ValueError, ZeroDivisionError):
            nav = None
    revision = str(payload.get("decision_revision") or "legacy")
    return build_decision_resolution(
        action=str(capital.get("action") or "AVOID"),
        decision_revision=revision,
        policy_version=str(payload.get("policy_version") or risk.get("policy_version") or "risk-policy.v2:legacy"),
        provenance={
            "as_of": payload.get("as_of"),
            "input_hash": (payload.get("input_manifest") or {}).get("input_hash"),
            "source_versions": (payload.get("input_manifest") or {}).get("source_versions") or {},
        },
        ticker=str(payload.get("ticker") or capital.get("ticker") or ""),
        blockers=requests,
        entry=entry,
        size=selected.get("quantity"),
        invalidation=invalidation,
        exit=exit_plan,
        ttl=ttl,
        portfolio_context={"status": "complete" if nav is not None else "missing", "nav": nav},
        data_quality="COMPLETE" if not requests else "INCOMPLETE",
        rationale=str(capital.get("rationale") or ""),
        owned=bool(capital.get("owned")),
        price_condition=capital.get("price_condition"),
        catalyst=capital.get("catalyst"),
        expires_at=_date_or_datetime(ttl),
    )


def next_action_for(blocker: str | None) -> str:
    actions = {
        "current_price": "Refresh the confirmed current price.",
        "portfolio_nav": "Refresh PostgreSQL account facts before sizing.",
        "invalidation": "Add a concrete thesis invalidation.",
        "entry_range": "Refresh the point-in-time entry range.",
        "target_range": "Refresh the point-in-time exit target.",
        "paper_assignment_permission_required": "Keep CSP assignment disabled until paper permission is explicit.",
        "fresh_postgres_account_facts_required": "Refresh PostgreSQL cash and buying-power facts.",
    }
    return actions.get(str(blocker or ""), "Refresh the required fact and recalculate the resolution.")


def _choose_blocker(blockers: list[str]) -> str | None:
    priority = {
        "paper_assignment_permission_required": 5,
        "current_price": 10,
        "portfolio_nav": 20,
        "fresh_postgres_account_facts_required": 25,
        "invalidation": 30,
        "entry_range": 40,
        "target_range": 50,
    }
    return min(blockers, key=lambda item: (priority.get(item, 100), item)) if blockers else None


def _is_order_action(action: str) -> bool:
    return str(action).upper() in {"BUY", "ADD", "TRIM", "EXIT", "HEDGE", "SELL", "SELL_TO_OPEN", "BUY_TO_OPEN"}


def _missing(value: Any) -> bool:
    return value is None or value == "" or value == {} or value == [] or value == 0


def _date_or_datetime(value: Any) -> date | datetime | None:
    if isinstance(value, (date, datetime)):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


__all__ = [
    "AuthorizationMode",
    "DataQuality",
    "DecisionResolutionV2",
    "RESOLUTION_CONTRACT_VERSION",
    "ResolutionAction",
    "ResolutionEligibility",
    "ResolutionLifecycle",
    "ResolutionProvenance",
    "build_decision_resolution",
    "next_action_for",
    "resolution_from_legacy",
]
