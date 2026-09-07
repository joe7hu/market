"""Conservative execution and lifecycle for Radar and QQQ paper tickets.

This owner never calls a brokerage API.  It stages only immutable, current
READY tickets, then fills from a later point-in-time option quote package at
the pessimistic side of the market.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from math import floor, isfinite
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from investment_panel.core.decision import MARKET_TZ, is_market_open, is_us_market_day, market_session_bounds
from investment_panel.core.option_trade_ticket import execution_policy, exit_reason
from investment_panel.core.options_recovery import FEE_PER_CONTRACT_LEG
from investment_panel.database.actions import ActionRepository
from investment_panel.database.analysis import current_option_publication_answers, current_option_publication_rows
from investment_panel.database.confirmed_daily_prices import confirmed_daily_bars
from investment_panel.database.decision_inbox import DecisionInboxRepository
from investment_panel.database.opportunity_scorecards import OpportunityScorecardRepository
from investment_panel.database.options_paper_ledger import acquire_shared_sleeve_lock, paper_fill_totals
from investment_panel.database.options_paper_quotes import (
    is_credit_structure,
    latest_option_legs,
    package_price,
)
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


GENERIC_LANES = frozenset({"radar", "qqq"})
TERMINAL_STATUSES = frozenset({"exited", "invalidated", "unfilled", "rejected", "unmeasurable"})
PAPER_MARK_KEY = "observed_liquidation_v1"
ENTRY_CANCELLATION_KEY = "entry_remainder_cancellation_v1"


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
        experiment_publication_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Stage only current immutable tickets after lane-level gates pass."""

        enabled = set(enabled_lanes)
        radar_gate = None
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = (
                [dict(row) for row in connection.execute(
                    "SELECT publication.id::text AS publication_id, publication.scope, publication.published_at, item.payload, item.rank "
                    "FROM app.publication publication JOIN app.publication_content_item item ON item.publication_id = publication.id "
                    "WHERE publication.id = %s::uuid AND publication.status = 'published' AND item.model_name = 'option_paper_experiment'",
                    [experiment_publication_id],
                ).fetchall()]
                if experiment_publication_id else current_option_publication_answers(connection, cutoff=now)
            )
            rows.sort(
                key=lambda row: (
                    row["published_at"] or datetime.min.replace(tzinfo=UTC),
                    -int(row.get("rank") or 0),
                ),
                reverse=True,
            )
            if experiment_publication_id:
                rows = [row for row in rows if (
                    str(((row["payload"] or {}).get("ticket") or {}).get("state") or "").upper() == "READY"
                    and not ((row["payload"] or {}).get("ticket") or {}).get("blockers")
                )]
            rows = rows[:max(1, min(int(limit), 100))]
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
            if lane == "radar" and radar_gate is None and not experiment_publication_id:
                radar_gate = self._radar_gate(now)
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
            if experiment_publication_id:
                key = f"experiment:{experiment_publication_id}:{key}"
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
                    **({"experiment_publication_id": _uuid(experiment_publication_id)} if experiment_publication_id else {}),
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
                       paper.filled_quantity, paper.exited_quantity, paper.fees, paper.entry_fees,
                       paper.fill_evidence_at, paper.execution_quote, paper.contract_multiplier,
                       paper.ticket_version, paper.ticket_snapshot, paper.structure, paper.policy_result,
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
                       leg.open_interest, leg.volume, contract.multiplier, contract.expiration
                FROM app.paper_order_leg leg
                JOIN catalog.option_contract contract ON contract.id = leg.contract_id
                WHERE leg.paper_order_id = %s::uuid
                ORDER BY leg.leg_index
                """,
                [paper_order_id],
            ).fetchall()]
            status = str(item["status"])
            expires = _timestamp(ticket.get("expires_at") or (ticket.get("entry") or {}).get("valid_until"))
            thesis_reason = _thesis_blocker(connection, int(item["instrument_id"]), now)
            missing_legs = "immutable_ticket_legs_missing" if not legs else None
            entry_pending = status in {"staged", "open"} and _quantity(item.get("filled_quantity")) < _quantity(item.get("quantity"))
            quoted = None
            if entry_pending:
                current, reason = self._current_ticket(connection, item, ticket, as_of=now)
                entry_blocker = missing_legs or (reason if current is None else None) or thesis_reason
                if not entry_blocker:
                    entry_blocker = "ticket_expiry_missing" if expires is None else "ticket_expired_before_fill" if expires <= now else None
                if not entry_blocker and _quantity(item.get("filled_quantity")) > 0:
                    fills = paper_fill_totals(connection, item, as_of=now)
                    quoted = latest_option_legs(connection, ticket_legs=legs, as_of=now, **_experiment_quote_scope(ticket))
                    if (not fills or fills.get("fill_multipliers_verified") is not True
                        or _number(fills.get("entry_quantity")) != _quantity(item.get("filled_quantity"))):
                        entry_blocker = "paper_entry_multiplier_unverified"
                    elif quoted and any(_number(leg.get("multiplier")) != _number(item.get("contract_multiplier")) for leg in quoted):
                        entry_blocker = "paper_entry_multiplier_conflict"
                if entry_blocker:
                    if _quantity(item.get("filled_quantity")) <= 0:
                        return self._terminal(connection, item, status="unfilled" if entry_blocker == "ticket_expired_before_fill" else "rejected", reason=entry_blocker, now=now)
                    # Cancel only the remainder when entry authority ends. The
                    # filled quantity still needs its own holding-policy check.
                    cancellation = {"status": "cancelled", "paper_order_id": str(item["id"]),
                                    "cancelled_at": now.isoformat(), "reason": entry_blocker,
                                    "requested_quantity": _quantity(item.get("quantity")),
                                    "filled_quantity": _quantity(item.get("filled_quantity")),
                                    "cancelled_quantity": _quantity(item.get("quantity")) - _quantity(item.get("filled_quantity"))}
                    connection.execute(
                        "UPDATE app.paper_order SET status = 'entered', unfilled_reason = %s, "
                        "execution_quote = coalesce(execution_quote, '{}'::jsonb) || %s, updated_at = %s WHERE id = %s::uuid",
                        [f"{entry_blocker}: unfilled_remainder_cancelled", Jsonb({ENTRY_CANCELLATION_KEY: cancellation}), now, paper_order_id],
                    )
                    item["status"] = "entered"
                    item["execution_quote"] = {**dict(item.get("execution_quote") or {}), ENTRY_CANCELLATION_KEY: cancellation}
                    entry_pending = False
            if entry_pending:
                if quoted is None:
                    quoted = latest_option_legs(connection, ticket_legs=legs, as_of=now, **_experiment_quote_scope(ticket))
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
                fill_quantity = _available_quantity(quoted, phase="entry", requested=max(0.0, _quantity(item["quantity"]) - _quantity(item.get("filled_quantity"))))
                fill_price = package_price(quoted, phase="entry")
                new_filled = _quantity(item.get("filled_quantity")) + fill_quantity
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
                multiplier = _number(quoted[0].get("multiplier")) if quoted else None
                if multiplier is None or not isfinite(multiplier) or multiplier <= 0 or any(_number(leg.get("multiplier")) != multiplier for leg in quoted):
                    return {"paper_order_id": paper_order_id, "status": "submitted", "reason": "contract_multiplier_missing"}
                quote_payload = {
                    **dict(item.get("execution_quote") or {}),
                    "mid": _midpoint_package(quoted),
                    "spread": sum(float(leg["ask"]) - float(leg["bid"]) for leg in quoted),
                    "leg_count": len(quoted),
                    **_experiment_quote_evidence(ticket, quoted),
                }
                connection.execute(
                    """
                    UPDATE app.paper_order
                    SET status = %s, actual_fill_price = coalesce(actual_fill_price, %s), filled_at = coalesce(filled_at, %s),
                        fill_evidence_at = clock_timestamp(), execution_quote = %s,
                        contract_multiplier = %s, filled_quantity = coalesce(filled_quantity, 0) + %s,
                        fees = coalesce(fees, 0) + %s, entry_fees = coalesce(entry_fees, 0) + %s,
                        entry_slippage = %s, updated_at = %s, unfilled_reason = NULL
                    WHERE id = %s::uuid
                    """,
                    ["entered" if new_filled >= _quantity(item["quantity"]) else "open", fill_price, now, Jsonb(quote_payload), multiplier, fill_quantity, fees, fees, slippage, now, paper_order_id],
                )
                _journal(
                    connection, item, action="paper_entry", quantity=fill_quantity,
                    price=fill_price, key=f"generic:{paper_order_id}:entry:{now.isoformat()}",
                    details={"lane": item["lane"], "paper_order_id": paper_order_id, "slippage": slippage, "fees": fees,
                             "contract_multiplier": multiplier,
                             **_experiment_quote_evidence(ticket, quoted)},
                )
                _record_liquidation_mark(connection, {
                    **item, "filled_quantity": new_filled, "contract_multiplier": multiplier,
                    "filled_at": item.get("filled_at") or now,
                    "fees": float(item.get("fees") or 0) + fees,
                }, quoted, now=now, execution_blockers=[])
                self._record_phase4_fill(
                    connection, paper_order_id=paper_order_id, observed_at=now, status="entered" if new_filled >= _quantity(item["quantity"]) else "partial",
                )
                return {
                    "paper_order_id": paper_order_id, "status": "filled",
                    "event_status": "entered" if new_filled >= _quantity(item["quantity"]) else None, "filled_quantity": new_filled,
                    "fill_price": fill_price, "fees": fees,
                }
            # Publication refresh and entry expiry do not change the holding
            # policy. Real thesis/strategy invalidation still requests an exit.
            _current, current_reason = self._current_ticket(connection, item, ticket, as_of=now, for_entry=False)
            return self._manage_open(
                connection, item, ticket, legs, now,
                forced_exit_reason=missing_legs or thesis_reason or (current_reason if _current is None else None),
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
        structure = str(order.get("structure") or ticket.get("structure") or "")
        pending_settlement = None
        if structure == "cash_secured_put" and legs and any(
            leg.get("expiration") is not None and leg["expiration"] <= now.astimezone(MARKET_TZ).date() for leg in legs
        ):
            mark, pending_reason = _expiration_mark(connection, order, legs, now=now)
            strike = _number(legs[0].get("strike"))
            multiplier = _number(legs[0].get("multiplier"))
            if mark is not None and any(value is None or not isfinite(value) or value <= 0 for value in (strike, multiplier)):
                mark, pending_reason = None, "expiration_contract_terms_unverified"
            if mark is None:
                pending_settlement = {"status": "pending_settlement", "reason": pending_reason,
                                      "expiration": str(legs[0].get("expiration")), "checked_at": now.isoformat()}
                connection.execute(
                    "UPDATE app.paper_order SET unfilled_reason = %s, execution_quote = coalesce(execution_quote, '{}'::jsonb) || %s, "
                    "updated_at = %s WHERE id = %s::uuid",
                    [f"expiration_settlement_pending: {pending_reason}", Jsonb({"assignment": pending_settlement}), now, order["id"]],
                )
                # Before the close, executable risk exits still use the normal
                # holding policy. After close only an expiration mark can settle.
                if pending_reason != "expiration_session_not_closed" or not is_market_open(now):
                    return {"paper_order_id": str(order["id"]), "status": "filled", "reason": "expiration_settlement_pending",
                            "settlement_pending_reason": pending_reason}
            else:
                underlying_price = mark["close"]
                intrinsic = max(strike - underlying_price, 0.0)
                assigned = intrinsic > 0
                settlement_fee = _fees(len(legs), remaining)
                accounting = _exit_accounting(
                    connection, order, now=now, credit=True, exit_price=intrinsic,
                    quantity=remaining, multiplier=multiplier, exit_fees=settlement_fee,
                )
                assignment = {
                    "status": "assigned" if assigned else "expired_unassigned", "strike": strike, "underlying_price": underlying_price,
                    "multiplier": multiplier, "contract_count": remaining,
                    "settlement_value": intrinsic * multiplier * remaining,
                    "settled_at": now.isoformat(), "settlement_fee": settlement_fee,
                    "assignment_fee": settlement_fee if assigned else 0, "expiration_mark": mark,
                    **accounting,
                }
                connection.execute(
                    """UPDATE app.paper_order
                       SET status = 'exited', exited_quantity = %s, exit_price = %s, exit_at = %s,
                           contract_multiplier = %s, fees = coalesce(fees, 0) + %s,
                           exit_fees = coalesce(exit_fees, 0) + %s,
                           execution_quote = coalesce(execution_quote, '{}'::jsonb) || %s,
                           unfilled_reason = %s, updated_at = %s
                       WHERE id = %s::uuid""",
                    [exited_quantity + remaining, intrinsic, now,
                     multiplier, settlement_fee, settlement_fee, Jsonb({"assignment": assignment}),
                     "assigned_at_expiration" if assigned else "expired_unassigned", now, order["id"]],
                )
                reason = "assignment" if assigned else "expiration_unassigned"
                _journal(
                    connection, order, action=f"paper_exit:{reason}", quantity=remaining,
                    price=intrinsic, key=f"generic:{order['id']}:exit:{now.isoformat()}:{reason}",
                    details={"lane": order["lane"], "paper_order_id": str(order["id"]), "fees": settlement_fee,
                             **accounting, "assignment": assignment},
                )
                self._record_phase4_fill(connection, paper_order_id=str(order["id"]), observed_at=now, status="exited")
                return {"paper_order_id": str(order["id"]), "status": "closed", "event_status": "exited", "reason": reason,
                        "assigned_strike": strike if assigned else None}
        quoted = latest_option_legs(connection, ticket_legs=legs, as_of=now, **_experiment_quote_scope(ticket))
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
        _record_liquidation_mark(connection, order, quoted, now=now, execution_blockers=policy_blockers)
        trigger_reason = exit_reason(
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
                "reason": "expiration_settlement_pending" if pending_settlement else f"{pending_reason}_pending_executable_quote",
                **({"settlement_pending_reason": pending_settlement["reason"]} if pending_settlement else {}),
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
        exit_multipliers = [_number(leg.get("multiplier")) for leg in quoted]
        exit_multipliers = [value if value is not None and isfinite(value) and value > 0 else None for value in exit_multipliers]
        multipliers_agree = bool(exit_multipliers) and all(value == exit_multipliers[0] for value in exit_multipliers)
        accounting = _exit_accounting(
            connection, order, now=now, credit=credit, exit_price=exit_price,
            quantity=exit_quantity, multiplier=exit_multipliers[0] if multipliers_agree else None, exit_fees=fees,
        )
        if not multipliers_agree:
            accounting["exit_contract_multipliers"] = exit_multipliers
            if all(value is not None for value in exit_multipliers):
                accounting["net_pnl_basis"] = "paper_contract_multiplier_conflict"
        connection.execute(
            """
            UPDATE app.paper_order
            SET status = %s, exited_quantity = %s, exit_price = %s, exit_at = %s,
                fees = coalesce(fees, 0) + %s, exit_fees = coalesce(exit_fees, 0) + %s, exit_slippage = %s,
                updated_at = %s, unfilled_reason = NULL
            WHERE id = %s::uuid
            """,
            [status, new_exited, exit_price, now, fees, fees, slippage, now, order["id"]],
        )
        _journal(
            connection, order, action=f"paper_exit:{reason}", quantity=exit_quantity,
            price=exit_price, key=f"generic:{order['id']}:exit:{now.isoformat()}:{reason}",
            details={
                "lane": order["lane"], "paper_order_id": str(order["id"]),
                **accounting, "slippage": slippage, "fees": fees,
                **_experiment_quote_evidence(ticket, quoted),
            },
        )
        self._record_phase4_fill(
            connection, paper_order_id=str(order["id"]), observed_at=now, status=status,
        )
        return {
            "paper_order_id": str(order["id"]),
            "status": "closed" if terminal else "filled",
            "event_status": status if terminal else None,
            "reason": reason, "exit_quantity": exit_quantity,
            "exit_price": exit_price, "net_pnl": accounting["net_pnl"],
        }

    def _record_phase4_fill(
        self, connection: Any, *, paper_order_id: str, observed_at: datetime, status: str,
    ) -> None:
        """Bridge genuine option fills without breaking local unit seams."""

        runtime = getattr(self, "runtime", None)
        if runtime is None:
            return
        from investment_panel.database.portfolio import PortfolioLoopRepository

        PortfolioLoopRepository(runtime).record_existing_paper_order_fill(
            connection, paper_order_id=paper_order_id, observed_at=observed_at, status=status,
        )

    def _current_ticket(
        self,
        connection: Any,
        order: dict[str, Any],
        ticket: dict[str, Any],
        *,
        as_of: datetime,
        for_entry: bool = True,
    ) -> tuple[dict[str, Any] | None, str]:
        decision_id = str(order.get("decision_id") or "")
        version = _integer(order.get("ticket_version"))
        lane = str(order.get("lane") or "")
        if not decision_id or version is None:
            return None, "paper_order_ticket_identity_missing"
        scope = "options-decision-system" if lane == "qqq" else "options-radar"
        lineage = dict(ticket.get("publication_lineage") or {})
        expected_publication = str(lineage.get("publication_id") or "")
        try:
            publication_id = _uuid(expected_publication)
        except (ValueError, TypeError, AttributeError):
            return None, "ticket_publication_identity_missing"
        if ticket.get("experiment"):
            from investment_panel.database.options_experiments import experiment_publication_row

            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ["strategy:options-radar-core"])
            try:
                matches = [experiment_publication_row(
                    connection, str(publication_id), decision_id, as_of=as_of,
                    allow_superseded=not for_entry, for_entry=for_entry,
                )]
            except ValueError as error:
                return None, str(error)
        elif not for_entry:
            authority = connection.execute(
                """SELECT revision.status, revision.authority_group,
                          (SELECT count(*) FROM analysis.strategy_revision active
                           WHERE active.authority_group = revision.authority_group AND active.status = 'active') AS active_count
                   FROM analysis.decision decision
                   JOIN analysis.run run ON run.id = decision.run_id AND run.status = 'succeeded'
                        AND run.strategy_revision_id = decision.strategy_revision_id AND run.input_cutoff <= %s
                   JOIN analysis.strategy_revision revision ON revision.id = decision.strategy_revision_id
                   WHERE decision.id = %s::uuid FOR SHARE OF revision""", [as_of, decision_id],
            ).fetchone()
            if authority is None or authority["status"] != "active" or authority["active_count"] != 1:
                return None, "holding_strategy_authority_invalid"
            model_name = "options_decision_candidate" if lane == "qqq" else "option_radar_opportunity"
            matches = [row for row in current_option_publication_rows(
                connection, scope=scope, model_name=model_name, cutoff=as_of, publication_id=publication_id,
            ) if row["authoritative_decision_id"] == decision_id]
        else:
            matches = [
                row for row in current_option_publication_answers(connection, cutoff=as_of)
                if row["scope"] == scope
                if str(
                    (row["payload"] or {}).get("decision_id")
                    or (row["payload"] or {}).get("opportunity_id")
                    or ""
                ) == decision_id
            ]
        if len(matches) != 1:
            return None, "ticket_no_longer_in_current_publication"
        row = matches[0]
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
        if for_entry and (expires_at is None or expires_at <= as_of):
            return None, "ticket_expired"
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


def _expiration_mark(
    connection: Any, order: dict[str, Any], legs: list[dict[str, Any]], *, now: datetime,
) -> tuple[dict[str, Any] | None, str | None]:
    """Use an exact confirmed expiration close; a stored spot is not settlement."""
    expiration = _date(legs[0].get("expiration"))
    if (len(legs) != 1 or legs[0].get("option_type") != "put" or legs[0].get("side") not in {"sell", "short"}
        or expiration is None or not is_us_market_day(expiration)):
        return None, "expiration_session_or_contract_ineligible"
    session_close = market_session_bounds(expiration)[1].astimezone(UTC)
    if now < session_close:
        return None, "expiration_session_not_closed"
    rows = confirmed_daily_bars(
        connection, [int(order["instrument_id"])], as_of=now, trading_dates=[expiration], require_session_close=True,
    ).get(int(order["instrument_id"]), [])
    if len(rows) != 1:
        return None, "confirmed_expiration_close_unavailable"
    row = rows[0]
    close = _number(row.get("close"))
    observed, available, confirmed = (_timestamp(row.get(key)) for key in ("observed_at", "available_at", "confirmed_at"))
    if (close is None or not isfinite(close) or close <= 0 or not row.get("fact_id") or not row.get("source_id")
        or not row.get("ingest_run_id") or observed is None or available is None or confirmed is None
        or observed.astimezone(MARKET_TZ).date() != expiration
        or not session_close <= available <= confirmed <= now or observed > available):
        return None, "expiration_close_clock_or_value_invalid"
    return {
        "basis": "confirmed_expiration_daily_close", "close": close, "trading_date": expiration.isoformat(),
        "session_close_at": session_close.isoformat(), "source_id": str(row["source_id"]),
        "fact_id": row["fact_id"], "fact_table": row["fact_table"], "ingest_run_id": str(row["ingest_run_id"]),
        "observed_at": observed.isoformat(), "available_at": available.isoformat(), "confirmed_at": confirmed.isoformat(),
    }, None


def _exit_accounting(
    connection: Any, order: dict[str, Any], *, now: datetime, credit: bool,
    exit_price: float, quantity: float, multiplier: float | None, exit_fees: float,
) -> dict[str, Any]:
    """Allocate actual entry cost to this exit; never substitute the first fill."""
    entry_multiplier = _number(order.get("contract_multiplier"))
    entry_multiplier = entry_multiplier if entry_multiplier is not None and isfinite(entry_multiplier) and entry_multiplier > 0 else None
    exit_multiplier = multiplier if multiplier is not None and isfinite(multiplier) and multiplier > 0 else None
    result = {"net_pnl": None, "allocated_entry_fees": None, "entry_journal_ids": [],
              "net_pnl_basis": "paper_fill_or_fee_journal_incomplete",
              "entry_contract_multiplier": entry_multiplier, "exit_contract_multiplier": exit_multiplier}
    # Preserve the two observations even when accounting fails. A later order
    # or catalog update cannot establish agreement at this recorded exit.
    if entry_multiplier is None or exit_multiplier is None:
        return {**result, "net_pnl_basis": "paper_contract_multiplier_missing"}
    if entry_multiplier != exit_multiplier:
        return {**result, "net_pnl_basis": "paper_contract_multiplier_conflict"}
    fills = paper_fill_totals(connection, order, as_of=now)
    if not fills or fills["missing_fees"] or fills["invalid_fills"] or fills.get("fill_multipliers_verified") is not True:
        return result
    values = {key: _number(fills[key]) for key in ("entry_quantity", "exit_quantity", "entry_units", "entry_fees", "actual_fees")}
    filled, exited = _number(order.get("filled_quantity")), _number(order.get("exited_quantity"))
    paid_entry_fees, paid_fees = _number(order.get("entry_fees")), _number(order.get("fees"))
    numbers = [*values.values(), filled, exited, paid_entry_fees, paid_fees, multiplier, exit_price, quantity, exit_fees]
    if any(value is None or not isfinite(value) for value in numbers):
        return result
    if (filled <= 0 or values["entry_quantity"] != filled or values["exit_quantity"] != exited
        or values["entry_units"] <= 0 or not 0 < quantity <= filled - exited
        or exit_price < 0 or exit_fees < 0
        or abs(values["entry_fees"] - paid_entry_fees) > 1e-6 or abs(values["actual_fees"] - paid_fees) > 1e-6):
        return result
    allocated_entry_fees = values["entry_fees"] * quantity / filled
    entry_vwap = values["entry_units"] / filled
    gross = (entry_vwap - exit_price if credit else exit_price - entry_vwap) * multiplier * quantity
    return {**result, "net_pnl": round(gross - allocated_entry_fees - exit_fees, 2),
            "allocated_entry_fees": allocated_entry_fees, "entry_journal_ids": fills["entry_journal_ids"],
            "net_pnl_basis": "journal_entry_vwap_and_paid_fees"}


def _record_liquidation_mark(
    connection: Any, order: dict[str, Any], quotes: list[dict[str, Any]], *,
    now: datetime, execution_blockers: list[str],
) -> None:
    """Measure the observed debit-paper wealth path; never authorize an exit."""
    measured_at = max(now, datetime.now(UTC))
    previous = dict((order.get("execution_quote") or {}).get(PAPER_MARK_KEY) or {})
    mark = {**previous, "status": "unknown", "checked_at": measured_at.isoformat(),
            "current_net_return": None, "max_drawdown": None, "coverage": "observed_executable_quotes_only"}
    reason = None
    filled, exited = _quantity(order.get("filled_quantity")), _quantity(order.get("exited_quantity"))
    requested = _quantity(order.get("quantity"))
    cancellation = dict((order.get("execution_quote") or {}).get(ENTRY_CANCELLATION_KEY) or {})
    cancelled_at = _timestamp(cancellation.get("cancelled_at"))
    remainder_cancelled = (
        0 < filled < requested and str(order.get("status")) in {"entered", "partial_exited"}
        and cancellation.get("status") == "cancelled" and cancellation.get("paper_order_id") == str(order["id"])
        and cancelled_at is not None and cancelled_at <= now
        and cancellation.get("requested_quantity") == requested and cancellation.get("filled_quantity") == filled
        and cancellation.get("cancelled_quantity") == requested - filled
    )
    entry_quantity_fixed = filled == requested or remainder_cancelled
    remaining = filled - exited
    multiplier = _number(order.get("contract_multiplier"))
    if is_credit_structure(str(order.get("structure") or "")):
        reason = "credit_or_assignment_return_basis_unavailable"
    elif filled <= 0 or filled > requested or remaining <= 0:
        reason = "entry_fill_quantity_incomplete"
    elif execution_blockers or not quotes or any(
        not leg.get("quote_id") or leg.get("capture_complete") is not True
        or not isinstance(leg.get("observed_at"), datetime) or not isinstance(leg.get("quote_time"), datetime)
        or not leg["observed_at"] <= leg["quote_time"] <= measured_at for leg in quotes
    ):
        reason = "complete_fresh_executable_mark_unavailable"
    elif multiplier is None or not isfinite(multiplier) or multiplier <= 0 or any(leg.get("multiplier") != multiplier for leg in quotes):
        reason = "contract_multiplier_unverified"
    elif _available_quantity(quotes, phase="exit", requested=remaining) < remaining:
        reason = "full_remaining_liquidation_size_unavailable"
    else:
        fills = paper_fill_totals(connection, order, as_of=measured_at)
        if fills is None:
            reason = "paper_fill_journal_missing"
        else:
            entry_cash = float(fills["entry_units"]) * multiplier
            exit_cash = float(fills["exit_units"]) * multiplier
            actual_fees = float(fills["actual_fees"])
            price = package_price(quotes, phase="exit")
            # Entry marks are provisional while another fill can change the
            # capital basis. Retain their real quote tape if cancellation fixes
            # that same basis; start again if a later entry added capital.
            if previous.get("entry_quantity_fixed") is False and (
                previous.get("entry_cash") != entry_cash or previous.get("entry_quantity") != filled
            ):
                previous = {}
                mark = {"status": "unknown", "checked_at": measured_at.isoformat(),
                        "current_net_return": None, "max_drawdown": None, "coverage": "observed_executable_quotes_only"}
            if (float(fills["entry_quantity"]) != filled or float(fills["exit_quantity"]) != exited
                or fills["missing_fees"] or fills["invalid_fills"] or fills.get("fill_multipliers_verified") is not True
                or entry_cash <= 0 or price is None
                or any(not isfinite(value) for value in (entry_cash, exit_cash, actual_fees))
                or abs(actual_fees - float(order.get("fees") or 0)) > 1e-6):
                reason = "paper_fill_or_fee_journal_incomplete"
            elif previous.get("entry_cash") is not None and abs(float(previous["entry_cash"]) - entry_cash) > 1e-6:
                reason = "entry_capital_basis_changed"
            else:
                modeled_fees = _fees(len(quotes), remaining)
                value = (exit_cash + remaining * price * multiplier - entry_cash - actual_fees - modeled_fees) / entry_cash
                quote_ids = [str(leg["quote_id"]) for leg in quotes]
                count = int(previous.get("mark_count") or 0) + int(quote_ids != previous.get("quote_ids"))
                previous_peak = max(0.0, float(previous.get("peak_net_return") or 0))
                peak = max(previous_peak, value)
                new_quote_peak = value > previous_peak
                initial_at = (_timestamp(order.get("filled_at")) or measured_at).isoformat()
                peak_at = measured_at.isoformat() if new_quote_peak else previous.get("peak_at") or initial_at
                evidence = [{key: item.isoformat() if isinstance(item, (datetime, date)) else item for key, item in leg.items()} for leg in quotes]
                drawdown = min(0.0, (1 + value) / (1 + peak) - 1) if peak > -1 else None
                observed_drawdown = min(float(previous.get("observed_max_drawdown") or 0), drawdown) if drawdown is not None else None
                mark.update({
                    "status": "observed", "reason": None, "measured_at": measured_at.isoformat(),
                    "first_observed_at": previous.get("first_observed_at") or measured_at.isoformat(),
                    "current_net_return": value, "peak_net_return": peak, "peak_at": peak_at,
                    "mark_count": count, "max_drawdown": observed_drawdown if count >= 2 else None,
                    "observed_max_drawdown": observed_drawdown,
                    "drawdown_reason": None if count >= 2 and drawdown is not None else "two_distinct_marks_required",
                    "quote_ids": quote_ids, "quotes": evidence, "journal_ids": fills["journal_ids"],
                    "entry_quantity": filled, "exited_quantity": exited, "remaining_quantity": remaining,
                    "entry_quantity_fixed": entry_quantity_fixed,
                    "entry_quantity_basis": "cancelled_remainder" if remainder_cancelled else "filled_order" if entry_quantity_fixed else "pending_entry",
                    "entry_cash": entry_cash, "exit_cash": exit_cash, "contract_multiplier": multiplier,
                    "actual_fees": actual_fees, "modeled_remaining_exit_fees": modeled_fees,
                    "capital_basis": "actual_entry_debit",
                })
                if new_quote_peak:
                    mark.update({"peak_quotes": evidence, "peak_basis": "observed_liquidation", "peak_journal_ids": []})
                elif not previous.get("peak_at"):
                    mark.update({"peak_quotes": [], "peak_basis": "initial_entry_capital", "peak_journal_ids": fills["entry_journal_ids"]})
                if drawdown is not None and drawdown <= float(previous.get("observed_max_drawdown") or 0):
                    mark.update({"drawdown_peak_at": peak_at, "drawdown_trough_at": measured_at.isoformat(),
                                 "drawdown_peak_basis": mark.get("peak_basis"), "drawdown_peak_journal_ids": mark.get("peak_journal_ids"),
                                 "drawdown_peak_quotes": mark.get("peak_quotes"), "drawdown_trough_quotes": evidence})
                if not entry_quantity_fixed:
                    mark.update({"status": "unknown", "reason": "entry_fill_quantity_incomplete",
                                 "current_net_return": None, "max_drawdown": None,
                                 "drawdown_reason": "entry_quantity_not_fixed"})
    if reason:
        mark["reason"] = reason
        mark["missing_mark_count"] = int(previous.get("missing_mark_count") or 0) + 1
    connection.execute(
        "UPDATE app.paper_order SET execution_quote = coalesce(execution_quote, '{}'::jsonb) || %s WHERE id = %s::uuid",
        [Jsonb({PAPER_MARK_KEY: mark}), str(order["id"])],
    )


def _experiment_quote_scope(ticket: dict[str, Any]) -> dict[str, Any]:
    if not ticket.get("experiment"):
        return {}
    return {"source_id": str((ticket.get("provenance") or {}).get("quote_source") or ""), "complete_capture_only": True}


def _experiment_quote_evidence(ticket: dict[str, Any], legs: list[dict[str, Any]]) -> dict[str, Any]:
    if not ticket.get("experiment"):
        return {}
    return {"experiment": ticket["experiment"], "quotes": [
        {key: value.isoformat() if isinstance(value, (date, datetime)) else value for key, value in leg.items()}
        for leg in legs
    ]}


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
