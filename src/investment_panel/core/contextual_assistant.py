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
    source_ids = sorted({str(item) for item in coverage.get("sources", []) if str(item).strip()})
    missing: list[str] = []
    if not revision:
        missing.append("decision_revision")
    if not manifest.get("input_hash"):
        missing.append("input_manifest")
    if coverage.get("status") != "available":
        missing.append("source_evidence")
    identity = {
        "ticker": symbol,
        "decision_revision": revision,
        "input_hash": manifest.get("input_hash"),
        "as_of": decision.get("as_of"),
        "coverage_status": coverage.get("status"),
        "source_ids": source_ids,
        "missing_evidence": missing,
    }
    packet_id = "packet:" + sha256(json.dumps(identity, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()[:24]
    citations = [{
        "id": f"decision:{symbol}:{revision or 'missing'}",
        "label": f"{symbol} decision {revision or 'revision unavailable'}",
        "kind": "decision",
        "available": bool(revision),
    }]
    citations.extend({"id": f"source:{source}", "label": source, "kind": "source", "available": True} for source in source_ids)
    return {
        "packet_id": packet_id,
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
