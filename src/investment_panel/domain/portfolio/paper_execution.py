"""Causal quote consumption shared by the funded paper execution owners.

Valuations may carry a previous close. Fills may not: every leg must have a new
regular-session observation after the order, and a displayed quote can fund at
most one fill of that order. Persistence is the execution owner's transaction.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from investment_panel.domain.decision import MARKET_TZ, is_market_open, market_session_bounds, normalized_utc

CONSUMPTION_KEY = "quote_consumption_v1"


def quote_consumption_blocker(
    order: dict[str, Any], quotes: list[dict[str, Any]], *, now: datetime, phase: str,
) -> str | None:
    now = normalized_utc(now)
    if not is_market_open(now):
        return "market_closed_wait_for_next_session"
    if phase not in {"entry", "exit"} or not quotes:
        return "paper_quote_evidence_missing"
    created = _timestamp(order.get("created_at"))
    if created is None or created > now:
        return "paper_order_clock_missing_or_invalid"
    floor = created
    # Legacy positions have no consumption ledger. Require a post-fill quote,
    # rather than inventing old quote IDs or rewriting the historical journal.
    if (filled := _timestamp(order.get("filled_at"))) is not None:
        floor = max(floor, filled)
    if (exited := _timestamp(order.get("exit_at"))) is not None:
        floor = max(floor, exited)
    evidence = order.get("execution_quote")
    if evidence is None:
        evidence = {}
    if not isinstance(evidence, dict):
        return "paper_quote_consumption_invalid"
    state = evidence.get(CONSUMPTION_KEY)
    if state is None:
        state = {}
    if not isinstance(state, dict) or any(
        not isinstance(record, dict) or not isinstance(record.get("quotes"), list)
        or any(not isinstance(prior, dict) for prior in record["quotes"])
        for record in state.values()
    ):
        return "paper_quote_consumption_invalid"
    opened, _ = market_session_bounds(now.astimezone(MARKET_TZ).date())
    for quote in quotes:
        observed = _timestamp(quote.get("observed_at"))
        available = _timestamp(quote.get("available_at"))
        if observed is None or available is None:
            return "paper_quote_clocks_missing"
        if not observed <= available <= now:
            return "paper_quote_clock_conflict"
        if observed < opened or now - observed > timedelta(seconds=120):
            return "fresh_regular_session_quote_required"
        identity = str(quote.get("contract_id") or order.get("instrument_id"))
        for consumed in state.values():
            for prior in consumed.get("quotes", []):
                if str(prior.get("instrument")) != identity:
                    continue
                prior_at = _timestamp(prior.get("observed_at"))
                if prior_at is None or observed <= prior_at:
                    return "quote_already_consumed_wait_for_new_observation"
                if quote.get("quote_id") is not None and str(quote["quote_id"]) == str(prior.get("quote_id")):
                    return "quote_already_consumed_wait_for_new_observation"
        if observed <= floor:
            return "post_order_quote_required"
    return None


def consumed_quote_evidence(
    order: dict[str, Any], quotes: list[dict[str, Any]], *, now: datetime, phase: str,
) -> dict[str, Any]:
    """Return the new order evidence; caller writes this with the actual fill."""
    evidence = dict(order.get("execution_quote") or {})
    state = dict(evidence.get(CONSUMPTION_KEY) or {})
    state[phase] = {
        "filled_at": normalized_utc(now).isoformat(),
        "quotes": [{
            "instrument": str(quote.get("contract_id") or order.get("instrument_id")),
            "quote_id": str(quote["quote_id"]) if quote.get("quote_id") is not None else None,
            "source_id": quote.get("source_id"),
            "observed_at": _timestamp(quote.get("observed_at")).isoformat(),
            "available_at": _timestamp(quote.get("available_at")).isoformat(),
        } for quote in quotes],
    }
    evidence[CONSUMPTION_KEY] = state
    return evidence


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None
