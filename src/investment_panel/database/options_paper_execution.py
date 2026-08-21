"""Conservative execution and lifecycle for Radar and QQQ paper tickets.

This owner never calls a brokerage API.  It stages only immutable, current
READY tickets, then fills from a later point-in-time option quote package at
the pessimistic side of the market.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from math import floor
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from investment_panel.core.decision import is_market_open
from investment_panel.core.option_trade_ticket import execution_policy
from investment_panel.core.options_recovery import FEE_PER_CONTRACT_LEG
from investment_panel.database.actions import ActionRepository
from investment_panel.database.decision_inbox import DecisionInboxRepository
from investment_panel.database.opportunity_scorecards import OpportunityScorecardRepository
from investment_panel.database.options_paper_ledger import acquire_shared_sleeve_lock
from investment_panel.database.options_paper_quotes import (
    is_credit_structure,
    latest_option_legs,
    package_price,
)
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


GENERIC_LANES = frozenset({"radar", "qqq"})
TERMINAL_STATUSES = frozenset({"exited", "invalidated", "unfilled", "rejected", "unmeasurable"})


class OptionsPaperExecutionRepository:
    """Single deterministic owner for non-Recovery options paper lifecycle."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime
        self.actions = ActionRepository(runtime)

    def process(
        self,
        *,
        enabled_lanes: Iterable[str],
        sleeve_capital: float | None,
        daily_loss_halt_pct: float | None,
        max_open_positions: int | None,
        decision_inbox_enabled: bool,
        now: datetime | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Stage eligible tickets, then attempt deterministic fill/exit marks."""

        reference = _utc(now)
        lanes = tuple(sorted({str(lane).lower() for lane in enabled_lanes} & GENERIC_LANES))
        staged = (
            self.stage_current_ready(
                enabled_lanes=lanes,
                sleeve_capital=sleeve_capital,
                daily_loss_halt_pct=daily_loss_halt_pct,
                max_open_positions=max_open_positions,
                now=reference,
                limit=limit,
            )
            if lanes
            else []
        )
        managed = self.manage_orders(
            # Entry switches only control staging.  Every existing generic
            # position remains in lifecycle management until it is terminal.
            lanes=GENERIC_LANES,
            decision_inbox_enabled=decision_inbox_enabled,
            now=reference,
            limit=limit,
        )
        return {
            "status": "ok",
            "paper_only": True,
            "staged": staged,
            "managed": managed,
            "lane_count": len(lanes),
            "entry_staging": "enabled" if lanes else "disabled",
        }

    def stage_current_ready(
        self,
        *,
        enabled_lanes: Iterable[str],
        sleeve_capital: float | None,
        daily_loss_halt_pct: float | None,
        max_open_positions: int | None,
        now: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Stage only current immutable tickets after lane-level gates pass."""

        enabled = set(enabled_lanes)
        radar_gate = self._radar_gate(now) if "radar" in enabled else None
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT publication.id::text AS publication_id, publication.scope,
                       publication.published_at, item.payload
                FROM app.publication publication
                JOIN app.publication_content_item item ON item.publication_id = publication.id
                WHERE publication.status = 'published'
                  AND publication.published_at <= %s
                  AND (
                    (publication.scope = 'options-radar' AND item.model_name = 'option_radar_opportunity')
                    OR
                    (publication.scope = 'options-decision-system' AND item.model_name = 'options_decision_candidate')
                  )
                ORDER BY publication.published_at DESC NULLS LAST, item.rank
                LIMIT %s
                """,
                [now, max(1, min(int(limit), 100))],
            ).fetchall()
        result: list[dict[str, Any]] = []
        for source in rows:
            payload = dict(source["payload"] or {})
            ticket = dict(payload.get("ticket") or {})
            lane = str(ticket.get("lane") or ("qqq" if source["scope"] == "options-decision-system" else "radar")).lower()
            if lane not in enabled:
                continue
            decision_id = str(ticket.get("decision_id") or payload.get("decision_id") or "")
            version = _integer(ticket.get("ticket_version"))
            if not decision_id or version is None:
                continue
            if str(ticket.get("state") or "").upper() != "READY" or list(ticket.get("blockers") or []):
                continue
            expires = _timestamp(ticket.get("expires_at") or (ticket.get("entry") or {}).get("valid_until"))
            execution_ready_at = _timestamp(ticket.get("execution_ready_at"))
            if execution_ready_at is None or execution_ready_at > now:
                result.append({"decision_id": decision_id, "lane": lane, "status": "skipped", "reason": "ticket_not_yet_execution_ready"})
                continue
            if expires is None or expires <= now:
                result.append({"decision_id": decision_id, "lane": lane, "status": "skipped", "reason": "ticket_expired"})
                continue
            if lane == "radar" and radar_gate is not None and radar_gate.get("status") != "READY_FOR_REVIEW":
                result.append({
                    "decision_id": decision_id, "lane": lane, "status": "skipped",
                    "reason": "radar_independent_episode_gate", "gaps": list(radar_gate.get("gaps") or []),
                })
                continue
            risk = dict(ticket.get("risk") or {})
            quantity = _integer(risk.get("recommended_quantity")) or 0
            entry = dict(ticket.get("entry") or {})
            limit_price = _number(entry.get("minimum_credit") if is_credit_structure(str(ticket.get("structure") or "")) else entry.get("limit_price"))
            if quantity <= 0 or limit_price is None or limit_price <= 0:
                result.append({"decision_id": decision_id, "lane": lane, "status": "skipped", "reason": "ticket_quantity_or_limit_missing"})
                continue
            entry_window = str(ticket.get("execution_ready_at") or ticket.get("expires_at"))
            key = f"{lane}:{decision_id}:v{version}:{entry_window}"
            try:
                staged = self.actions.stage_option_paper_entry(
                    decision_id=_uuid(decision_id),
                    idempotency_key=key,
                    ticket_version=version,
                    quantity=quantity,
                    limit_price=limit_price,
                    current_options_risk_sleeve_capital=sleeve_capital,
                    daily_loss_halt_pct=daily_loss_halt_pct,
                    max_open_positions=max_open_positions,
                )
            except ValueError as exc:
                result.append({"decision_id": decision_id, "lane": lane, "status": "rejected", "reason": str(exc)})
            else:
                result.append(staged)
        return result

    def manage_orders(
        self,
        *,
        lanes: Iterable[str],
        decision_inbox_enabled: bool,
        now: datetime | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        normalized = [lane for lane in lanes if lane in GENERIC_LANES]
        if not normalized:
            return []
        reference = _utc(now)
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT id::text
                FROM app.paper_order
                WHERE lane = ANY(%s::text[])
                  AND event_id IS NULL
                  AND status NOT IN ('exited', 'invalidated', 'unfilled', 'rejected', 'unmeasurable')
                ORDER BY created_at, id
                LIMIT %s
                """,
                [normalized, max(1, min(int(limit), 100))],
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            update = self._manage_one(str(row["id"]), reference)
            if update is None:
                continue
            results.append(update)
            event_status = str(update.get("event_status") or "")
            if decision_inbox_enabled and event_status in {"entered", "exited", "invalidated"}:
                DecisionInboxRepository(self.runtime).record_paper_lifecycle(
                    str(update["paper_order_id"]), status=event_status,
                    payload={"reason": update.get("reason")},
                )
        return results

    def _radar_gate(self, now: datetime) -> dict[str, Any]:
        return OpportunityScorecardRepository(self.runtime).scorecard(
            lane="radar", window_days=120, as_of=now,
        )

    def _manage_one(self, paper_order_id: str, now: datetime) -> dict[str, Any] | None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            acquire_shared_sleeve_lock(connection)
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"paper-order:generic:{paper_order_id}"],
            )
            order = connection.execute(
                """
                SELECT paper.id::text, paper.decision_id::text, paper.instrument_id,
                       paper.lane, paper.status, paper.quantity, paper.limit_price,
                       paper.actual_fill_price, paper.filled_at, paper.submitted_at,
                       paper.filled_quantity, paper.exited_quantity, paper.fees,
                       paper.ticket_version, paper.ticket_snapshot, paper.structure,
                       paper.created_at, instrument.symbol
                FROM app.paper_order paper
                JOIN catalog.instrument instrument ON instrument.id = paper.instrument_id
                WHERE paper.id = %s::uuid AND paper.event_id IS NULL
                FOR UPDATE OF paper
                """,
                [paper_order_id],
            ).fetchone()
            if order is None:
                return None
            item = dict(order)
            if str(item["status"]) in TERMINAL_STATUSES:
                return None
            ticket = dict(item.get("ticket_snapshot") or {})
            legs = [dict(row) for row in connection.execute(
                """
                SELECT leg.contract_id, leg.option_type, leg.side, leg.strike::double precision AS strike,
                       leg.bid, leg.ask, leg.bid_size, leg.ask_size, leg.quote_time,
                       leg.open_interest, leg.volume
                FROM app.paper_order_leg leg
                WHERE leg.paper_order_id = %s::uuid
                ORDER BY leg.leg_index
                """,
                [paper_order_id],
            ).fetchall()]
            if not legs:
                return self._terminal(connection, item, status="rejected", reason="immutable_ticket_legs_missing", now=now)
            status = str(item["status"])
            if status == "staged":
                current, reason = self._current_ticket(connection, item, ticket, as_of=now)
                if current is None:
                    return self._terminal(connection, item, status="rejected", reason=reason, now=now)
                expires = _timestamp(ticket.get("expires_at") or (ticket.get("entry") or {}).get("valid_until"))
                if expires is None:
                    return self._terminal(connection, item, status="rejected", reason="ticket_expiry_missing", now=now)
                thesis_reason = _thesis_blocker(connection, int(item["instrument_id"]), now)
                if thesis_reason:
                    return self._terminal(connection, item, status="rejected", reason=thesis_reason, now=now)
                if expires <= now:
                    return self._terminal(connection, item, status="unfilled", reason="ticket_expired_before_fill", now=now)
                quoted = latest_option_legs(connection, ticket_legs=legs, as_of=now)
                current_execution = execution_policy(
                    quoted,
                    structure=str(item.get("structure") or ticket.get("structure") or ""),
                    entry_price=_number(item.get("limit_price")),
                    market_session="regular" if is_market_open(now) else "closed",
                    evaluated_at=now,
                )
                if current_execution["blockers"]:
                    connection.execute(
                        "UPDATE app.paper_order SET submitted_at = coalesce(submitted_at, %s), updated_at = %s WHERE id = %s::uuid",
                        [now, now, paper_order_id],
                    )
                    return {"paper_order_id": paper_order_id, "status": "submitted", "reason": "fresh_quote_not_fillable", "blockers": current_execution["blockers"]}
                fill_quantity = _available_quantity(quoted, phase="entry", requested=_quantity(item["quantity"]))
                fill_price = package_price(quoted, phase="entry")
                credit = is_credit_structure(str(item.get("structure") or ticket.get("structure") or ""))
                limit_price = _number(item.get("limit_price"))
                can_fill = bool(
                    fill_quantity > 0 and fill_price is not None and limit_price is not None
                    and (fill_price >= limit_price if credit else fill_price <= limit_price)
                )
                connection.execute(
                    "UPDATE app.paper_order SET submitted_at = coalesce(submitted_at, %s), updated_at = %s WHERE id = %s::uuid",
                    [now, now, paper_order_id],
                )
                if not can_fill:
                    return {"paper_order_id": paper_order_id, "status": "submitted", "reason": "limit_not_reached"}
                fees = _fees(len(quoted), fill_quantity)
                slippage = _entry_slippage(quoted, fill_price, credit)
                connection.execute(
                    """
                    UPDATE app.paper_order
                    SET status = 'entered', actual_fill_price = %s, filled_at = %s,
                        filled_quantity = %s, fees = coalesce(fees, 0) + %s,
                        entry_slippage = %s, updated_at = %s, unfilled_reason = NULL
                    WHERE id = %s::uuid
                    """,
                    [fill_price, now, fill_quantity, fees, slippage, now, paper_order_id],
                )
                _journal(
                    connection, item, action="paper_entry", quantity=fill_quantity,
                    price=fill_price, key=f"generic:{paper_order_id}:entry:{now.isoformat()}",
                    details={"lane": item["lane"], "paper_order_id": paper_order_id, "slippage": slippage, "fees": fees},
                )
                return {
                    "paper_order_id": paper_order_id, "status": "filled",
                    "event_status": "entered", "filled_quantity": fill_quantity,
                    "fill_price": fill_price, "fees": fees,
                }
            # A filled position must retain a safe exit path even after its
            # publication is superseded or the global entry kill switch flips.
            # Those conditions block new entries and force an exit; they must
            # never relabel a live paper position as an unfilled ticket.
            _current, current_reason = self._current_ticket(connection, item, ticket, as_of=now)
            thesis_reason = _thesis_blocker(connection, int(item["instrument_id"]), now)
            return self._manage_open(
                connection, item, ticket, legs, now,
                forced_exit_reason=thesis_reason or (current_reason if _current is None else None),
            )

    def _manage_open(
        self,
        connection: Any,
        order: dict[str, Any],
        ticket: dict[str, Any],
        legs: list[dict[str, Any]],
        now: datetime,
        forced_exit_reason: str | None = None,
    ) -> dict[str, Any]:
        filled_quantity = _quantity(order.get("filled_quantity") or order.get("quantity"))
        exited_quantity = _quantity(order.get("exited_quantity"))
        remaining = max(0, filled_quantity - exited_quantity)
        if remaining <= 0:
            return self._terminal(connection, order, status="exited", reason="no_remaining_quantity", now=now)
        quoted = latest_option_legs(connection, ticket_legs=legs, as_of=now)
        structure = str(order.get("structure") or ticket.get("structure") or "")
        execution = execution_policy(
            quoted,
            structure=structure,
            entry_price=_number(order.get("actual_fill_price")),
            market_session="regular" if is_market_open(now) else "closed",
            evaluated_at=now,
        )
        exits = dict(ticket.get("exits") or {})
        credit = is_credit_structure(structure)
        exit_price = package_price(quoted, phase="exit")
        policy_blockers = list(execution.get("blockers") or [])
        trigger_reason = _exit_reason(
            ticket=ticket, exits=exits, credit=credit, entry_price=_number(order.get("actual_fill_price")),
            exit_price=exit_price, execution_blockers=[], now=now,
        )
        reason = forced_exit_reason or trigger_reason
        if policy_blockers:
            pending_reason = reason or "liquidity_exit"
            connection.execute(
                "UPDATE app.paper_order SET unfilled_reason = %s, updated_at = %s WHERE id = %s::uuid",
                [f"{pending_reason}: complete_fresh_executable_exit_quote_required", now, order["id"]],
            )
            return {
                "paper_order_id": str(order["id"]),
                "status": "filled",
                "reason": f"{pending_reason}_pending_executable_quote",
                "blockers": policy_blockers,
            }
        if reason is None:
            return {"paper_order_id": str(order["id"]), "status": "filled", "reason": "exit_not_triggered"}
        if exit_price is None or not quoted:
            connection.execute(
                "UPDATE app.paper_order SET unfilled_reason = %s, updated_at = %s WHERE id = %s::uuid",
                [f"{reason}: fresh_executable_exit_quote_required", now, order["id"]],
            )
            return {"paper_order_id": str(order["id"]), "status": "filled", "reason": f"{reason}_pending_liquidity"}
        exit_quantity = min(remaining, _available_quantity(quoted, phase="exit", requested=remaining))
        if exit_quantity <= 0:
            connection.execute(
                "UPDATE app.paper_order SET unfilled_reason = %s, updated_at = %s WHERE id = %s::uuid",
                [f"{reason}: displayed_size_unavailable", now, order["id"]],
            )
            return {"paper_order_id": str(order["id"]), "status": "filled", "reason": f"{reason}_pending_size"}
        new_exited = exited_quantity + exit_quantity
        terminal = new_exited >= filled_quantity
        status = "exited" if terminal else "partial_exited"
        fees = _fees(len(quoted), exit_quantity)
        slippage = _exit_slippage(quoted, exit_price, credit)
        entry_price = _number(order.get("actual_fill_price")) or 0.0
        net_pnl = _net_pnl(
            credit=credit, entry_price=entry_price, exit_price=exit_price,
            quantity=exit_quantity, leg_count=len(quoted),
        )
        connection.execute(
            """
            UPDATE app.paper_order
            SET status = %s, exited_quantity = %s, exit_price = %s, exit_at = %s,
                fees = coalesce(fees, 0) + %s, exit_slippage = %s,
                updated_at = %s, unfilled_reason = NULL
            WHERE id = %s::uuid
            """,
            [status, new_exited, exit_price, now, fees, slippage, now, order["id"]],
        )
        _journal(
            connection, order, action=f"paper_exit:{reason}", quantity=exit_quantity,
            price=exit_price, key=f"generic:{order['id']}:exit:{now.isoformat()}:{reason}",
            details={
                "lane": order["lane"], "paper_order_id": str(order["id"]),
                "net_pnl": round(net_pnl, 2), "slippage": slippage, "fees": fees,
            },
        )
        return {
            "paper_order_id": str(order["id"]),
            "status": "closed" if terminal else "filled",
            "event_status": status if terminal else None,
            "reason": reason, "exit_quantity": exit_quantity,
            "exit_price": exit_price, "net_pnl": round(net_pnl, 2),
        }

    def _current_ticket(
        self,
        connection: Any,
        order: dict[str, Any],
        ticket: dict[str, Any],
        *,
        as_of: datetime,
    ) -> tuple[dict[str, Any] | None, str]:
        decision_id = str(order.get("decision_id") or "")
        version = _integer(order.get("ticket_version"))
        lane = str(order.get("lane") or "")
        if not decision_id or version is None:
            return None, "paper_order_ticket_identity_missing"
        scope = "options-decision-system" if lane == "qqq" else "options-radar"
        model = "options_decision_candidate" if lane == "qqq" else "option_radar_opportunity"
        row = connection.execute(
            """
            SELECT publication.id::text AS publication_id, item.payload
            FROM app.publication publication
            JOIN app.publication_content_item item ON item.publication_id = publication.id
            WHERE publication.scope = %s AND publication.status = 'published'
              AND publication.published_at <= %s
              AND item.model_name = %s AND item.payload->>'decision_id' = %s
            LIMIT 1
            """,
            [scope, as_of, model, decision_id],
        ).fetchone()
        if row is None:
            return None, "ticket_no_longer_in_current_publication"
        current = dict(row["payload"] or {})
        current_ticket = dict(current.get("ticket") or {})
        if _integer(current_ticket.get("ticket_version")) != version:
            return None, "ticket_version_superseded"
        if str(current_ticket.get("state") or "").upper() != "READY" or list(current_ticket.get("blockers") or []):
            return None, "ticket_no_longer_ready"
        execution_ready_at = _timestamp(current_ticket.get("execution_ready_at"))
        expires_at = _timestamp(current_ticket.get("expires_at") or (current_ticket.get("entry") or {}).get("valid_until"))
        if execution_ready_at is None or execution_ready_at > as_of:
            return None, "ticket_not_yet_execution_ready"
        if expires_at is None or expires_at <= as_of:
            return None, "ticket_expired"
        lineage = dict(ticket.get("publication_lineage") or {})
        expected_publication = str(lineage.get("publication_id") or "")
        if not expected_publication or expected_publication != str(row["publication_id"]):
            return None, "ticket_publication_superseded"
        return current_ticket, ""

    def _terminal(
        self,
        connection: Any,
        order: dict[str, Any],
        *,
        status: str,
        reason: str,
        now: datetime,
    ) -> dict[str, Any]:
        connection.execute(
            "UPDATE app.paper_order SET status = %s, unfilled_reason = %s, updated_at = %s WHERE id = %s::uuid",
            [status, reason, now, order["id"]],
        )
        _journal(
            connection, order, action=f"paper_{status}", quantity=0,
            price=None, key=f"generic:{order['id']}:{status}:{reason}",
            details={"lane": order["lane"], "paper_order_id": str(order["id"]), "reason": reason},
        )
        return {"paper_order_id": str(order["id"]), "status": "closed" if status in {"exited", "invalidated"} else status, "reason": reason, "event_status": status if status in {"exited", "invalidated"} else None}


def _available_quantity(legs: list[dict[str, Any]], *, phase: str, requested: float) -> float:
    sizes: list[int] = []
    for leg in legs:
        short = str(leg.get("side") or "").lower() in {"short", "sell"}
        size = leg.get("bid_size") if (phase == "entry" and short) or (phase == "exit" and not short) else leg.get("ask_size")
        amount = _integer(size)
        if amount is None or amount <= 0:
            return 0
        sizes.append(amount)
    return float(min(floor(requested), min(sizes))) if sizes else 0


def _exit_reason(
    *,
    ticket: dict[str, Any],
    exits: dict[str, Any],
    credit: bool,
    entry_price: float | None,
    exit_price: float | None,
    execution_blockers: list[str],
    now: datetime,
) -> str | None:
    if _timestamp(ticket.get("expires_at")) and _timestamp(ticket.get("expires_at")) <= now:
        return "ticket_expired"
    expiration = _date(ticket.get("expiration"))
    time_exit_dte = _integer(exits.get("time_exit_dte"))
    if expiration is not None and time_exit_dte is not None and (expiration - now.date()).days <= time_exit_dte:
        return "time_exit"
    if execution_blockers:
        return "liquidity_exit"
    if entry_price is None or exit_price is None:
        return None
    profit = _number(exits.get("profit_price"))
    loss = _number(exits.get("loss_price"))
    if credit:
        if profit is not None and exit_price <= profit:
            return "profit_target"
        if loss is not None and exit_price >= loss:
            return "stop_loss"
    else:
        if profit is not None and exit_price >= profit:
            return "profit_target"
        if loss is not None and exit_price <= loss:
            return "stop_loss"
    return None


def _thesis_blocker(connection: Any, instrument_id: int, now: datetime) -> str | None:
    row = connection.execute(
        """
        SELECT thesis, updated_at
        FROM app.thesis
        WHERE instrument_id = %s AND status = 'current' AND updated_at <= %s
        ORDER BY revision DESC LIMIT 1
        """,
        [instrument_id, now],
    ).fetchone()
    if row is None:
        return None
    thesis = dict(row["thesis"] or {})
    lifecycle = str(thesis.get("lifecycle_status") or thesis.get("status") or "active").lower()
    if lifecycle in {"invalidated", "closed", "expired"}:
        return "thesis_invalidated_or_closed"
    horizon = _date(thesis.get("horizon_date"))
    if horizon is not None and horizon < now.date():
        return "thesis_horizon_expired"
    return None


def _journal(
    connection: Any,
    order: dict[str, Any],
    *,
    action: str,
    quantity: float,
    price: float | None,
    key: str,
    details: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO app.trade_journal (decision_id, instrument_id, action, quantity, price, rationale, details)
        SELECT %s::uuid, %s, %s, %s, %s, %s, %s
        WHERE NOT EXISTS (
          SELECT 1 FROM app.trade_journal WHERE details->>'idempotency_key' = %s
        )
        """,
        [
            order.get("decision_id"), order["instrument_id"], action, quantity, price,
            "deterministic_options_paper_execution", Jsonb({"idempotency_key": key, **details}), key,
        ],
    )


def _fees(leg_count: int, quantity: float) -> float:
    return round(FEE_PER_CONTRACT_LEG * max(1, leg_count) * max(0, quantity), 2)


def _entry_slippage(legs: list[dict[str, Any]], price: float, credit: bool) -> float | None:
    midpoint = _midpoint_package(legs)
    if midpoint is None:
        return None
    return round(max(midpoint - price, 0) if credit else max(price - midpoint, 0), 6)


def _exit_slippage(legs: list[dict[str, Any]], price: float, credit: bool) -> float | None:
    midpoint = _midpoint_package(legs)
    if midpoint is None:
        return None
    return round(max(price - midpoint, 0) if credit else max(midpoint - price, 0), 6)


def _midpoint_package(legs: list[dict[str, Any]]) -> float | None:
    signed = 0.0
    for leg in legs:
        bid, ask = _number(leg.get("bid")), _number(leg.get("ask"))
        if bid is None or ask is None or bid <= 0 or ask < bid:
            return None
        midpoint = (bid + ask) / 2
        signed += -midpoint if str(leg.get("side") or "").lower() in {"short", "sell"} else midpoint
    return abs(signed)


def _net_pnl(*, credit: bool, entry_price: float, exit_price: float, quantity: float, leg_count: int) -> float:
    gross = (entry_price - exit_price) if credit else (exit_price - entry_price)
    return gross * 100 * quantity - _fees(leg_count, quantity) * 2


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    return current.astimezone(UTC) if current.tzinfo is not None else current.replace(tzinfo=UTC)


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return _utc(parsed)


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        result = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return result


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _quantity(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _uuid(value: str):
    from uuid import UUID

    return UUID(value)


available_quantity = _available_quantity
exit_reason = _exit_reason
net_pnl = _net_pnl
