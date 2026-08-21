"""Tactical and fundamental decision math for Event Decision Packets."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from investment_panel.core.event_truth import EVENT_SCOUT_ROUTE_VERSION


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _value(value: Any) -> Any:
    return value.get("value") if isinstance(value, Mapping) and "value" in value else value


def _decision_values(category: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _value(value) for key, value in category.items() if isinstance(value, Mapping) and "value" in value}


def build_event_decisions(
    *,
    symbol: str,
    as_of: datetime,
    market_tape: Mapping[str, Any],
    positioning: Mapping[str, Any],
    fundamentals: Mapping[str, Any],
    platform: Mapping[str, Any],
    history: Mapping[str, Any],
    risk_inputs: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    tape = _decision_values(market_tape)
    pos = _decision_values(positioning)
    fund = _decision_values(fundamentals)
    risk = dict(risk_inputs or {})
    volume_short_ratio = _number(pos.get("volume_over_latest_reported_short_shares"))
    move = _number(tape.get("change_from_event_pct") or tape.get("event_return_pct") or tape.get("day_change_pct"))
    short_pct = _number(pos.get("short_pct_float"))
    dtc = _number(pos.get("days_to_cover"))
    squeeze_risk = bool(volume_short_ratio and volume_short_ratio > 1 and ((short_pct or 0) >= 10 or (dtc or 0) >= 5))
    missing_risk: list[str] = []
    max_loss = _number(risk.get("max_loss"))
    spread = _number(tape.get("bid_ask_spread"))
    liquidity = str(tape.get("liquidity_status") or "").lower()
    if max_loss is None or max_loss <= 0:
        missing_risk.append("max_loss_required")
    if spread is None:
        missing_risk.append("spread_required")
    if not liquidity or liquidity in {"unknown", "missing", "illiquid"}:
        missing_risk.append("liquidity_required")
    if str(tape.get("halt_status") or "").lower() in {"halted", "unknown"}:
        missing_risk.append("halt_status_not_clear")
    if _value(tape.get("latest_price")) is None:
        missing_risk.append("latest_price_required")
    if _value(tape.get("volume")) is None:
        missing_risk.append("volume_required")
    # Squeeze risk forbids shorting. It does not forbid a defined-risk long
    # paper probe when the full price and liquidity risk packet is present.
    paper_eligible = not missing_risk
    tactical_blockers = list(dict.fromkeys(missing_risk))
    support = [
        "official event source remains valid",
        "price holds the event reference while volume remains above its packet baseline",
    ]
    invalidation = [
        "event thesis is withdrawn or contradicted by a verified source",
        "price loses the event reference with falling volume",
    ]
    if squeeze_risk:
        support.insert(1, "reported short interest and days-to-cover remain elevated")
    tactical = {
        "horizon": "minutes_to_days",
        "stance": "event_driven_momentum_watch" if move is not None and move > 0 else "event_watch",
        "continuation": "possible_but_unconfirmed" if move is not None else "unknown",
        "squeeze_risk": "high" if squeeze_risk else "unknown" if volume_short_ratio is None else "not_high_on_available_inputs",
        "retracement_risk": "high" if move is not None and move >= 50 else "unknown" if move is None else "elevated",
        "liquidity_risk": "high" if missing_risk else "bounded_by_packet_inputs",
        "do_not_short": squeeze_risk,
        "risk_warning": "Do not short: event momentum plus reported short interest can create squeeze risk." if squeeze_risk else None,
        "support_conditions": support,
        "invalidation_conditions": invalidation,
        "maximum_loss": max_loss,
        "paper_only_momentum_probe": {
            "eligible": paper_eligible,
            "maximum_loss": max_loss if paper_eligible else None,
            "blockers": [] if paper_eligible else tactical_blockers,
        },
        "uncertainty": {
            "price_range": "expanded" if not paper_eligible or move is None else "bounded",
            "reason": "missing risk or event evidence inputs are uncertainty, not a bearish signal",
        },
        "blockers": tactical_blockers,
        "next_action": "paper_only_momentum_probe_with_defined_max_loss" if paper_eligible else "wait_for_price_discovery_and_complete_liquidity_inputs",
    }
    missing_fundamentals = [
        name for name in ("hazard_ratio", "confidence_interval", "p_value", "absolute_benefit", "os_trend", "regulatory_path", "possible_label")
        if fund.get(name) is None
    ]
    fundamental = {
        "horizon": "months_to_years",
        "underwriting_state": "UNDERWRITTEN" if not missing_fundamentals else "UNUNDERWRITTEN",
        "clinical_success_probability": None,
        "commercial_opportunity": _value(platform.get("platform_value_extension")) if platform else None,
        "valuation_range": None,
        "unresolved_questions": missing_fundamentals or ["validate manufacturing and partner economics"],
        "confidence": "high" if not missing_fundamentals else "low",
        "narrative_uncertainty": "expanded" if missing_fundamentals else "bounded",
        "not_bearish_by_missing_data": True,
        "blockers": ["fundamental_evidence_incomplete"] if missing_fundamentals else [],
        "next_action": "obtain_hazard_ratio_absolute_benefit_os_and_regulatory_path" if missing_fundamentals else "refresh_valuation_and_manufacturing_underwriting",
    }
    blockers = list(dict.fromkeys([*tactical_blockers, *fundamental["blockers"]]))
    candidate_state = "SETUP" if move is not None and move > 0 else "RESEARCH"
    truth = {
        "symbol": symbol,
        "lane": "event_scout",
        "as_of": as_of.isoformat(),
        "publication_id": None,
        "candidate_state": candidate_state,
        "route_verdict": "NO_TRADE",
        "readiness_state": "ready" if not blockers else "incomplete",
        "execution_state": "PAPER_ONLY_PROBE_ELIGIBLE" if paper_eligible else "DISABLED",
        "primary_blocker": blockers[0] if blockers else None,
        "blockers": blockers,
        "next_action": tactical["next_action"] if tactical_blockers else fundamental["next_action"],
        "route_version": EVENT_SCOUT_ROUTE_VERSION,
        "evidence_refs": [],
        "tactical_route_verdict": "NO_TRADE",
        "fundamental_route_verdict": "UNUNDERWRITTEN" if missing_fundamentals else "UNDERWRITTEN",
        "history_evidence_state": {
            "intraday": history.get("intraday", {}).get("evidence_state"),
            "monthly_yearly": history.get("monthly_yearly", {}).get("evidence_state"),
        },
    }
    return tactical, fundamental, truth


__all__ = ["build_event_decisions"]
