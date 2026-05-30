"""Actionable signal read model."""

from __future__ import annotations

import json
from typing import Any

from investment_panel.core.db import query_rows


def signal_rows(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT c.run_date, c.symbol, i.name, i.asset_class, i.category,
               c.score, c.decision, c.score_breakdown, c.evidence,
               tf.features
        FROM candidates c
        LEFT JOIN instruments i ON i.symbol = c.symbol
        LEFT JOIN technical_features tf ON tf.symbol = c.symbol
        QUALIFY row_number() OVER (PARTITION BY c.symbol ORDER BY c.run_date DESC, c.score DESC, tf.date DESC NULLS LAST) = 1
        ORDER BY c.score DESC
        LIMIT 100
        """,
    )
    return [_compact_empty_fields(to_signal(row)) for row in rows]


def to_signal(row: dict[str, Any]) -> dict[str, Any]:
    breakdown = parse_json(row.get("score_breakdown"))
    evidence = parse_json(row.get("evidence"))
    features = parse_json(row.get("features"))
    components = breakdown.get("components", {})
    gates = breakdown.get("gates", [])
    score = float(row.get("score") or 0)
    decision = row.get("decision") or "monitor"
    evidence_count = len(evidence) if isinstance(evidence, list) else 0
    confidence = confidence_for(score, evidence_count, gates)
    why_now = why_now_for(components, features, evidence_count, gates)
    invalidation = invalidation_for(row, components, gates)
    next_action = next_action_for(decision, score, confidence, gates)
    return {
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "asset_class": row.get("asset_class"),
        "category": row.get("category"),
        "signal_grade": grade_for(score),
        "confidence": confidence,
        "decision": decision,
        "score": round(score, 2),
        "why_now": why_now,
        "evidence_count": evidence_count,
        "invalidation": invalidation,
        "next_action": next_action,
        "source_freshness": source_freshness(features),
        "components": components,
        "gates": gates,
        "price_source": features.get("price_source") or features.get("source"),
    }


def _compact_empty_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}


def grade_for(score: float) -> str:
    if score >= 82:
        return "A"
    if score >= 72:
        return "B"
    if score >= 62:
        return "C"
    if score >= 50:
        return "D"
    return "F"


def confidence_for(score: float, evidence_count: int, gates: list[str]) -> str:
    if gates:
        return "low"
    if score >= 75 and evidence_count >= 2:
        return "high"
    if score >= 62 or evidence_count >= 1:
        return "medium"
    return "low"


def why_now_for(components: dict[str, Any], features: dict[str, Any], evidence_count: int, gates: list[str]) -> str:
    reasons = []
    if float(components.get("technical") or 0) >= 70:
        reasons.append("constructive trend/momentum")
    if evidence_count:
        reasons.append(f"{evidence_count} thesis evidence item(s)")
    if float(components.get("category") or 0) >= 60:
        reasons.append("category tailwind")
    if gates:
        reasons.append("gated: " + ", ".join(gates))
    if not reasons:
        return "No action-grade catalyst yet."
    return "; ".join(reasons)


def invalidation_for(row: dict[str, Any], components: dict[str, Any], gates: list[str]) -> str:
    if "data_stale" in gates:
        return "Refresh source data before acting."
    if float(components.get("thesis") or 0) < 35:
        return "No specific thesis evidence connects the setup to fundamentals or a catalyst."
    return "Signal weakens if price loses trend support or new evidence contradicts the thesis."


def next_action_for(decision: str, score: float, confidence: str, gates: list[str]) -> str:
    if gates:
        return "Do not act; clear gates first."
    if decision == "research":
        return "Generate memo and verify against primary sources before sizing."
    if decision == "watch":
        return "Add to watchlist; wait for entry zone or catalyst confirmation."
    if score >= 50:
        return "Monitor; needs stronger evidence."
    return "Ignore unless new evidence arrives."


def source_freshness(features: dict[str, Any]) -> str:
    date = features.get("date")
    return f"technical features through {date}" if date else "no technical feature date"


def parse_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}
