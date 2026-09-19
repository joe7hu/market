"""Fail-closed packet and citation rules for contextual research assistance."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any


def build_contextual_packet(payload: dict[str, Any], ticker: str) -> dict[str, Any]:
    decision = payload.get("ticker_decision") if isinstance(payload.get("ticker_decision"), dict) else {}
    symbol = str(decision.get("ticker") or ticker).strip().upper()
    revision = str(decision.get("decision_revision") or "").strip()
    manifest = decision.get("input_manifest") if isinstance(decision.get("input_manifest"), dict) else {}
    dossier = payload.get("dossier") if isinstance(payload.get("dossier"), dict) else {}
    sources = dossier.get("sources") if isinstance(dossier.get("sources"), dict) else {}
    coverage = sources.get("coverage") if isinstance(sources.get("coverage"), dict) else {}
    source_ids = sorted({str(item) for item in (coverage.get("sources") or []) if isinstance(item, str) and item.strip()}) if isinstance(coverage.get("sources"), list) else []
    missing: list[str] = []
    if not revision:
        missing.append("decision_revision")
    if not manifest.get("input_hash"):
        missing.append("input_manifest")
    if coverage.get("status") != "available":
        missing.append("source_evidence")
    resolution = decision.get("resolution") if isinstance(decision.get("resolution"), dict) else {}
    action = decision.get("capital_action") if isinstance(decision.get("capital_action"), dict) else {}
    requests = [item for item in decision.get("data_requests", []) if isinstance(item, dict)][:12]
    horizons = {name: decision.get(name) or {} for name in ("tactical", "fundamental")}
    # Explicitly bounded presentation evidence. Do not serialize the raw input
    # manifest, account credentials, or arbitrary provider payload into chat.
    explanation_evidence = {
        "action": resolution.get("action") or action.get("action"),
        "eligibility": resolution.get("eligibility"),
        "rationale": resolution.get("rationale") or action.get("rationale"),
        "blockers": list(resolution.get("blockers") or []),
        "next_action": resolution.get("next_action"),
        "catalyst": resolution.get("catalyst") or action.get("catalyst"),
        "price_condition": resolution.get("price_condition") or action.get("price_condition"),
        "requests": [{key: item.get(key) for key in ("field", "why_it_matters", "owner", "collect_now", "required_source")} for item in requests],
        "horizons": {name: {key: value.get(key) for key in ("stance", "action", "fact_that_would_flip", "evidence_against", "invalidation")}
                     for name, value in horizons.items() if isinstance(value, dict)},
    }
    identity = {
        "ticker": symbol,
        "decision_revision": revision,
        "input_hash": manifest.get("input_hash"),
        "as_of": decision.get("as_of"),
        "coverage_status": coverage.get("status"),
        "source_ids": source_ids,
        "missing_evidence": missing,
        "explanation_evidence": explanation_evidence,
    }
    packet_id = "packet:" + sha256(json.dumps(identity, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()[:24]
    citations = [{
        "id": f"decision:{symbol}:{revision or 'missing'}",
        "label": f"{symbol} decision {revision or 'revision unavailable'}",
        "kind": "decision",
        "available": bool(revision),
    }]
    citations.extend({"id": f"source:{source}", "label": source, "kind": "source", "available": True} for source in source_ids)
    decision_citation = citations[0]["id"]
    why = str(explanation_evidence["rationale"] or "No decision rationale was published for this revision.")
    blockers = explanation_evidence["blockers"]
    if blockers:
        why += " Blocked by: " + "; ".join(str(value).replace("_", " ") for value in blockers[:8]) + "."
    next_steps = [str(explanation_evidence["next_action"])] if explanation_evidence["next_action"] else []
    next_steps.extend(f"{item['field']}: {item.get('collect_now') or item.get('why_it_matters') or 'inspect source coverage'} (owner: {item.get('owner') or 'not recorded'})" for item in explanation_evidence["requests"])
    change = [f"{name}: " + str(values["fact_that_would_flip"].get("statement") or "No falsifying statement recorded")
              for name, values in explanation_evidence["horizons"].items() if isinstance(values.get("fact_that_would_flip"), dict)]
    explanations = [
        {"topic": "decision", "question": "Why this decision?", "answer": why},
        {"topic": "repair", "question": "What needs to happen next?", "answer": "\n".join(next_steps) or "No concrete next action was recorded. Open the full decision to inspect its missing evidence."},
        {"topic": "countercase", "question": "What would change the thesis?", "answer": "\n".join(change) or "No falsifying fact was recorded in this decision."},
    ]
    for item in explanations:
        item["citation_ids"] = [decision_citation] if revision else []
    return {
        "packet_id": packet_id,
        "explanation_evidence": explanation_evidence,
        "explanations": explanations,
        "ticker": symbol,
        "decision_revision": revision or None,
        "as_of": decision.get("as_of"),
        "authority": "postgresql",
        "execution_mode": "paper_only",
        "citations": citations,
        "missing_evidence": missing,
        "limitations": ["Advisory only; this packet cannot authorize orders, risk changes, or gate bypasses."],
    }


def validate_contextual_response(packet: dict[str, Any], citation_ids: list[str], *, requested_calculation: bool = False) -> dict[str, Any]:
    allowed = {str(item.get("id")) for item in packet.get("citations", []) if isinstance(item, dict) and item.get("available")}
    requested = [str(item) for item in citation_ids]
    invalid = [item for item in requested if item not in allowed]
    limitations = list(packet.get("limitations") or [])
    if packet.get("missing_evidence"):
        limitations.append("Evidence unavailable: " + ", ".join(str(item) for item in packet["missing_evidence"]) + ".")
    if requested_calculation:
        limitations.append("Authoritative values must come from the PostgreSQL decision read model; the assistant cannot calculate or authorize them.")
    if invalid:
        limitations.append("Response rejected: citation is outside the immutable packet.")
    return {"status": "limited" if limitations or invalid else "ready", "citations": [item for item in requested if item in allowed], "limitations": limitations}
