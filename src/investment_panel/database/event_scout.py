"""PostgreSQL persistence for Event Scout packets and shared decision truth."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from psycopg.types.json import Jsonb

from investment_panel.core.event_scout import build_event_decision_packet
from investment_panel.database.runtime import DatabaseRuntime


def persist_event_packet(
    runtime: DatabaseRuntime,
    packet: Mapping[str, Any],
    scout_event: Mapping[str, Any] | None = None,
    *,
    enforce_cooldown: bool = False,
    reference_at: datetime | None = None,
    cooldown_minutes: int = 30,
) -> dict[str, Any] | None:
    """Atomically publish a packet, its shared truth, and the Scout event."""

    event = dict(scout_event or {})
    truth = dict(packet["decision_truth"])
    reference = reference_at or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    reference = reference.astimezone(UTC)
    with runtime.transaction() as connection:
        if enforce_cooldown and event:
            symbol = str(event.get("symbol") or packet["symbol"]).strip().upper()
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"event-scout:{symbol}"],
            )
            latest = connection.execute(
                """
                SELECT observed_at, cooldown_until
                FROM analysis.event_scout_event
                WHERE upper(symbol) = %s
                ORDER BY observed_at DESC, event_id DESC
                LIMIT 1
                FOR UPDATE
                """,
                [symbol],
            ).fetchone()
            latest_cooldown = latest["cooldown_until"] if latest else None
            if latest_cooldown is None and latest and latest["observed_at"] is not None:
                latest_cooldown = latest["observed_at"] + timedelta(minutes=max(1, cooldown_minutes))
            if latest_cooldown is not None and reference < latest_cooldown:
                return None
            event["cooldown_until"] = (
                reference + timedelta(minutes=max(1, cooldown_minutes))
            ).isoformat()
        connection.execute(
            """
            INSERT INTO analysis.event_decision_packet (
                event_id, symbol, event_kind, trigger_type, as_of, publication_id, headline,
                market_tape, positioning, event_fundamentals, platform_optionality,
                historical_cases, tactical_decision, fundamental_decision, decision_truth,
                evidence_refs, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (event_id) DO UPDATE SET
                symbol = EXCLUDED.symbol,
                event_kind = EXCLUDED.event_kind,
                trigger_type = EXCLUDED.trigger_type,
                as_of = EXCLUDED.as_of,
                publication_id = EXCLUDED.publication_id,
                headline = EXCLUDED.headline,
                market_tape = EXCLUDED.market_tape,
                positioning = EXCLUDED.positioning,
                event_fundamentals = EXCLUDED.event_fundamentals,
                platform_optionality = EXCLUDED.platform_optionality,
                historical_cases = EXCLUDED.historical_cases,
                tactical_decision = EXCLUDED.tactical_decision,
                fundamental_decision = EXCLUDED.fundamental_decision,
                decision_truth = EXCLUDED.decision_truth,
                evidence_refs = EXCLUDED.evidence_refs,
                updated_at = now()
            """,
            [
                packet["event_id"], packet["symbol"], packet["event_kind"], packet["trigger_type"], packet["as_of"],
                packet.get("publication_id"), packet.get("headline"), Jsonb(packet["market_tape"]), Jsonb(packet["positioning"]),
                Jsonb(packet["event_fundamentals"]), Jsonb(packet["platform_optionality"]), Jsonb(packet["historical_cases"]),
                Jsonb(packet["tactical_decision"]), Jsonb(packet["fundamental_decision"]), Jsonb(packet["decision_truth"]),
                Jsonb(packet["evidence_refs"]),
            ],
        )
        connection.execute(
            """
            INSERT INTO app.decision_truth (
                symbol, lane, as_of, publication_id, candidate_state, route_verdict,
                readiness_state, execution_state, primary_blocker, blockers, next_action,
                route_version, evidence_refs, event_id, raw, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (symbol, lane) DO UPDATE SET
                as_of = EXCLUDED.as_of,
                publication_id = EXCLUDED.publication_id,
                candidate_state = EXCLUDED.candidate_state,
                route_verdict = EXCLUDED.route_verdict,
                readiness_state = EXCLUDED.readiness_state,
                execution_state = EXCLUDED.execution_state,
                primary_blocker = EXCLUDED.primary_blocker,
                blockers = EXCLUDED.blockers,
                next_action = EXCLUDED.next_action,
                route_version = EXCLUDED.route_version,
                evidence_refs = EXCLUDED.evidence_refs,
                event_id = EXCLUDED.event_id,
                raw = EXCLUDED.raw,
                updated_at = now()
            WHERE EXCLUDED.as_of >= app.decision_truth.as_of
            """,
            [
                truth["symbol"], truth["lane"], truth["as_of"], truth.get("publication_id"), truth.get("candidate_state"),
                truth.get("route_verdict"), truth.get("readiness_state"), truth.get("execution_state"), truth.get("primary_blocker"),
                Jsonb(truth.get("blockers") or []), truth.get("next_action"), truth.get("route_version"), Jsonb(truth.get("evidence_refs") or []),
                packet["event_id"], Jsonb(truth),
            ],
        )
        if event:
            connection.execute(
                """
                INSERT INTO analysis.event_scout_event (
                    event_id, symbol, trigger_type, observed_at, source_url, source_kind,
                    status, cooldown_until, collection_status, raw
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO UPDATE SET
                    symbol = EXCLUDED.symbol,
                    trigger_type = EXCLUDED.trigger_type,
                    observed_at = EXCLUDED.observed_at,
                    source_url = EXCLUDED.source_url,
                    source_kind = EXCLUDED.source_kind,
                    status = EXCLUDED.status,
                    cooldown_until = EXCLUDED.cooldown_until,
                    collection_status = EXCLUDED.collection_status,
                    raw = EXCLUDED.raw
                """,
                [
                    packet["event_id"], event.get("symbol", packet["symbol"]), event.get("trigger_type", packet["trigger_type"]),
                    event.get("observed_at", packet["as_of"]), event.get("source_url"), event.get("source_kind"),
                    event.get("status", "accepted"), event.get("cooldown_until"), Jsonb(event.get("collection_status") or {}), Jsonb(event),
                ],
            )
    return dict(packet)


def event_decision_packet_rows(runtime: DatabaseRuntime, *, symbol: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    clause = "WHERE upper(symbol) = %s" if symbol else ""
    params: list[Any] = [symbol.upper()] if symbol else []
    params.append(max(1, min(int(limit), 500)))
    with runtime.read() as connection:
        rows = connection.execute(
            f"""
            SELECT event_id, symbol, event_kind, trigger_type, as_of, publication_id, headline,
                   market_tape, positioning, event_fundamentals, platform_optionality,
                   historical_cases, tactical_decision, fundamental_decision, decision_truth,
                   evidence_refs, created_at, updated_at
            FROM analysis.event_decision_packet
            {clause}
            ORDER BY as_of DESC, event_id DESC
            LIMIT %s
            """,
            params,
        )
        return [dict(row) for row in rows.fetchall()]


def decision_truth_rows(runtime: DatabaseRuntime, *, symbol: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    clause = "WHERE upper(symbol) = %s" if symbol else ""
    params: list[Any] = [symbol.upper()] if symbol else []
    params.append(max(1, min(int(limit), 1000)))
    with runtime.read() as connection:
        rows = connection.execute(
            f"""
            SELECT symbol, lane, as_of, publication_id, candidate_state, route_verdict,
                   readiness_state, execution_state, primary_blocker, blockers,
                   next_action, route_version, evidence_refs, event_id, raw, updated_at
            FROM app.decision_truth
            {clause}
            ORDER BY as_of DESC, symbol
            LIMIT %s
            """,
            params,
        )
        return [dict(row) for row in rows.fetchall()]


def event_scout_rows(runtime: DatabaseRuntime, *, symbol: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    clause = "WHERE upper(symbol) = %s" if symbol else ""
    params: list[Any] = [symbol.upper()] if symbol else []
    params.append(max(1, min(int(limit), 500)))
    with runtime.read() as connection:
        rows = connection.execute(
            f"""
            SELECT event_id, symbol, trigger_type, observed_at, source_url, source_kind,
                   status, cooldown_until, collection_status, raw
            FROM analysis.event_scout_event
            {clause}
            ORDER BY observed_at DESC, event_id DESC
            LIMIT %s
            """,
            params,
        )
        return [dict(row) for row in rows.fetchall()]


def latest_event_scout_seen(runtime: DatabaseRuntime, *, symbol: str) -> dict[str, Any] | None:
    """Return the latest accepted Scout timestamp used by the cooldown gate."""

    with runtime.read() as connection:
        row = connection.execute(
            """
            SELECT symbol, observed_at, cooldown_until
            FROM analysis.event_scout_event
            WHERE upper(symbol) = %s
            ORDER BY observed_at DESC, event_id DESC
            LIMIT 1
            """,
            [symbol.upper()],
        ).fetchone()
        return dict(row) if row else None


def replay_event(runtime: DatabaseRuntime, packet: Mapping[str, Any]) -> dict[str, Any]:
    built = build_event_decision_packet(**dict(packet))
    return persist_event_packet(runtime, built)


__all__ = [
    "persist_event_packet", "event_decision_packet_rows", "decision_truth_rows",
    "event_scout_rows", "latest_event_scout_seen", "replay_event",
]
