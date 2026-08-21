"""PostgreSQL direct queries for Event Scout panel read models."""

from __future__ import annotations


EVENT_DIRECT_QUERIES: dict[str, str] = {
    "event_decision_packets": """
        SELECT event_id, symbol, event_kind, trigger_type, as_of, publication_id, headline,
               market_tape, positioning, event_fundamentals, platform_optionality,
               historical_cases, tactical_decision, fundamental_decision,
               decision_truth, evidence_refs, created_at, updated_at
        FROM analysis.event_decision_packet
        ORDER BY as_of DESC, event_id DESC
        LIMIT 200
    """,
    "decision_truth": """
        SELECT symbol, lane, as_of, publication_id, candidate_state, route_verdict,
               readiness_state, execution_state, primary_blocker, blockers,
               next_action, route_version, evidence_refs, event_id, raw, updated_at
        FROM app.decision_truth
        ORDER BY as_of DESC, symbol
        LIMIT 500
    """,
    "event_scout_events": """
        SELECT event_id, symbol, trigger_type, observed_at, source_url, source_kind,
               status, cooldown_until, collection_status, raw
        FROM analysis.event_scout_event
        ORDER BY observed_at DESC, event_id DESC
        LIMIT 200
    """,
}
