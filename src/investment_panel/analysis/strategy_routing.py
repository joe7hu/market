"""Deterministic, advisory-only strategy routing for the daily trend challenger."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from investment_panel.core.risk_policy import (
    PortfolioAssignmentPolicy,
    coerce_portfolio_assignment_policy,
)


ROUTE_VERSION = "daily-strategy-route-v1"
DEFINED_RISK_STRUCTURES = (
    "long_call",
    "call_debit_spread",
    "long_put",
    "put_debit_spread",
    "cash_secured_put",
)
PROHIBITED_STRUCTURES = (
    "short_strangle",
    "ratio_spread",
    "naked_call",
    "naked_put",
)
ROUTE_PROMOTION_MIN_OUTCOMES = 30
ROUTE_PROMOTION_MAX_BRIER = 0.25


def route_promotion_gate(evidence: dict[str, Any]) -> dict[str, Any]:
    """Require exact route cohorts, a forward session, and human approval."""
    blockers: list[str] = []
    if int(evidence.get("mature_exact_structure_regime_outcomes") or 0) < ROUTE_PROMOTION_MIN_OUTCOMES:
        blockers.append("route_exact_cohort_sample_below_30")
    lower_bound = _number(evidence.get("cost_adjusted_expectancy_lower_bound"))
    if lower_bound is None or lower_bound <= 0:
        blockers.append("route_cost_adjusted_expectancy_lower_bound_not_positive")
    brier = _number(evidence.get("brier_score"))
    if brier is None or brier > ROUTE_PROMOTION_MAX_BRIER:
        blockers.append("route_brier_above_0_25_or_unavailable")
    if evidence.get("forward_session_passed") is not True:
        blockers.append("route_forward_session_required")
    if evidence.get("human_approved") is not True:
        blockers.append("route_human_approval_required")
    return {
        "eligible": not blockers,
        "blockers": blockers,
        "automatic_promotion": False,
        "route_version": ROUTE_VERSION,
    }


def route_strategy(
    symbol_feature: dict[str, Any],
    market_regime: dict[str, Any],
    *,
    option_iv: float | None,
    realized_vol: float | None,
    iv_percentile: float | None,
    event_summary: dict[str, Any] | None = None,
    candidate_structure: str | None = None,
    thesis_direction: str | None = None,
    assignment_policy: PortfolioAssignmentPolicy | dict[str, Any] | None = None,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    """Select a shadow structure without changing ticket or paper state."""

    trend = str(symbol_feature.get("trend_state") or "unavailable")
    market_trend = str(market_regime.get("trend_state") or market_regime.get("state") or "unavailable")
    volatility = str(symbol_feature.get("volatility_state") or "unstable")
    event = dict(event_summary or {})
    event_state = str(event.get("evidence_state") or "insufficient_event_evidence")
    assignment = coerce_portfolio_assignment_policy(
        assignment_policy,
        paper_assignment_allowed=False,
        thesis_direction=thesis_direction,
        evaluated_at=_as_datetime(as_of),
    )
    reference = _as_datetime(as_of) or assignment.evaluated_at
    assignment_blockers = list(assignment.blockers(
        as_of=reference,
        thesis_direction=thesis_direction,
    ))
    blockers: list[str] = []
    reasons: list[str] = []
    alternatives: list[str] = []
    selected = "NO_TRADE"
    ratio = option_iv / realized_vol if _positive(option_iv) and _positive(realized_vol) else None
    cheap_vol = (ratio is not None and ratio <= 1.10) or (
        iv_percentile is not None and iv_percentile <= 0.40
    )
    rich_vol = (ratio is not None and ratio >= 1.25) or (
        iv_percentile is not None and iv_percentile >= 0.60
    )

    if symbol_feature.get("data_quality_status") != "complete" or trend == "unavailable":
        blockers.extend(symbol_feature.get("reason_codes") or ["symbol_feature_unavailable"])
    if market_regime.get("quality_status") != "complete" or market_trend == "unavailable":
        blockers.append("market_state_unavailable")
    if market_trend in {"range", "transition"}:
        blockers.append("market_transition")
    if (trend == "trend_up" and market_trend == "trend_down") or (
        trend == "trend_down" and market_trend == "trend_up"
    ):
        blockers.append("market_trend_conflict")

    directional_blocked = bool(blockers)
    if not directional_blocked and trend == "trend_up":
        strong = _number(symbol_feature.get("kaufman_er_20d"), 0.0) >= 0.35 and _number(
            symbol_feature.get("relative_strength_20d"), 0.0
        ) > 0
        assignment_direction = str(assignment.thesis_direction or thesis_direction or "").strip().lower()
        if rich_vol and assignment_direction in {"bullish", "long", "up"}:
            selected = "cash_secured_put"
            alternatives = ["call_debit_spread"]
            reasons.extend(["bullish_thesis", "option_volatility_rich", "portfolio_accepts_assignment"])
            blockers.extend(assignment_blockers)
        elif cheap_vol and strong:
            selected = "long_call"
            alternatives = ["call_debit_spread"]
            reasons.extend(["confirmed_uptrend", "strong_relative_strength", "option_volatility_not_rich"])
        else:
            selected = "call_debit_spread"
            alternatives = ["long_call"]
            reasons.extend(["confirmed_uptrend", "defined_risk_caps_premium_or_target_cost"])
    elif not directional_blocked and trend == "trend_down":
        strong = _number(symbol_feature.get("kaufman_er_20d"), 0.0) >= 0.35 and _number(
            symbol_feature.get("relative_strength_20d"), 0.0
        ) < 0
        if cheap_vol and strong:
            selected = "long_put"
            alternatives = ["put_debit_spread"]
            reasons.extend(["confirmed_downtrend", "weak_relative_strength", "option_volatility_not_rich"])
        else:
            selected = "put_debit_spread"
            alternatives = ["long_put"]
            reasons.extend(["confirmed_downtrend", "defined_risk_caps_premium_or_target_cost"])
    elif not blockers and trend in {"range", "transition"}:
        blockers.append("trend_not_directional")

    if (
        selected == "NO_TRADE"
        and candidate_structure == "cash_secured_put"
        and assignment.eligible
        and (trend == "unavailable" or market_trend == "unavailable")
    ):
        selected = "cash_secured_put"
        alternatives = ["call_debit_spread"]
        reasons.extend(["policy_authorized_csp_candidate", "portfolio_accepts_assignment"])

    if selected == "NO_TRADE" and _event_vol_research_ready(event):
        selected = "EVENT_VOL_RESEARCH"
        blockers = [blocker for blocker in blockers if blocker not in {"trend_not_directional"}]
        reasons.append("historical_event_move_exceeds_complete_same_expiry_straddle_cost")
        alternatives = []
    elif selected == "NO_TRADE" and event_state != "ready":
        blockers.append("insufficient_event_evidence")
    rejected = []
    for structure in DEFINED_RISK_STRUCTURES:
        if structure != selected and structure not in alternatives:
            rejected.append({"structure": structure, "reason": _rejection_reason(structure, trend, rich_vol)})
    rejected.extend(
        {"structure": structure, "reason": "unlimited_or_unbounded_risk_prohibited"}
        for structure in PROHIBITED_STRUCTURES
    )
    evidence_refs = [
        {"kind": "symbol_feature", "version": symbol_feature.get("feature_version")},
        {"kind": "market_regime", "version": market_regime.get("feature_version")},
    ]
    if event:
        evidence_refs.append({"kind": "event_study", "reference": event.get("reference_key")})
    return {
        "route_version": ROUTE_VERSION,
        "shadow": True,
        "selected_structure": selected,
        "alternative_structures": alternatives,
        "trend_state": trend,
        "trend_confidence": symbol_feature.get("trend_confidence"),
        "volatility_state": volatility,
        "event_state": event_state,
        "iv_rv_ratio": ratio,
        "selection_reasons": reasons,
        "rejected_structures": rejected,
        "route_blockers": sorted(set(blockers)),
        "assignment_policy": assignment.snapshot(),
        "assignment_policy_version": assignment.assignment_policy_version,
        "risk_policy_version": assignment.risk_policy_version,
        "as_of": as_of.isoformat() if isinstance(as_of, datetime) else as_of,
        "evidence_refs": evidence_refs,
        "paper_quantity_authorized": False,
        "ai_can_override": False,
        "promotion_gate": route_promotion_gate({}),
    }


def _event_vol_research_ready(event: dict[str, Any]) -> bool:
    sample_size = int(event.get("sample_size") or 0)
    actual = _number(event.get("actual_move_median"))
    implied = _number(event.get("implied_move"))
    return (
        event.get("evidence_state") == "ready"
        and sample_size >= 20
        and actual is not None
        and implied is not None
        and actual > implied
        and event.get("complete_same_expiry_atm_legs") is True
    )


def _rejection_reason(structure: str, trend: str, rich_vol: bool) -> str:
    if structure.startswith("long_") and rich_vol:
        return "premium_rich_for_uncapped_long_option"
    if structure.startswith("call") and trend != "trend_up":
        return "bullish_structure_conflicts_with_trend"
    if structure.startswith("put") and trend != "trend_down":
        return "bearish_structure_conflicts_with_trend"
    if structure == "cash_secured_put":
        return "assignment_and_portfolio_risk_consent_required"
    return "lower_priority_under_current_route"


def _positive(value: Any) -> bool:
    number = _number(value)
    return number is not None and number > 0


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
