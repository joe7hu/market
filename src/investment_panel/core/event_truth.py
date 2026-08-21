"""Shared decision-truth builders for event and options surfaces."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Iterable, Mapping


EVENT_SCOUT_ROUTE_VERSION = "event-scout-decision-v1"
OPTIONS_DECISION_ROUTE_VERSION = "options-decision-truth-v1"


def _timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=UTC)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: Any) -> str | None:
    parsed = _timestamp(value)
    return parsed.isoformat() if parsed else None


def build_decision_truth(
    *,
    symbol: str,
    lane: str,
    as_of: Any,
    candidate_state: str,
    route_verdict: str,
    readiness_state: str,
    execution_state: str,
    blockers: Iterable[Any] = (),
    next_action: str | None = None,
    route_version: str = EVENT_SCOUT_ROUTE_VERSION,
    publication_id: str | None = None,
    evidence_refs: Iterable[Mapping[str, Any] | str] = (),
) -> dict[str, Any]:
    normalized_blockers = list(dict.fromkeys(str(item) for item in blockers if str(item).strip()))
    normalized_refs: list[Any] = []
    for ref in evidence_refs:
        normalized_refs.append(ref if isinstance(ref, Mapping) else {"type": "source", "url": str(ref)})
    return {
        "symbol": str(symbol).strip().upper(),
        "lane": lane,
        "as_of": _iso(as_of),
        "publication_id": publication_id,
        "candidate_state": candidate_state,
        "route_verdict": route_verdict,
        "readiness_state": readiness_state,
        "execution_state": execution_state,
        "primary_blocker": normalized_blockers[0] if normalized_blockers else None,
        "blockers": normalized_blockers,
        "next_action": next_action,
        "route_version": route_version,
        "evidence_refs": normalized_refs,
    }


def build_options_decision_truth(
    row: Mapping[str, Any],
    *,
    lane: str = "options_radar",
    publication_id: str | None = None,
) -> dict[str, Any]:
    """Build the fail-closed truth used by Radar, Today, and Options Box."""

    strategy_route = row.get("strategy_route")
    route = dict(strategy_route) if isinstance(strategy_route, Mapping) else {}
    ticket = row.get("ticket")
    ticket_data = dict(ticket) if isinstance(ticket, Mapping) else {}
    blockers = list(row.get("blockers") or [])
    blockers.extend(route.get("route_blockers") or [])
    blockers.extend(ticket_data.get("blockers") or [])
    if not route:
        blockers.append("route_incomplete")
    selected_structure = route.get("selected_structure")
    structure = row.get("structure")
    if route and not selected_structure:
        blockers.append("route_structure_missing")
    elif route and selected_structure and structure and str(selected_structure) != str(structure):
        blockers.append("route_structure_mismatch")
    if row.get("data_readiness") is not None and str(row.get("data_readiness")) != "A":
        blockers.append("data_readiness_incomplete")
    execution_ready = bool(row.get("execution_ready")) or ticket_data.get("state") == "READY"
    if not execution_ready:
        blockers.append("execution_gate_incomplete")
    normalized_blockers = list(dict.fromkeys(str(item) for item in blockers if str(item).strip()))
    ready = not normalized_blockers
    evidence_refs = row.get("evidence_refs") or route.get("evidence_refs") or []
    if isinstance(evidence_refs, (str, Mapping)):
        evidence_refs = [evidence_refs]
    return build_decision_truth(
        symbol=str(row.get("symbol") or row.get("ticker") or ""),
        lane=lane,
        as_of=row.get("as_of") or row.get("quote_observed_at") or row.get("analysis_cutoff") or row.get("evaluated_at"),
        candidate_state=str(row.get("candidate_state") or row.get("paper_state") or row.get("state") or "SETUP"),
        route_verdict="PAPER_ONLY" if ready else "NO_TRADE",
        readiness_state="ready" if ready else "incomplete",
        execution_state="PAPER_ONLY_READY" if ready else "PAPER_ONLY_BLOCKED",
        blockers=normalized_blockers,
        next_action=(
            ticket_data.get("required_next_action")
            or row.get("next_evidence")
            or "Complete route, readiness, and paper execution gates before considering this candidate."
        ),
        route_version=str(route.get("route_version") or row.get("route_version") or OPTIONS_DECISION_ROUTE_VERSION),
        publication_id=publication_id or row.get("publication_id"),
        evidence_refs=evidence_refs,
    )


__all__ = [
    "EVENT_SCOUT_ROUTE_VERSION", "OPTIONS_DECISION_ROUTE_VERSION",
    "build_decision_truth", "build_options_decision_truth",
]
