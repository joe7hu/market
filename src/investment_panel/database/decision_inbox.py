"""Decision-only Inbox and Telegram-owner notification outbox.

The tables deliberately contain only actionable ticket lifecycle and critical
risk events.  Provider, scheduler, and agent failures remain health data.
"""

from __future__ import annotations

from collections.abc import Mapping
import base64
import json
from datetime import UTC, datetime, timedelta
import math
from typing import Any, Callable

from psycopg.types.json import Jsonb

from investment_panel.core.decision import TradePlan
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.ticker_decisions import plan_authority


INBOX_EVENT_TYPES = frozenset({
    "ready", "revoked", "expired", "paper_filled", "paper_exited",
    "portfolio_critical", "paper_engine_halt", "high_priority_research",
})
# Research delivery is durable in the Inbox but can never become a paper-order
# or Telegram path.  READY is the only option ticket state that may notify.
TELEGRAM_EVENT_TYPES = INBOX_EVENT_TYPES - {"high_priority_research"}
CANONICAL_TRANSITIONS = frozenset({
    "newly_actionable", "action_changed", "thesis_invalidated", "decision_authority_degraded",
})
ACTIONABLE_TRANSITIONS = frozenset({"newly_actionable", "action_changed"})
CANONICAL_STATE_KEY = "canonical_decision_transition"
TICKER_PAPER_TRANSITIONS = frozenset({"paper_staged", "paper_filled", "paper_exited"})
TICKER_PAPER_STATE_KEY = "canonical_ticker_paper_lifecycle"
PORTFOLIO_RISK_STATE_TRANSITION = "portfolio_risk_breached"
PORTFOLIO_RISK_LOCK_KEY = "decision-inbox:portfolio-risk"


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
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        if event_type.strip().lower() not in INBOX_EVENT_TYPES:
            raise ValueError("unsupported decision Inbox event type")
        with self.runtime.transaction() as connection:
            return self._emit_in_transaction(
                connection,
                event_type=event_type,
                payload=payload,
                opportunity_id=opportunity_id,
                ticket_version=ticket_version,
                paper_order_id=paper_order_id,
                lane=lane,
                severity=severity,
                enqueue_telegram=enqueue_telegram,
                dedupe_key=dedupe_key,
            )

    def _emit_in_transaction(
        self,
        connection: Any,
        *,
        event_type: str,
        payload: dict[str, Any],
        opportunity_id: str | None = None,
        ticket_version: int | None = None,
        paper_order_id: str | None = None,
        lane: str | None = None,
        severity: str = "info",
        enqueue_telegram: bool = True,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        normalized_type = event_type.strip().lower()
        if normalized_type not in INBOX_EVENT_TYPES:
            raise ValueError("unsupported decision Inbox event type")
        normalized_lane = (lane or str(payload.get("lane") or "")).lower() or None
        if normalized_lane not in {None, "radar", "qqq", "recovery", "ticker"}:
            raise ValueError("unsupported paper lane")
        if severity not in {"info", "warning", "critical"}:
            raise ValueError("unsupported decision Inbox severity")
        key = dedupe_key or _dedupe_key(
            event_type=normalized_type,
            opportunity_id=opportunity_id,
            ticket_version=ticket_version,
            paper_order_id=paper_order_id,
        )
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

    def sync_current_decisions(
        self, rows: list[dict[str, Any]], *, now: datetime | None = None,
    ) -> dict[str, int]:
        """Record post-activation transitions from the canonical ticker model."""

        reference = _utc(now)
        activation_at, is_bootstrap = self._decision_sync_activation(reference)
        created = {transition: 0 for transition in CANONICAL_TRANSITIONS}
        if is_bootstrap:
            return created
        for row in _current_rows_after_activation(rows, activation_at, reference):
            episode_id = str(row["opportunity_episode_id"])
            revision = str(row["decision_revision"])
            policy_version = str(row["policy_version"])
            try:
                plan, plan_blocker = plan_authority(row)
            except (AttributeError, KeyError, TypeError, ValueError):
                plan, plan_blocker = None, "ticker_decision_lineage_invalid"
            exact_authority = plan is not None and plan_blocker is None
            raw_resolution = row.get("resolution")
            if not isinstance(raw_resolution, dict):
                continue
            resolution = dict(raw_resolution)
            lifecycle = str(resolution.get("lifecycle") or "").upper()
            prior = self._latest_canonical_state(episode_id, activation_at)
            transition: str | None = None
            if exact_authority and lifecycle == "INVALIDATED":
                transition = "thesis_invalidated" if prior and prior["actionable"] else None
            elif exact_authority and plan.eligibility == "ACTIONABLE":
                if not prior or not prior["actionable"]:
                    transition = "newly_actionable"
                elif str(prior.get("action") or "") != plan.action:
                    transition = "action_changed"
            elif prior and prior["actionable"]:
                transition = "decision_authority_degraded"
            if transition is None:
                continue
            blocker = None
            next_action = None
            if transition == "decision_authority_degraded":
                blocker = (
                    plan.primary_blocker if exact_authority and plan is not None
                    else plan_blocker
                ) or "trade_plan_authority_invalid"
                next_action = "Refresh and republish the canonical TradePlan authority."
                if exact_authority and plan is not None:
                    next_action = plan.next_action
            payload = _canonical_event_payload(
                row,
                plan if exact_authority else None,
                transition=transition,
                published_at=_parse_time(row["published_at"]),
                blocker=blocker,
                next_action=next_action,
            )
            dedupe_key = _canonical_dedupe_key(episode_id, revision, transition, policy_version)
            with self.runtime.transaction() as connection:
                event = self._emit_in_transaction(
                    connection,
                    event_type="ready" if transition in ACTIONABLE_TRANSITIONS else "revoked",
                    payload=payload,
                    severity="info" if transition in ACTIONABLE_TRANSITIONS else "warning",
                    dedupe_key=dedupe_key,
                )
                self._resolve_canonical_actionable_item(
                    connection, episode_id, exclude_dedupe_key=dedupe_key,
                )
            created[transition] += int(event["created"])
        return created

    def _decision_sync_activation(self, reference: datetime) -> tuple[datetime, bool]:
        """Return the durable canonical decision watermark and bootstrap state."""

        with self.runtime.transaction() as connection:
            inserted = connection.execute(
                """
                INSERT INTO app.decision_inbox_sync_state (state_key, activated_at)
                VALUES (%s, %s)
                ON CONFLICT (state_key) DO NOTHING
                RETURNING activated_at
                """,
                [CANONICAL_STATE_KEY, reference],
            ).fetchone()
            if inserted is not None:
                return inserted["activated_at"], True
            existing = connection.execute(
                "SELECT activated_at FROM app.decision_inbox_sync_state WHERE state_key = %s",
                [CANONICAL_STATE_KEY],
            ).fetchone()
            assert existing is not None
            return existing["activated_at"], False

    def sync_ticker_paper_lifecycle(
        self, *, now: datetime | None = None,
    ) -> dict[str, int]:
        """Record post-activation lifecycle facts for canonical ticker paper orders."""

        reference = _utc(now)
        activation_at, is_bootstrap = self._paper_lifecycle_activation(reference)
        created = {transition: 0 for transition in TICKER_PAPER_TRANSITIONS}
        if is_bootstrap:
            return created

        with self.runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT paper.id::text, instrument.symbol AS instrument_symbol,
                       paper.created_at, paper.status, paper.quantity, paper.filled_quantity,
                       paper.filled_at, paper.actual_fill_price, paper.exited_quantity, paper.exit_at,
                       paper.exit_price, paper.fees, paper.unfilled_reason, paper.lane, paper.paper_only,
                       paper.ticker_decision_revision, paper.expression_kind, paper.policy_result,
                       paper.policy_snapshot, paper.thesis_snapshot, paper.ticket_snapshot
                FROM app.paper_order paper
                JOIN catalog.instrument instrument ON instrument.id = paper.instrument_id
                WHERE paper.lane = 'ticker' AND paper.paper_only = TRUE
                ORDER BY paper.created_at, paper.id
                """
            ).fetchall()

        candidates: dict[str, list[dict[str, Any]]] = {}
        for raw in rows:
            order = _validated_ticker_paper_order(dict(raw))
            if order is None:
                continue
            events = _ticker_paper_events(order, activation_at, reference)
            if events:
                candidates[str(order["id"])] = events

        for paper_order_id in sorted(candidates):
            with self.runtime.transaction() as connection:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    [f"paper-order:decision-inbox:ticker-lifecycle:{paper_order_id}"],
                )
                row = connection.execute(
                    """
                    SELECT paper.id::text, instrument.symbol AS instrument_symbol,
                           paper.created_at, paper.status, paper.quantity, paper.filled_quantity,
                           paper.filled_at, paper.actual_fill_price, paper.exited_quantity, paper.exit_at,
                           paper.exit_price, paper.fees, paper.unfilled_reason, paper.lane, paper.paper_only,
                           paper.ticker_decision_revision, paper.expression_kind, paper.policy_result,
                           paper.policy_snapshot, paper.thesis_snapshot, paper.ticket_snapshot
                    FROM app.paper_order paper
                    JOIN catalog.instrument instrument ON instrument.id = paper.instrument_id
                    WHERE paper.id = %s::uuid AND paper.lane = 'ticker' AND paper.paper_only = TRUE
                    FOR UPDATE
                    """,
                    [paper_order_id],
                ).fetchone()
                order = _validated_ticker_paper_order(dict(row)) if row is not None else None
                if order is None:
                    continue
                for event in _ticker_paper_events(order, activation_at, reference):
                    transition = str(event["state_transition"])
                    emitted = self._emit_in_transaction(
                        connection,
                        event_type={"paper_staged": "ready", "paper_filled": "paper_filled", "paper_exited": "paper_exited"}[transition],
                        payload=event,
                        paper_order_id=paper_order_id,
                        lane="ticker",
                        severity="info",
                        dedupe_key=_ticker_paper_dedupe_key(
                            paper_order_id,
                            str(event["trade_plan_id"]),
                            str(event["decision_revision"]),
                            str(event["policy_version"]),
                            transition,
                        ),
                    )
                    self._resolve_ticker_paper_items(connection, event)
                    created[transition] += int(emitted["created"])
        return created

    def _paper_lifecycle_activation(self, reference: datetime) -> tuple[datetime, bool]:
        """Return the durable ticker paper lifecycle watermark and bootstrap state."""

        with self.runtime.transaction() as connection:
            inserted = connection.execute(
                """
                INSERT INTO app.decision_inbox_sync_state (state_key, activated_at)
                VALUES (%s, %s)
                ON CONFLICT (state_key) DO NOTHING
                RETURNING activated_at
                """,
                [TICKER_PAPER_STATE_KEY, reference],
            ).fetchone()
            if inserted is not None:
                return inserted["activated_at"], True
            existing = connection.execute(
                "SELECT activated_at FROM app.decision_inbox_sync_state WHERE state_key = %s",
                [TICKER_PAPER_STATE_KEY],
            ).fetchone()
            assert existing is not None
            return existing["activated_at"], False

    def sync_current_portfolio_risk(
        self,
        cards: list[dict[str, Any]] | None,
        summary: Mapping[str, Any] | list[dict[str, Any]] | None,
        *,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Record only current critical portfolio-risk cards."""

        reference = _utc(now)
        current = _validated_portfolio_risk_cards(cards, summary, reference)
        if current is None:
            return {"breached": 0, "resolved": 0}
        current_by_id, summary_times = current

        with self.runtime.transaction() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [PORTFOLIO_RISK_LOCK_KEY],
            )
            active_rows = connection.execute(
                """
                SELECT id::text, payload
                FROM app.decision_inbox_item
                WHERE event_type = 'portfolio_critical' AND status = 'active'
                ORDER BY created_at DESC, id DESC
                FOR UPDATE
                """
            ).fetchall()
            history_rows = connection.execute(
                """
                SELECT payload
                FROM app.decision_inbox_item
                WHERE event_type = 'portfolio_critical'
                FOR UPDATE
                """
            ).fetchall()

            highest_sequence: dict[str, int] = {}
            for row in history_rows:
                payload = row.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                card_id = _compact_text(payload.get("card_id"))
                sequence = _int(payload.get("breach_sequence"))
                if card_id and sequence is not None and sequence > highest_sequence.get(card_id, 0):
                    highest_sequence[card_id] = sequence

            active_by_card: dict[str, list[str]] = {}
            for row in active_rows:
                payload = row.get("payload")
                card_id = _compact_text(payload.get("card_id")) if isinstance(payload, Mapping) else ""
                if card_id:
                    active_by_card.setdefault(card_id, []).append(str(row["id"]))

            breached = 0
            for card_id, card in current_by_id.items():
                if card["severity"] != "critical" or card_id in active_by_card:
                    continue
                sequence = highest_sequence.get(card_id, 0) + 1
                payload = _portfolio_risk_payload(card, summary_times, sequence)
                event = self._emit_in_transaction(
                    connection,
                    event_type="portfolio_critical",
                    payload=payload,
                    severity="critical",
                    dedupe_key=_portfolio_risk_dedupe_key(card_id, sequence),
                )
                breached += int(event["created"])

            stale_item_ids: list[str] = []
            for card_id, item_ids in active_by_card.items():
                if current_by_id.get(card_id, {}).get("severity") == "critical":
                    stale_item_ids.extend(item_ids[1:])
                else:
                    stale_item_ids.extend(item_ids)
            resolved = self._resolve_portfolio_risk_items(connection, stale_item_ids)
        return {"breached": breached, "resolved": resolved}

    def _resolve_ticker_paper_items(self, connection: Any, event: dict[str, Any]) -> None:
        transition = str(event["state_transition"])
        paper_order_id = str(event["paper_order_id"])
        if transition == "paper_staged":
            connection.execute(
                """
                UPDATE app.decision_inbox_item
                SET status = 'resolved', resolved_at = now()
                WHERE status = 'active' AND event_type = 'ready'
                  AND payload->>'ticker' = %s
                  AND payload->>'opportunity_episode_id' = %s
                  AND payload->>'trade_plan_id' = %s
                  AND payload->>'decision_revision' = %s
                  AND payload->>'policy_version' = %s
                  AND payload->>'state_transition' IN ('newly_actionable', 'action_changed')
                """,
                [
                    event["ticker"], event["opportunity_episode_id"],
                    event["trade_plan_id"], event["decision_revision"],
                    event["policy_version"],
                ],
            )
            return
        transitions = {
            "paper_filled": ("paper_staged",),
            "paper_exited": ("paper_staged", "paper_filled"),
        }[transition]
        connection.execute(
            """
            UPDATE app.decision_inbox_item
            SET status = 'resolved', resolved_at = now()
            WHERE status = 'active' AND paper_order_id = %s::uuid
              AND payload->>'state_transition' = ANY(%s::text[])
            """,
            [paper_order_id, list(transitions)],
        )

    def _latest_canonical_state(self, episode_id: str, activation_at: datetime) -> dict[str, Any] | None:
        with self.runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT payload
                FROM app.decision_inbox_item
                WHERE payload->>'opportunity_episode_id' = %s
                  AND payload->>'state_transition' IN (
                      'newly_actionable', 'action_changed',
                      'thesis_invalidated', 'decision_authority_degraded'
                  )
                  AND created_at >= %s
                ORDER BY created_at DESC, id DESC
                LIMIT 20
                """,
                [episode_id, activation_at],
            ).fetchall()
        for row in rows:
            payload = dict(row["payload"] or {})
            if payload.get("state_transition") in CANONICAL_TRANSITIONS:
                return {
                    "actionable": str(payload.get("state") or "").upper() == "ACTIONABLE",
                    "action": payload.get("action"),
                }
        return None

    def _resolve_canonical_actionable_item(
        self, connection: Any, episode_id: str, *, exclude_dedupe_key: str,
    ) -> None:
        connection.execute(
            """
            UPDATE app.decision_inbox_item
            SET status = 'resolved', resolved_at = now()
            WHERE status = 'active' AND event_type = 'ready'
              AND payload->>'opportunity_episode_id' = %s
              AND payload->>'state_transition' IN ('newly_actionable', 'action_changed')
              AND dedupe_key <> %s
            """,
            [episode_id, exclude_dedupe_key],
        )

    def _resolve_portfolio_risk_items(self, connection: Any, item_ids: list[str]) -> int:
        if not item_ids:
            return 0
        result = connection.execute(
            """
            UPDATE app.decision_inbox_item
            SET status = 'resolved', resolved_at = now()
            WHERE id = ANY(%s::uuid[]) AND status = 'active'
            """,
            [item_ids],
        )
        return int(result.rowcount)

    def _resolve_ready_item(self, decision_id: str, ticket_version: int) -> None:
        with self.runtime.transaction() as connection:
            connection.execute(
                """
                UPDATE app.decision_inbox_item
                SET status = 'resolved', resolved_at = now()
                WHERE event_type = 'ready' AND opportunity_id = %s::uuid
                  AND ticket_version = %s AND status = 'active'
                """,
                [decision_id, ticket_version],
            )

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

    if payload.get("state_transition") in TICKER_PAPER_TRANSITIONS:
        ticker = str(payload.get("ticker") or "TICKER")
        parts = ["PAPER ONLY", f"{ticker} · {payload['state_transition']}"]
        for key, label in (
            ("paper_order_id", "Paper order"),
            ("trade_plan_id", "TradePlan"),
            ("quantity", "Quantity"),
            ("filled_quantity", "Filled quantity"),
            ("exited_quantity", "Exited quantity"),
            ("fill_price", "Fill price"),
            ("exit_price", "Exit price"),
            ("fees", "Fees"),
            ("reason", "Reason"),
        ):
            value = payload.get(key)
            if value is not None and str(value).strip():
                parts.append(f"{label}: {value}")
        detail_url = str(payload.get("detail_url") or "").strip()
        if detail_url:
            parts.append(detail_url)
        return "\n".join(parts)[:4096]

    if payload.get("state_transition") == PORTFOLIO_RISK_STATE_TRANSITION:
        parts = [
            "PORTFOLIO RISK · CRITICAL",
            _compact_text(payload.get("title")) or "Portfolio risk exception",
        ]
        symbols = payload.get("symbols")
        if isinstance(symbols, (list, tuple)):
            symbol_text = ", ".join(str(symbol).strip() for symbol in symbols if str(symbol).strip())
        else:
            symbol_text = str(symbols or "").strip()
        if symbol_text:
            parts.append(f"Symbols: {symbol_text}")
        impact = _compact_text(payload.get("impact"))
        if impact:
            parts.append(f"Impact: {impact}")
        next_action = _compact_text(payload.get("next_action"))
        if next_action:
            parts.append(f"Next: {next_action}")
        detail_url = _compact_text(payload.get("detail_url"))
        if detail_url:
            parts.append(detail_url)
        return "\n".join(parts)[:4096]

    if payload.get("state_transition") in CANONICAL_TRANSITIONS:
        ticker = str(payload.get("ticker") or payload.get("symbol") or "TICKER")
        transition = str(payload["state_transition"])
        action = str(payload.get("action") or "NO_TRADE").upper()
        parts = [f"{ticker} · {transition}", f"Action: {action}"]
        expression = str(payload.get("selected_expression_kind") or "").strip()
        if expression and action != "NO_TRADE":
            parts.append(f"Expression: {expression}")
        expires = payload.get("expires_at")
        if expires:
            parts.append(f"Valid until: {expires}")
        blocker = str(payload.get("primary_blocker") or "").strip()
        if blocker:
            parts.append(f"Blocker: {blocker}")
        next_action = str(payload.get("next_action") or "").strip()
        if next_action:
            parts.append(f"Next: {next_action}")
        detail_url = str(payload.get("detail_url") or "").strip()
        if detail_url:
            parts.append(detail_url)
        return "\n".join(parts)[:4096]

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


def _current_rows_after_activation(
    rows: list[dict[str, Any]], activation_at: datetime, reference: datetime,
) -> list[dict[str, Any]]:
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        try:
            row = dict(raw)
        except (TypeError, ValueError):
            continue
        ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
        episode_id = str(row.get("opportunity_episode_id") or "").strip()
        revision = str(row.get("decision_revision") or "").strip()
        policy_version = str(row.get("policy_version") or "").strip()
        published_at = _parse_time(row.get("published_at"))
        if not ticker or not episode_id or not revision or not policy_version or published_at is None:
            continue
        if published_at <= activation_at or published_at > reference:
            continue
        as_of = _parse_time(row.get("as_of"))
        if as_of is not None and as_of > reference:
            continue
        if row.get("status") is not None and str(row["status"]).lower() != "published":
            continue
        by_ticker.setdefault(ticker, []).append(row)
    unique = [items[0] for items in by_ticker.values() if len(items) == 1]
    by_episode: dict[str, list[dict[str, Any]]] = {}
    for row in unique:
        by_episode.setdefault(str(row["opportunity_episode_id"]), []).append(row)
    duplicate_episodes = {
        episode_id for episode_id, items in by_episode.items() if len(items) > 1
    }
    return sorted(
        (row for row in unique if str(row["opportunity_episode_id"]) not in duplicate_episodes),
        key=lambda row: (
            _parse_time(row["published_at"]) or datetime.min.replace(tzinfo=UTC),
            str(row.get("ticker") or row.get("symbol") or ""),
        ),
    )


def _canonical_event_payload(
    row: dict[str, Any],
    plan: Any,
    *,
    transition: str,
    published_at: datetime | None,
    blocker: str | None,
    next_action: str | None,
) -> dict[str, Any]:
    ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
    payload: dict[str, Any] = {
        "ticker": ticker,
        "symbol": ticker,
        "opportunity_episode_id": str(row["opportunity_episode_id"]),
        "decision_revision": str(row["decision_revision"]),
        "policy_version": str(row["policy_version"]),
        "state_transition": transition,
        "state": "ACTIONABLE" if transition in ACTIONABLE_TRANSITIONS else "NO_TRADE",
        "action": plan.action if transition in ACTIONABLE_TRANSITIONS and plan is not None else "NO_TRADE",
        "authorization_mode": (
            plan.authorization_mode
            if transition in ACTIONABLE_TRANSITIONS and plan is not None
            else "NONE"
        ),
        "detail_url": f"/tickers/{ticker}" if ticker else "/tickers",
    }
    if published_at is not None:
        payload["published_at"] = published_at.isoformat()
    if transition in ACTIONABLE_TRANSITIONS and plan is not None:
        payload.update({
            "eligibility": plan.eligibility,
            "data_quality": plan.data_quality,
            "trade_plan_id": plan.trade_plan_id,
            "selected_expression_kind": plan.selected_expression_kind.value,
            "selected_expression_identity": plan.selected_expression_identity,
            "expires_at": plan.expiry.isoformat() if plan.expiry is not None else None,
            "rationale": _compact_text(plan.rationale),
        })
    elif transition == "thesis_invalidated":
        payload["lifecycle"] = "INVALIDATED"
    else:
        payload.update({
            "eligibility": "BLOCKED",
            "primary_blocker": _compact_text(blocker),
            "blockers": [_compact_text(blocker)],
            "next_action": _compact_text(next_action),
        })
    return payload


def _canonical_dedupe_key(
    episode_id: str, decision_revision: str, transition: str, policy_version: str,
) -> str:
    return "canonical:" + json.dumps(
        [episode_id, decision_revision, transition, policy_version], separators=(",", ":"),
    )


def _validated_ticker_paper_order(row: dict[str, Any]) -> dict[str, Any] | None:
    if str(row.get("lane") or "").strip().lower() != "ticker" or row.get("paper_only") is not True:
        return None
    paper_order_id = str(row.get("id") or "").strip()
    revision = str(row.get("ticker_decision_revision") or "").strip()
    expression_kind = str(row.get("expression_kind") or "").strip().upper()
    if not paper_order_id or not revision or not expression_kind:
        return None
    created_at = _parse_time(row.get("created_at"))
    if created_at is None or not str(row.get("status") or "").strip():
        return None

    plans: list[TradePlan] = []
    for field in ("policy_snapshot", "policy_result", "ticket_snapshot", "thesis_snapshot"):
        container = row.get(field)
        if not isinstance(container, Mapping) or "trade_plan" not in container:
            continue
        raw_plan = container.get("trade_plan")
        if not isinstance(raw_plan, Mapping):
            return None
        try:
            plans.append(TradePlan.model_validate(raw_plan))
        except (KeyError, TypeError, ValueError):
            return None
    if not plans or any(plan.model_dump(mode="json") != plans[0].model_dump(mode="json") for plan in plans[1:]):
        return None
    plan = plans[0]
    publication_id = str(plan.publication_id or "").strip()
    plan_cutoff = _parse_time(plan.cutoff)
    if (
        not publication_id
        or plan_cutoff is None
        or plan_cutoff > created_at
        or plan.eligibility != "ACTIONABLE"
        or plan.authorization_mode != "PAPER"
        or plan.decision_revision != revision
        or str(row.get("expression_kind") or "").upper() != plan.selected_expression_kind.value
        or str(row.get("instrument_symbol") or "").strip() != plan.ticker
    ):
        return None

    policy_sources = [
        value for field in ("policy_snapshot", "policy_result")
        if isinstance((value := row.get(field)), Mapping) and value
    ]
    if not policy_sources:
        return None
    owners = [str(source.get("owner") or "").strip() for source in policy_sources if source.get("owner") is not None]
    if not owners or any(owner != "ticker-first" for owner in owners):
        return None
    expected_policy = {
        "decision_revision": plan.decision_revision,
        "policy_version": plan.policy_version,
        "trade_plan_id": plan.trade_plan_id,
        "trade_plan_publication_id": publication_id,
        "expression_kind": plan.selected_expression_kind.value,
    }
    for key, expected in expected_policy.items():
        values = [source.get(key) for source in policy_sources if source.get(key) not in (None, "")]
        if not values or any(str(value) != str(expected) for value in values):
            return None
    expected_identity = {
        "paper_order_id": paper_order_id,
        "ticker": plan.ticker,
        "opportunity_episode_id": plan.opportunity_episode_id,
        "selected_expression_identity": plan.selected_expression_identity,
    }
    for source in policy_sources:
        rank = source.get("opportunity_rank")
        if rank is not None and not isinstance(rank, Mapping):
            return None
        for key, expected in expected_identity.items():
            if key in source and str(source.get(key) or "") != str(expected):
                return None
        if isinstance(rank, Mapping):
            for key in ("ticker", "opportunity_episode_id", "selected_expression_identity"):
                if key in rank and str(rank.get(key) or "") != str(expected_identity[key]):
                    return None
    for field in ("ticket_snapshot", "thesis_snapshot"):
        snapshot = row.get(field)
        if not isinstance(snapshot, Mapping):
            continue
        for key, expected in (
            ("ticker", plan.ticker),
            ("opportunity_episode_id", plan.opportunity_episode_id),
            ("decision_revision", plan.decision_revision),
            ("policy_version", plan.policy_version),
        ):
            if key in snapshot and str(snapshot.get(key) or "") != str(expected):
                return None
    result = dict(row)
    result["_trade_plan"] = plan
    return result


def _ticker_paper_events(
    order: dict[str, Any], activation_at: datetime, reference: datetime,
) -> list[dict[str, Any]]:
    plan = order["_trade_plan"]
    quantity = _positive_finite_number(order.get("quantity"))
    created_at = _parse_time(order.get("created_at"))
    plan_cutoff = _parse_time(plan.cutoff)
    if (
        quantity is None
        or created_at is None
        or plan_cutoff is None
        or plan_cutoff > reference
        or created_at > reference
    ):
        return []

    def post_activation(stamp: datetime | None) -> bool:
        return stamp is not None and activation_at < stamp <= reference

    events: list[dict[str, Any]] = []
    if post_activation(created_at):
        events.append(_ticker_paper_payload(order, plan, "paper_staged", created_at, quantity))

    filled_quantity = _positive_finite_number(order.get("filled_quantity"))
    filled_at = _parse_time(order.get("filled_at"))
    if (
        filled_quantity is not None
        and filled_quantity <= quantity
        and filled_at is not None
        and created_at <= filled_at <= reference
        and post_activation(filled_at)
    ):
        events.append(
            _ticker_paper_payload(
                order, plan, "paper_filled", filled_at, quantity,
                filled_quantity=filled_quantity,
            )
        )

    exited_quantity = _positive_finite_number(order.get("exited_quantity"))
    exit_at = _parse_time(order.get("exit_at"))
    if (
        str(order.get("status") or "").lower() == "exited"
        and filled_quantity is not None
        and exited_quantity is not None
        and exited_quantity <= filled_quantity
        and filled_at is not None
        and exit_at is not None
        and created_at <= filled_at <= exit_at <= reference
        and post_activation(exit_at)
    ):
        events.append(
            _ticker_paper_payload(
                order, plan, "paper_exited", exit_at, quantity,
                filled_quantity=filled_quantity, exited_quantity=exited_quantity,
            )
        )
    return events


def _ticker_paper_payload(
    order: dict[str, Any], plan: TradePlan, transition: str, transition_at: datetime,
    quantity: float, *, filled_quantity: float | None = None,
    exited_quantity: float | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "paper_only": True,
        "ticker": plan.ticker,
        "state_transition": transition,
        "paper_order_id": str(order["id"]),
        "trade_plan_id": plan.trade_plan_id,
        "trade_plan_publication_id": str(plan.publication_id),
        "opportunity_episode_id": plan.opportunity_episode_id,
        "decision_revision": plan.decision_revision,
        "policy_version": plan.policy_version,
        "selected_expression_kind": plan.selected_expression_kind.value,
        "selected_expression_identity": plan.selected_expression_identity,
        "transition_at": transition_at.isoformat(),
        "quantity": quantity,
        "detail_url": f"/tickers/{plan.ticker}",
    }
    if filled_quantity is not None:
        payload["filled_quantity"] = filled_quantity
    if exited_quantity is not None:
        payload["exited_quantity"] = exited_quantity
    if transition in {"paper_filled", "paper_exited"}:
        fill_price = _positive_finite_number(order.get("actual_fill_price"))
        if fill_price is not None:
            payload["fill_price"] = fill_price
    if transition == "paper_exited":
        exit_price = _positive_finite_number(order.get("exit_price"))
        if exit_price is not None:
            payload["exit_price"] = exit_price
    fees = _finite_number(order.get("fees"))
    if fees is not None:
        payload["fees"] = fees
    reason = _compact_text(order.get("unfilled_reason"))
    if reason:
        payload["reason"] = reason
    return payload


def _ticker_paper_dedupe_key(
    paper_order_id: str, trade_plan_id: str, decision_revision: str,
    policy_version: str, transition: str,
) -> str:
    return "ticker-paper:" + json.dumps(
        [paper_order_id, trade_plan_id, decision_revision, policy_version, transition],
        separators=(",", ":"),
    )


def _portfolio_risk_dedupe_key(card_id: str, breach_sequence: int) -> str:
    return "portfolio-risk:" + json.dumps([card_id, breach_sequence], separators=(",", ":"))


def _portfolio_risk_payload(
    card: Mapping[str, Any], summary_times: Mapping[str, str], breach_sequence: int,
) -> dict[str, Any]:
    return {
        "card_id": card["card_id"],
        "risk_type": card["risk_type"],
        "severity": card["severity"],
        "title": card["title"],
        "summary": card["summary"],
        "symbols": card["symbols"],
        "impact": card["impact"],
        "next_action": card["next_action"],
        **summary_times,
        "breach_sequence": breach_sequence,
        "state_transition": PORTFOLIO_RISK_STATE_TRANSITION,
        "detail_url": "/portfolio",
    }


def _validated_portfolio_risk_cards(
    cards: list[dict[str, Any]] | None,
    summary: Mapping[str, Any] | list[dict[str, Any]] | None,
    reference: datetime,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]] | None:
    if not isinstance(cards, list) or _contains_nonfinite(cards):
        return None
    if isinstance(summary, list):
        if len(summary) != 1 or not isinstance(summary[0], Mapping):
            return None
        summary = summary[0]
    if not isinstance(summary, Mapping) or _contains_nonfinite(summary):
        return None

    parsed_summary_times: dict[str, datetime] = {}
    for name in ("as_of", "available_at"):
        parsed = _strict_parse_time(summary.get(name))
        if parsed is None or parsed > reference:
            return None
        parsed_summary_times[name] = parsed
    if parsed_summary_times["as_of"] > parsed_summary_times["available_at"]:
        return None
    summary_times = {
        name: parsed.isoformat() for name, parsed in parsed_summary_times.items()
    }

    current: dict[str, dict[str, Any]] = {}
    for raw in cards:
        if not isinstance(raw, Mapping):
            return None
        card_id = raw.get("card_id")
        risk_type = raw.get("risk_type")
        severity = raw.get("severity")
        if not all(isinstance(value, str) and value.strip() for value in (card_id, risk_type, severity)):
            return None
        normalized_id = card_id.strip()
        normalized_type = risk_type.strip().lower()
        normalized_severity = severity.strip().lower()
        canonical_identity = (
            (normalized_id == "largest-position" and normalized_type == "concentration")
            or (
                normalized_id.startswith("correlation:")
                and len(normalized_id) > len("correlation:")
                and normalized_type == "correlation"
            )
            or (normalized_id == "portfolio-drawdown" and normalized_type == "drawdown")
            or (normalized_id == "stale-owned-quotes" and normalized_type == "data_freshness")
        )
        if not canonical_identity or normalized_severity not in {"critical", "watch", "info"} or (
            normalized_type == "data_freshness" and normalized_severity == "critical"
        ):
            return None
        if normalized_id in current:
            return None

        values: dict[str, str] = {}
        for name in ("title", "summary", "impact"):
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip():
                return None
            values[name] = _compact_text(value)
        next_action = raw.get("next_action")
        next_step = raw.get("next_step")
        if next_action is not None and next_step is not None and str(next_action).strip() != str(next_step).strip():
            return None
        next_value = next_action if next_action is not None else next_step
        if not isinstance(next_value, str) or not next_value.strip():
            return None

        symbols = raw.get("symbols")
        if not isinstance(symbols, list):
            return None
        normalized_symbols: set[str] = set()
        for symbol in symbols:
            if not isinstance(symbol, str) or not symbol.strip():
                return None
            normalized_symbols.add(symbol.strip().upper())

        card_times: dict[str, datetime] = {}
        for name in ("as_of", "available_at", "updated_at", "created_at"):
            if name in raw and raw[name] is not None:
                parsed = _strict_parse_time(raw[name])
                if parsed is None or parsed > reference:
                    return None
                card_times[name] = parsed
        if (
            "as_of" in card_times
            and "available_at" in card_times
            and card_times["as_of"] > card_times["available_at"]
        ):
            return None

        current[normalized_id] = {
            "card_id": normalized_id,
            "risk_type": normalized_type,
            "severity": normalized_severity,
            **values,
            "symbols": sorted(normalized_symbols),
            "next_action": _compact_text(next_value),
        }
    return current, summary_times


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


def _compact_text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


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


def _strict_parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None and value.utcoffset() is not None else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
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
    except (TypeError, ValueError, OverflowError):
        return None


def _finite_number(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and math.isfinite(number) else None


def _positive_finite_number(value: Any) -> float | None:
    number = _finite_number(value)
    return number if number is not None and number > 0 else None


def _contains_nonfinite(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_nonfinite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_nonfinite(item) for item in value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return not math.isfinite(value)
    return False


def _first(value: Any) -> str | None:
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else None
    return str(value) if value else None
