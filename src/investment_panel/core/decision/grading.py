"""Decision gating, grading, and basis assembly."""

from __future__ import annotations
from typing import Any

from investment_panel.core.decision.coerce import parse_json



def gate_reasons(
    candidate: dict[str, Any],
    freshness: dict[str, str],
    evidence_count: int,
    independent_source_count: int,
    primary_evidence_count: int,
    liquidity: dict[str, Any],
) -> list[str]:
    gates = []
    breakdown = parse_json(candidate.get("score_breakdown"))
    for gate in breakdown.get("gates", []) if isinstance(breakdown.get("gates"), list) else []:
        gates.append(str(gate))
    quote_status = freshness.get("quote_freshness", "missing")
    daily_status = freshness.get("daily_analysis_freshness", "missing")
    if quote_status in {"missing", "unknown"}:
        gates.append("missing_intraday_quote")
    elif quote_status in {"stale", "failed"}:
        gates.append("stale_intraday_quote")
    if daily_status in {"missing", "unknown"}:
        gates.append("missing_daily_analysis")
    elif daily_status in {"stale", "failed"}:
        gates.append("stale_daily_analysis")
    if freshness.get("overall_decision_freshness") in {"stale", "failed", "missing"}:
        gates.append("stale_data")
    if evidence_count < 2 or independent_source_count < 2 or primary_evidence_count < 1:
        gates.append("evidence_thin")
    grade = str(liquidity.get("grade") or "").upper()
    dollar_volume = float(liquidity.get("avg_dollar_volume") or 0)
    if grade in {"F", "D"} or (dollar_volume and dollar_volume < 1_000_000):
        gates.append("liquidity_bad")
    return sorted(set(gates))




def action_grade_for(score: float, freshness: str, evidence_count: int, source_count: int, gates: list[str]) -> str:
    hard_freshness_gates = {
        "stale_data",
        "stale_intraday_quote",
        "missing_intraday_quote",
        "stale_daily_analysis",
        "missing_daily_analysis",
        "broker_account_sync_unhealthy",
    }
    if freshness in {"stale", "failed", "degraded", "missing"} or hard_freshness_gates.intersection(gates):
        return "Stale"
    if "liquidity_bad" in gates:
        return "Reject"
    if "missing_intraday_quote" in gates or "liquidity_unknown" in gates:
        return "Watch" if score >= 60 else "Reject"
    if "evidence_thin" in gates:
        return "Watch" if score >= 60 else "Reject"
    if evidence_count < 2 or source_count < 2:
        return "Watch" if score >= 60 else "Reject"
    if score >= 90:
        return "Act"
    if score >= 75:
        return "Research"
    if score >= 55:
        return "Watch"
    return "Reject"




def apply_blocking_penalties(score: float, gates: list[str]) -> float:
    penalties = {
        "stale_data": 25,
        "stale_intraday_quote": 18,
        "missing_intraday_quote": 18,
        "stale_daily_analysis": 15,
        "missing_daily_analysis": 15,
        "liquidity_unknown": 12,
        "liquidity_bad": 30,
        "evidence_thin": 10,
        "broker_account_sync_unhealthy": 30,
    }
    return max(0.0, score - sum(penalties.get(gate, 0) for gate in gates))




def decision_basis(
    symbol: str,
    decision_score: float,
    action_score: float,
    discovery_score: float,
    universe: dict[str, Any],
    quote: dict[str, Any],
    liquidity: dict[str, Any],
    event: dict[str, Any],
    evidence_count: int,
    raw_source_rows: int,
    independent_source_count: int,
    evidence_items_count: int,
    primary_evidence_count: int,
    freshness: dict[str, str],
) -> dict[str, Any]:
    return {
        "summary": f"{symbol} action score {round(action_score, 2)} from decision score {round(decision_score, 2)}.",
        "discovery_score": round(discovery_score, 2),
        "decision_score": round(decision_score, 2),
        "action_score": round(action_score, 2),
        "inclusion_reasons": universe.get("inclusion_reasons") or [],
        "source_counts": universe.get("source_counts") or {},
        "source_count": universe.get("source_count") or 0,
        "raw_source_rows": raw_source_rows,
        "independent_source_count": independent_source_count,
        "evidence_count": evidence_count,
        "evidence_items_count": evidence_items_count,
        "primary_evidence_count": primary_evidence_count,
        "eligibility_status": universe.get("eligibility_status"),
        "asset_class": universe.get("asset_class"),
        "freshness": freshness,
        "latest_quote": quote.get("price"),
        "liquidity_grade": liquidity.get("grade"),
        "catalyst": event.get("event"),
    }




def invalidation_for(action_grade: str, gates: list[str]) -> str:
    if any("stale" in gate for gate in gates):
        return "Refresh source data before making an investment decision."
    if any("liquidity" in gate for gate in gates):
        return "Do not act unless liquidity improves enough to size safely."
    if action_grade in {"Act", "Research"}:
        return "Thesis weakens if source evidence is contradicted or trend/liquidity support breaks."
    return "Needs stronger evidence, catalyst confirmation, or improved liquidity before action."




def catalyst_window(row: dict[str, Any]) -> str:
    event_date = row.get("event_date")
    if not event_date:
        return "-"
    return f"{event_date}: {row.get('event') or 'event'}"




def portfolio_impact(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {"owned": False}
    return {
        "owned": True,
        "quantity": row.get("quantity"),
        "avg_cost": row.get("avg_cost") or row.get("average_cost"),
        "source": row.get("source"),
        "market_value": row.get("market_value"),
    }
