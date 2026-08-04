"""Paper-order lifecycle persistence for the recovery execution owner."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.core.options_event_tape import (
    event_strip_expiration_after_fill,
    trading_sessions_between,
)
from investment_panel.core.options_recovery import (
    FEE_PER_CONTRACT_LEG,
    ExecutableLeg,
    QuoteCapture,
    evaluate_lifecycle,
)
from investment_panel.database.options_recovery_execution_support import (
    invalidated as _invalidated,
    journal as _journal,
    leg_expiry_days,
    order_status as _order_status,
)
from investment_panel.database.runtime import JOB_PROFILE


class RecoveryOrderLifecycle:
    """Mixin that applies fill-relative lifecycle rules to local paper orders."""

    def _manage_order(self, order: dict[str, Any], now: datetime) -> dict[str, Any]:
        captures = self._order_captures(order, now)
        ticket = dict(order.get("ticket_snapshot") or {})
        result = evaluate_lifecycle(
            published_at=order["created_at"], quantity=int(order["quantity"]), captures=captures,
            entry_limit=(ticket.get("entry") or {}).get("limit_price"),
        )
        leg_count = max(1, len(ticket.get("legs") or ()))
        with self.runtime.transaction(JOB_PROFILE) as connection:
            if result.entry_fill_at and order.get("filled_at") is None:
                connection.execute("UPDATE app.paper_order SET status = 'entered', actual_fill_price = %s, filled_at = %s WHERE id = %s", [result.entry_fill_price, result.entry_fill_at, order["id"]])
                lifecycle_expires_at = event_strip_expiration_after_fill(result.entry_fill_at)
                # A staged/entered paper order keeps its fixed event tape
                # alive through the actual fill-relative ten-session window.
                # Without this extension, a late fill could be left observing
                # after the event-age collection horizon closed.
                connection.execute(
                    """
                    UPDATE app.option_history_policy
                    SET expires_at = CASE
                            WHEN expires_at IS NULL OR expires_at < %s THEN %s
                            ELSE expires_at
                        END,
                        reason = 'paper position lifecycle window',
                        updated_at = now(), lock_version = lock_version + 1
                    WHERE profile = 'event_strip' AND event_id = %s
                      AND requested_state = 'on'
                    """,
                    [lifecycle_expires_at, lifecycle_expires_at, order["event_id"]],
                )
                _journal(connection, order, action="paper_entry", quantity=int(order["quantity"]), price=result.entry_fill_price,
                         key=f"recovery:{order['id']}:entry", details={"paper_order_id": str(order["id"])})
            for fill in result.exit_fills:
                net_pnl = None
                if result.entry_fill_price is not None:
                    net_pnl = (
                        (fill.executable_price - result.entry_fill_price) * 100.0 * fill.quantity
                        - FEE_PER_CONTRACT_LEG * leg_count * 2 * fill.quantity
                    )
                _journal(connection, order, action=f"paper_exit:{fill.reason}", quantity=fill.quantity,
                         price=fill.executable_price, key=f"recovery:{order['id']}:exit:{fill.observed_at.isoformat()}:{fill.reason}",
                         details={
                             "paper_order_id": str(order["id"]),
                             "session_number": fill.session_number,
                             "net_pnl": round(net_pnl, 2) if net_pnl is not None else None,
                         })
            status = _order_status(result, int(order["quantity"]))
            if status != order["status"]:
                connection.execute("UPDATE app.paper_order SET status = %s, entry_capture_count = %s WHERE id = %s", [status, result.entry_capture_count, order["id"]])
                connection.execute("UPDATE analysis.option_event_signal SET status = %s, updated_at = now() WHERE id = %s", [status, order["signal_id"]])
        return {"paper_order_id": str(order["id"]), "status": status, "classification": result.classification,
                "entry_capture_count": result.entry_capture_count, "exit_count": len(result.exit_fills)}

    def _order_captures(self, order: dict[str, Any], now: datetime) -> list[QuoteCapture]:
        with self.runtime.read(JOB_PROFILE) as connection:
            captures = connection.execute(
                """
                SELECT capture.id, capture.scheduled_at, capture.finished_at, event.reference_price, event.event_low,
                       spot.price AS spot_price
                FROM analysis.option_event_capture capture
                JOIN analysis.option_event event ON event.id = capture.event_id
                LEFT JOIN LATERAL (
                  SELECT price FROM analysis.option_event_spot
                  WHERE event_id = event.id AND available_at <= capture.finished_at
                  ORDER BY available_at DESC LIMIT 1
                ) spot ON true
                WHERE capture.event_id = %s AND capture.status IN ('complete', 'partial')
                  AND capture.finished_at > %s AND capture.finished_at <= %s
                ORDER BY capture.scheduled_at
                """,
                [order["event_id"], order["created_at"], now],
            ).fetchall()
            legs = connection.execute(
                """
                SELECT leg.contract_id, leg.option_type, leg.side, leg.strike, contract.expiration
                FROM app.paper_order_leg leg
                JOIN catalog.option_contract contract ON contract.id = leg.contract_id
                WHERE leg.paper_order_id = %s ORDER BY leg.leg_index
                """,
                [order["id"]],
            ).fetchall()
            quotes = connection.execute(
                """
                SELECT capture.id AS capture_id, quote.contract_id, quote.bid, quote.ask, quote.bid_size, quote.ask_size,
                       quote.available_at
                FROM analysis.option_event_capture capture
                JOIN raw.option_quote quote ON quote.snapshot_id = capture.snapshot_id
                  AND quote.capture_generation_id = capture.capture_generation_id
                  AND quote.available_at <= capture.finished_at
                WHERE capture.event_id = %s AND capture.status IN ('complete', 'partial')
                  AND capture.finished_at > %s AND capture.finished_at <= %s
                """,
                [order["event_id"], order["created_at"], now],
            ).fetchall()
        by_capture: dict[str, dict[int, dict[str, Any]]] = {}
        for quote in quotes:
            by_capture.setdefault(str(quote["capture_id"]), {})[int(quote["contract_id"])] = dict(quote)
        result: list[QuoteCapture] = []
        for capture in captures:
            package = by_capture.get(str(capture["id"]), {})
            rows = [package.get(int(leg["contract_id"])) for leg in legs]
            continuity = all(row is not None for row in rows)
            executable = tuple(
                ExecutableLeg(str(leg["contract_id"]), str(leg["side"]), row.get("bid"), row.get("ask"), row.get("bid_size"), row.get("ask_size"))
                for leg, row in zip(legs, rows) if row is not None
            )
            price = capture.get("spot_price")
            invalidated = _invalidated(str(order["strategy_key"]), price, capture["event_low"], capture["reference_price"])
            result.append(QuoteCapture(
                observed_at=capture["finished_at"], legs=executable,
                session_number=trading_sessions_between(order["started_at"], capture["finished_at"]),
                dte=min((leg_expiry_days(leg, capture["finished_at"]) for leg in legs), default=None),
                invalidated=invalidated, continuity_ok=continuity,
                reason="same_contract_continuity_missing" if not continuity else None,
            ))
        return result

    def _set_signal_status(self, signal_id: str, status: str, diagnostics: dict[str, Any]) -> None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                "UPDATE analysis.option_event_signal SET status = %s, gate_result = gate_result || %s, updated_at = now() WHERE id = %s",
                [status, Jsonb(diagnostics), signal_id],
            )
