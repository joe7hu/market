"""Decision-only Inbox and Telegram-owner notification outbox.

The tables deliberately contain only actionable ticket lifecycle and critical
risk events.  Provider, scheduler, and agent failures remain health data.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime


INBOX_EVENT_TYPES = frozenset({
    "ready", "revoked", "expired", "paper_filled", "paper_exited",
    "portfolio_critical", "paper_engine_halt",
})
TELEGRAM_EVENT_TYPES = INBOX_EVENT_TYPES


class DecisionInboxRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def emit(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        opportunity_id: str | None = None,
        ticket_version: int | None = None,
        paper_order_id: str | None = None,
        lane: str | None = None,
        severity: str = "info",
        enqueue_telegram: bool = True,
    ) -> dict[str, Any]:
        normalized_type = event_type.strip().lower()
        if normalized_type not in INBOX_EVENT_TYPES:
            raise ValueError("unsupported decision Inbox event type")
        normalized_lane = (lane or str(payload.get("lane") or "")).lower() or None
        if normalized_lane not in {None, "radar", "qqq", "recovery"}:
            raise ValueError("unsupported paper lane")
        if severity not in {"info", "warning", "critical"}:
            raise ValueError("unsupported decision Inbox severity")
        key = _dedupe_key(
            event_type=normalized_type,
            opportunity_id=opportunity_id,
            ticket_version=ticket_version,
            paper_order_id=paper_order_id,
        )
        with self.runtime.transaction() as connection:
            row = connection.execute(
                """
                INSERT INTO app.decision_inbox_item
                    (dedupe_key, event_type, opportunity_id, ticket_version,
                     paper_order_id, lane, severity, payload)
                VALUES (%s, %s, %s::uuid, %s, %s::uuid, %s, %s, %s)
                ON CONFLICT (dedupe_key) DO NOTHING
                RETURNING id, created_at
                """,
                [
                    key, normalized_type, opportunity_id, ticket_version,
                    paper_order_id, normalized_lane, severity, Jsonb(_safe_payload(payload)),
                ],
            ).fetchone()
            if row is None:
                existing = connection.execute(
                    "SELECT id, created_at FROM app.decision_inbox_item WHERE dedupe_key = %s",
                    [key],
                ).fetchone()
                assert existing is not None
                return {"id": str(existing["id"]), "dedupe_key": key, "created": False, "created_at": existing["created_at"].isoformat()}
            inbox_id = str(row["id"])
            if enqueue_telegram and normalized_type in TELEGRAM_EVENT_TYPES:
                connection.execute(
                    """
                    INSERT INTO app.notification_outbox
                        (dedupe_key, inbox_item_id, event_type, payload)
                    VALUES (%s, %s::uuid, %s, %s)
                    ON CONFLICT (dedupe_key) DO NOTHING
                    """,
                    [key, inbox_id, normalized_type, Jsonb(_safe_payload(payload))],
                )
        return {"id": inbox_id, "dedupe_key": key, "created": True, "created_at": row["created_at"].isoformat()}

    def rows(self, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        bounded_limit = max(1, min(int(limit), 100))
        after = _decode_cursor(cursor)
        conditions = []
        params: list[Any] = []
        if after is not None:
            conditions.append("(item.created_at, item.id::text) < (%s, %s)")
            params.extend(after)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.runtime.read() as connection:
            result = connection.execute(
                f"""
                SELECT item.id::text, item.event_type, item.opportunity_id::text,
                       item.ticket_version, item.paper_order_id::text, item.lane,
                       item.severity, item.status, item.payload, item.created_at,
                       item.resolved_at,
                       outbox.status AS delivery_status, outbox.attempts,
                       outbox.last_error, outbox.sent_at
                FROM app.decision_inbox_item item
                LEFT JOIN LATERAL (
                    SELECT status, attempts, last_error, sent_at
                    FROM app.notification_outbox notification
                    WHERE notification.inbox_item_id = item.id
                    ORDER BY notification.created_at DESC LIMIT 1
                ) outbox ON true
                {where}
                ORDER BY item.created_at DESC, item.id DESC
                LIMIT %s
                """,
                [*params, bounded_limit + 1],
            ).fetchall()
        values = [_row_payload(row) for row in result]
        page = values[:bounded_limit]
        next_cursor = _encode_cursor(page[-1]) if len(values) > bounded_limit and page else None
        return {"items": page, "count": len(page), "next_cursor": next_cursor}

    def sync_current_tickets(self, *, now: datetime | None = None) -> dict[str, int]:
        """Create idempotent READY/revoked/expired events from current publications."""

        reference = _utc(now)
        with self.runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT item.payload
                FROM app.publication publication
                JOIN app.publication_item item ON item.publication_id = publication.id
                WHERE publication.status = 'published'
                  AND publication.published_at <= %s
                  AND (
                    (publication.scope = 'options-radar' AND item.model_name = 'option_radar_opportunity')
                    OR
                    (publication.scope = 'options-decision-system' AND item.model_name = 'options_decision_candidate')
                  )
                """,
                [reference],
            ).fetchall()
            recovery_rows = connection.execute(
                """
                SELECT signal.ticket, signal.decision_id::text, signal.status,
                       signal.available_at, instrument.symbol, signal.strategy_key
                FROM analysis.option_event_signal signal
                JOIN analysis.option_event event ON event.id = signal.event_id
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                WHERE event.status = 'active'
                  AND signal.status IN ('shadow', 'ticketed', 'entered', 'partial_exited')
                  AND signal.ticket->>'state' = 'READY'
                  AND signal.available_at <= %s
                ORDER BY signal.available_at DESC, signal.id DESC
                LIMIT 100
                """,
                [reference],
            ).fetchall()
        current: dict[tuple[str, int], dict[str, Any]] = {}
        created = {"ready": 0, "revoked": 0, "expired": 0}
        for row in rows:
            payload = dict(row["payload"] or {})
            ticket = dict(payload.get("ticket") or {})
            decision_id = str(ticket.get("decision_id") or payload.get("decision_id") or "")
            version = _int(ticket.get("ticket_version"))
            if not decision_id or version is None:
                continue
            expires_at = _parse_time(ticket.get("expires_at") or (ticket.get("entry") or {}).get("valid_until"))
            ready = str(ticket.get("state") or "").upper() == "READY" and not list(ticket.get("blockers") or [])
            event_payload = _ticket_event_payload(payload, ticket)
            if expires_at is not None and expires_at <= reference:
                current[(decision_id, version)] = {"payload": payload, "ticket": {**ticket, "state": "EXPIRED"}}
                event = self.emit(event_type="expired", payload=event_payload, opportunity_id=decision_id, ticket_version=version, lane=str(ticket.get("lane") or "radar"), severity="warning")
                created["expired"] += int(event["created"])
                if event["created"]:
                    with self.runtime.transaction() as connection:
                        connection.execute(
                            """
                            UPDATE app.decision_inbox_item
                            SET status = 'resolved', resolved_at = now()
                            WHERE event_type = 'ready' AND opportunity_id = %s::uuid
                              AND ticket_version = %s AND status = 'active'
                            """,
                            [decision_id, version],
                        )
            elif ready:
                current[(decision_id, version)] = {"payload": payload, "ticket": ticket}
                event = self.emit(event_type="ready", payload=event_payload, opportunity_id=decision_id, ticket_version=version, lane=str(ticket.get("lane") or "radar"), severity="info")
                created["ready"] += int(event["created"])
            else:
                current[(decision_id, version)] = {"payload": payload, "ticket": ticket}
        for row in recovery_rows:
            ticket = dict(row["ticket"] or {})
            decision_id = str(ticket.get("decision_id") or row["decision_id"] or "")
            version = _int(ticket.get("ticket_version"))
            if not decision_id or version is None:
                continue
            payload = {
                "decision_id": decision_id,
                "ticker": str(row["symbol"]),
                "structure": ticket.get("structure"),
                "top_reasons": [str(row["strategy_key"])],
                "ticket": ticket,
            }
            current[(decision_id, version)] = {"payload": payload, "ticket": ticket}
            event = self.emit(
                event_type="ready", payload=_ticket_event_payload(payload, ticket),
                opportunity_id=decision_id, ticket_version=version,
                lane="recovery", severity="info",
            )
            created["ready"] += int(event["created"])
        with self.runtime.read() as connection:
            active_ready = connection.execute(
                """
                SELECT opportunity_id::text, ticket_version, payload
                FROM app.decision_inbox_item
                WHERE event_type = 'ready' AND status = 'active'
                  AND opportunity_id IS NOT NULL AND ticket_version IS NOT NULL
                """
            ).fetchall()
        for prior in active_ready:
            key = (str(prior["opportunity_id"]), int(prior["ticket_version"]))
            current_ticket = current.get(key)
            if current_ticket and str(current_ticket["ticket"].get("state") or "").upper() == "READY":
                continue
            prior_payload = dict(prior["payload"] or {})
            event = self.emit(
                event_type="revoked",
                payload={**prior_payload, "state": "REVOKED", "reason": "ticket_not_current_or_not_ready"},
                opportunity_id=key[0], ticket_version=key[1],
                lane=str(prior_payload.get("lane") or "radar"), severity="warning",
            )
            created["revoked"] += int(event["created"])
            if event["created"]:
                with self.runtime.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE app.decision_inbox_item
                        SET status = 'resolved', resolved_at = now()
                        WHERE event_type = 'ready' AND opportunity_id = %s::uuid
                          AND ticket_version = %s AND status = 'active'
                        """,
                        [key[0], key[1]],
                    )
        return created

    def record_paper_lifecycle(self, paper_order_id: str, *, status: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        normalized = status.lower()
        event_type = "paper_filled" if normalized in {"entered", "filled"} else "paper_exited" if normalized in {"exited", "closed", "invalidated"} else None
        if event_type is None:
            return None
        with self.runtime.read() as connection:
            row = connection.execute(
                """
                SELECT paper.id::text, paper.decision_id::text, paper.ticket_version,
                       paper.lane, paper.status, paper.limit_price, paper.actual_fill_price,
                       paper.exit_price, paper.fees, instrument.symbol, paper.ticket_snapshot
                FROM app.paper_order paper
                JOIN catalog.instrument instrument ON instrument.id = paper.instrument_id
                WHERE paper.id = %s::uuid
                """,
                [paper_order_id],
            ).fetchone()
        if row is None:
            return None
        ticket = dict(row["ticket_snapshot"] or {})
        event_payload = _ticket_event_payload({"ticker": row["symbol"]}, ticket)
        event_payload.update({
            "status": normalized.upper(), "paper_order_id": str(row["id"]),
            "fill_price": _number(row["actual_fill_price"]), "exit_price": _number(row["exit_price"]),
            "fees": _number(row["fees"]), **dict(payload or {}),
        })
        # A fill is a lifecycle transition, not a revoked recommendation.  It
        # resolves the corresponding READY item before the fill/exit event is
        # emitted so inbox and Telegram sequencing stay truthful.
        with self.runtime.transaction() as connection:
            connection.execute(
                """
                UPDATE app.decision_inbox_item
                SET status = 'resolved', resolved_at = now()
                WHERE event_type = 'ready' AND opportunity_id = %s::uuid
                  AND ticket_version = %s AND status = 'active'
                """,
                [row["decision_id"], _int(row["ticket_version"])],
            )
        return self.emit(
            event_type=event_type, payload=event_payload,
            opportunity_id=str(row["decision_id"]) if row["decision_id"] else None,
            ticket_version=_int(row["ticket_version"]), paper_order_id=str(row["id"]),
            lane=str(row["lane"]), severity="info",
        )

    def deliver_outbox(
        self,
        *,
        sender: Callable[[str], None] | None,
        dry_run: bool,
        limit: int = 20,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Deliver only compact fixed-owner messages with bounded retry backoff."""

        reference = _utc(now)
        processed = {"sent": 0, "failed": 0, "dry_run": 0}
        for _ in range(max(1, min(int(limit), 100))):
            with self.runtime.transaction() as connection:
                row = connection.execute(
                    """
                    SELECT id::text, payload, attempts
                    FROM app.notification_outbox
                    WHERE status IN ('queued', 'failed') AND next_attempt_at <= %s
                    ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    [reference],
                ).fetchone()
                if row is None:
                    break
                connection.execute(
                    "UPDATE app.notification_outbox SET status = 'sending', attempts = attempts + 1, updated_at = %s WHERE id = %s::uuid",
                    [reference, row["id"]],
                )
            message = telegram_message(dict(row["payload"] or {}))
            if dry_run:
                with self.runtime.transaction() as connection:
                    connection.execute(
                        "UPDATE app.notification_outbox SET status = 'dry_run', sent_at = %s, updated_at = %s WHERE id = %s::uuid",
                        [reference, reference, row["id"]],
                    )
                processed["dry_run"] += 1
                continue
            try:
                if sender is None:
                    raise RuntimeError("shared GBrain Telegram owner relay is unavailable")
                sender(message)
            except Exception as exc:
                attempts = int(row["attempts"] or 0) + 1
                delay = min(3600, 30 * (2 ** min(6, attempts - 1)))
                with self.runtime.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE app.notification_outbox
                        SET status = 'failed', last_error = %s, next_attempt_at = %s,
                            updated_at = %s
                        WHERE id = %s::uuid
                        """,
                        [f"{type(exc).__name__}: {exc}"[:1000], reference + timedelta(seconds=delay), reference, row["id"]],
                    )
                processed["failed"] += 1
            else:
                with self.runtime.transaction() as connection:
                    connection.execute(
                        "UPDATE app.notification_outbox SET status = 'sent', sent_at = %s, last_error = NULL, updated_at = %s WHERE id = %s::uuid",
                        [reference, reference, row["id"]],
                    )
                processed["sent"] += 1
        return processed


def telegram_message(payload: dict[str, Any]) -> str:
    """Format an owner message without secrets or full evidence packets."""

    parts = [
        f"{str(payload.get('symbol') or 'OPTION')} · {str(payload.get('structure') or 'option').replace('_', ' ')} · {str(payload.get('lane') or 'radar')}",
        str(payload.get("state") or payload.get("event_type") or "UPDATE").upper(),
    ]
    entry = payload.get("entry")
    risk = payload.get("max_risk")
    expires = payload.get("expires_at")
    if entry is not None:
        parts.append(f"Entry: {entry}")
    if risk is not None:
        parts.append(f"Max risk: {risk}")
    if expires:
        parts.append(f"Valid until: {expires}")
    reason = str(payload.get("reason") or payload.get("primary_reason") or "").strip()
    veto = str(payload.get("veto") or payload.get("primary_blocker") or "").strip()
    if reason:
        parts.append(f"Why: {reason}")
    if veto:
        parts.append(f"Veto: {veto}")
    link = str(payload.get("detail_url") or "").strip()
    if link:
        parts.append(link)
    return "\n".join(parts)[:4096]


def _ticket_event_payload(source: dict[str, Any], ticket: dict[str, Any]) -> dict[str, Any]:
    blockers = [str(value) for value in ticket.get("blockers") or source.get("blockers") or [] if str(value)]
    entry = dict(ticket.get("entry") or {})
    risk = dict(ticket.get("risk") or {})
    decision_id = str(ticket.get("decision_id") or source.get("decision_id") or "")
    return {
        "symbol": str(ticket.get("symbol") or source.get("ticker") or source.get("symbol") or ""),
        "structure": str(ticket.get("structure") or source.get("structure") or "option"),
        "lane": str(ticket.get("lane") or "radar"),
        "state": str(ticket.get("state") or source.get("state") or ""),
        "entry": entry.get("limit_price"),
        "max_risk": risk.get("one_unit_max_loss") or risk.get("one_unit_collateral"),
        "expires_at": ticket.get("expires_at") or entry.get("valid_until"),
        "primary_reason": _first(source.get("top_reasons") or source.get("reasons")),
        "primary_blocker": _first(blockers),
        "detail_url": f"/options-radar?decision={decision_id}" if decision_id else "/options-radar",
    }


def _dedupe_key(*, event_type: str, opportunity_id: str | None, ticket_version: int | None, paper_order_id: str | None) -> str:
    owner = opportunity_id or paper_order_id or "global"
    version = "-" if ticket_version is None else str(ticket_version)
    return f"{owner}:{version}:{event_type}"


def _safe_payload(value: dict[str, Any]) -> dict[str, Any]:
    # The only persistent payload is a compact decision summary.  Drop common
    # evidence/provenance containers before a caller can accidentally enqueue
    # a large packet or an operational trace.
    excluded = {"evidence", "evidence_packet", "provenance", "agent_output", "raw"}
    return {str(key): item for key, item in value.items() if str(key) not in excluded}


def _row_payload(row: Any) -> dict[str, Any]:
    value = dict(row)
    for key in ("created_at", "resolved_at", "sent_at"):
        if value.get(key) is not None:
            value[key] = value[key].isoformat()
    value["payload"] = dict(value.get("payload") or {})
    return value


def _encode_cursor(row: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps([row["created_at"], row["id"]], separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        stamp, identifier = json.loads(raw)
        parsed = datetime.fromisoformat(str(stamp))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed, str(identifier)
    except Exception as exc:
        raise ValueError("invalid decision Inbox cursor") from exc


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    return current if current.tzinfo is not None else current.replace(tzinfo=UTC)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _first(value: Any) -> str | None:
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else None
    return str(value) if value else None
