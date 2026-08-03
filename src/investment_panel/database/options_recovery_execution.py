"""PostgreSQL execution owner for forward-only recovery signals and paper orders."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.core.options_event_tape import trading_sessions_between
from investment_panel.core.options_recovery import (
    FEE_PER_CONTRACT_LEG,
    ExecutableLeg,
    QuoteCapture,
    evaluate_lifecycle,
    executable_exit_price,
)
from investment_panel.core.options_recovery_paper import (
    RecoveryRiskContext,
    qualified_for_paper,
    size_recovery_position,
)
from investment_panel.core.options_recovery_registry import (
    EventSpot,
    RankedRecoveryCandidate,
    RecoveryContractQuote,
    RecoveryEventState,
    rank_candidate,
    signal_for,
    strategies,
)
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.database.options_recovery_execution_support import (
    contract_quote as _contract_quote,
    decision_key as _decision_key,
    invalidated as _invalidated,
    journal as _journal,
    leg_expiry_days,
    midpoint as _mid,
    number as _number,
    one_unit_ticket as _one_unit_ticket,
    order_status as _order_status,
    select_published as _select_published,
    selection_inputs as _selection_inputs,
    utc as _utc,
)


CODE_VERSION = "options-recovery-v4"


class RecoveryExecutionRepository:
    """Select, ticket, paper-stage, and manage recovery positions deterministically."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime
        self.analysis = AnalysisRepository(runtime)

    def evaluate_capture(
        self,
        event_id: str,
        *,
        capture_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Score one completed event-strip capture and create at most two tickets."""

        reference = _utc(now) or datetime.now(UTC)
        revisions = self._ensure_strategies()
        loaded = self._load_capture(event_id, capture_id=capture_id, now=reference)
        if loaded is None:
            return {"status": "skipped", "reason": "event_capture_not_available", "event_id": event_id}
        event, capture, contracts = loaded
        signals = [signal_for(event, strategy.key) for strategy in strategies()]
        from investment_panel.database.options_recovery_learning import RecoveryLearningRepository

        learning = RecoveryLearningRepository(self.runtime)
        learning.record_capture(
            event=event,
            capture=capture,
            contracts=contracts,
            revisions=revisions,
            family_signals=signals,
        )
        active = [signal for signal in signals if signal.active]
        if not active:
            return {
                "status": "ok", "event_id": event_id, "capture_id": str(capture["id"]),
                "signals": {signal.family: list(signal.reasons) for signal in signals}, "selected": [],
            }
        candidates: dict[str, list[tuple[RankedRecoveryCandidate, dict[str, Any]]]] = {}
        for family_signal in active:
            family_rows: list[tuple[RankedRecoveryCandidate, dict[str, Any]]] = []
            for source in contracts:
                quote = _contract_quote(source)
                if quote is None:
                    continue
                one_unit = _one_unit_ticket(
                    event=event,
                    family=family_signal.family,
                    quote=quote,
                    decision_id="pending",
                    created_at=reference,
                )
                maximum_loss = float((one_unit.get("risk") or {}).get("one_unit_max_loss") or 0.0)
                ranked = rank_candidate(
                    event=event,
                    family=family_signal.family,
                    quote=quote,
                    as_of=reference,
                    lower_confidence_expectancy=self._lower_confidence_expectancy(family_signal.family),
                    maximum_loss=maximum_loss,
                )
                if ranked.gate.eligible:
                    family_rows.append((ranked, source))
            candidates[family_signal.family] = sorted(family_rows, key=lambda item: item[0].selection_score, reverse=True)
        selected = _select_published(candidates)
        learning.mark_selection(
            event_id=event_id,
            capture=capture,
            revisions=revisions,
            selected=selected,
        )
        if not selected:
            return {
                "status": "ok", "event_id": event_id, "capture_id": str(capture["id"]),
                "signals": {signal.family: list(signal.reasons) for signal in signals}, "selected": [],
                "reason": "no_contract_passed_common_gates",
            }
        run_id = self.analysis.start_run(
            "options_recovery",
            input_cutoff=reference,
            code_version=CODE_VERSION,
            inputs={"event_id": event_id, "capture_id": str(capture["id"]), "selected": _selection_inputs(selected)},
            feature_versions={"option": "short_horizon_convex_v1"},
        )
        persisted: list[dict[str, Any]] = []
        try:
            for rank, (candidate, source) in enumerate(selected, start=1):
                strategy_id = revisions[candidate.family]
                decision_id = self.analysis.store_option_decision(
                    run_id,
                    decision_key=_decision_key(event_id, str(capture["id"]), candidate),
                    instrument_id=int(source["instrument_id"]),
                    contract_id=candidate.quote.contract_id,
                    snapshot_id=int(capture["snapshot_id"]),
                    quote_observed_at=candidate.quote.observed_at,
                    state="WATCH",
                    score=candidate.selection_score,
                    rank=rank,
                    inputs={
                        "event_id": event_id,
                        "capture_id": str(capture["id"]),
                        "family": candidate.family,
                        "contract_id": candidate.quote.contract_id,
                        "available_at": candidate.quote.available_at.isoformat(),
                    },
                    reasons=[candidate.family, "event_strip_forward_only"],
                    blockers=[],
                    details={
                        "structure": "long_option",
                        "premium_mid": _mid(candidate.quote.bid, candidate.quote.ask),
                        "fill_assumption": candidate.maximum_loss / 100.0,
                        "entry_price": candidate.maximum_loss / 100.0,
                        "max_loss": candidate.maximum_loss,
                        "details": {"objective_version": "short_horizon_convex_v1", "event_id": event_id},
                    },
                    strategy_revision_id=strategy_id,
                )
                risk = self._risk_context(
                    event_id=event_id,
                    instrument_id=int(source["instrument_id"]),
                    family=candidate.family,
                )
                risk_decision = size_recovery_position(candidate.maximum_loss, risk)
                ticket = _one_unit_ticket(
                    event=event,
                    family=candidate.family,
                    quote=candidate.quote,
                    decision_id=str(decision_id),
                    quantity=risk_decision.quantity,
                    created_at=reference,
                    blockers=[*candidate.gate.blockers, *risk_decision.blockers],
                    lower_confidence_expectancy=candidate.lower_confidence_expectancy,
                )
                signal = self._upsert_signal(
                    event_id=event_id,
                    event_contract_id=int(source["event_contract_id"]),
                    capture_id=str(capture["id"]),
                    decision_id=str(decision_id),
                    snapshot_id=int(capture["snapshot_id"]),
                    contract_id=candidate.quote.contract_id,
                    family=candidate.family,
                    strategy_revision_id=strategy_id,
                    signal_at=reference,
                    available_at=candidate.quote.available_at,
                    selection_score=candidate.selection_score,
                    lower_confidence_expectancy=candidate.lower_confidence_expectancy,
                    maximum_loss=candidate.maximum_loss,
                    gate_result={"eligible": True, "blockers": list(candidate.gate.blockers)},
                    ticket=ticket,
                    status="shadow",
                )
                learning.link_signal(
                    signal_id=signal,
                    event_id=event_id,
                    capture=capture,
                    contract_id=candidate.quote.contract_id,
                    family=candidate.family,
                    strategy_revision_id=strategy_id,
                )
                persisted.append({
                    "signal_id": signal, "decision_id": str(decision_id), "family": candidate.family,
                    "contract_id": candidate.quote.contract_id, "ticket": ticket,
                })
            self.analysis.finish_run(run_id, "succeeded", {"event_id": event_id, "signals": len(persisted)})
        except Exception:
            self.analysis.finish_run(run_id, "failed", {"event_id": event_id})
            raise
        return {
            "status": "ok", "event_id": event_id, "capture_id": str(capture["id"]),
            "signals": {signal.family: list(signal.reasons) for signal in signals}, "selected": persisted,
        }

    def stage_qualified_orders(
        self,
        event_id: str,
        *,
        now: datetime | None = None,
        enabled: bool = False,
    ) -> dict[str, Any]:
        """Create local paper orders only after five qualified event sessions."""

        reference = _utc(now) or datetime.now(UTC)
        if not enabled:
            return {"status": "disabled", "reason": "recovery_paper_actions_disabled", "orders": []}
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT signal.id, signal.ticket, signal.decision_id, signal.strategy_key, signal.contract_id,
                       event.id AS event_id, event.started_at, event.instrument_id, instrument.symbol
                FROM (
                  SELECT DISTINCT ON (strategy_key) *
                  FROM analysis.option_event_signal
                  WHERE event_id = %s AND status = 'shadow'
                    AND available_at >= %s - interval '20 minutes'
                  ORDER BY strategy_key, available_at DESC, id DESC
                ) signal
                JOIN analysis.option_event event ON event.id = signal.event_id
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                ORDER BY signal.available_at, signal.id
                """,
                [event_id, reference],
            ).fetchall()
        orders = []
        for row in rows:
            if not qualified_for_paper(row["started_at"], reference):
                continue
            orders.append(self._stage_order(dict(row), now=reference))
        return {"status": "ok", "event_id": event_id, "orders": orders}

    def manage_event_orders(self, event_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Advance local paper orders using only post-ticket, same-contract captures."""

        reference = _utc(now) or datetime.now(UTC)
        with self.runtime.read(JOB_PROFILE) as connection:
            orders = [dict(row) for row in connection.execute(
                """
                SELECT paper.*, event.started_at, event.reference_price, event.event_low,
                       signal.strategy_key, signal.id AS signal_id
                FROM app.paper_order paper
                JOIN analysis.option_event event ON event.id = paper.event_id
                JOIN analysis.option_event_signal signal ON signal.id = paper.event_signal_id
                WHERE paper.event_id = %s
                  AND paper.status IN ('staged', 'entered', 'partial_exited')
                ORDER BY paper.created_at, paper.id
                """,
                [event_id],
            ).fetchall()]
        managed = [self._manage_order(order, reference) for order in orders]
        return {"status": "ok", "event_id": event_id, "orders": managed}

    def _ensure_strategies(self) -> dict[str, int]:
        ids: dict[str, int] = {}
        for strategy in strategies():
            ids[strategy.key] = self.analysis.register_strategy(
                strategy.key,
                strategy.revision,
                name=strategy.name,
                parameters=dict(strategy.parameters),
                status="candidate",
                authority_group=f"options-recovery:{strategy.key}",
            )
        return ids

    def _load_capture(
        self, event_id: str, *, capture_id: str | None, now: datetime
    ) -> tuple[RecoveryEventState, dict[str, Any], list[dict[str, Any]]] | None:
        with self.runtime.read(JOB_PROFILE) as connection:
            event_row = connection.execute(
                """
                SELECT event.id, event.started_at, event.reference_price, event.event_low, instrument.symbol
                FROM analysis.option_event event
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                WHERE event.id = %s AND event.status = 'active'
                """,
                [event_id],
            ).fetchone()
            if event_row is None:
                return None
            capture = connection.execute(
                """
                SELECT id, snapshot_id, capture_generation_id, scheduled_at, finished_at
                FROM analysis.option_event_capture
                WHERE event_id = %s
                  AND status IN ('complete', 'partial')
                  AND snapshot_id IS NOT NULL
                  AND (%s::uuid IS NULL OR id = %s::uuid)
                ORDER BY scheduled_at DESC
                LIMIT 1
                """,
                [event_id, capture_id, capture_id],
            ).fetchone()
            if capture is None or capture["finished_at"] is None or capture["finished_at"] > now:
                return None
            spots = connection.execute(
                """
                SELECT observed_at, available_at, price
                FROM analysis.option_event_spot
                WHERE event_id = %s AND available_at <= %s
                ORDER BY available_at, observed_at
                """,
                [event_id, capture["finished_at"]],
            ).fetchall()
            rows = connection.execute(
                """
                SELECT event_contract.id AS event_contract_id, event_contract.contract_id,
                       event.instrument_id, instrument.symbol, contract.expiration, contract.strike, contract.option_type,
                       contract.provider_symbols, quote.bid, quote.ask, quote.bid_size, quote.ask_size,
                       quote.open_interest, quote.provider_delta, quote.observed_at, quote.available_at,
                       quote.volume
                FROM analysis.option_event_contract event_contract
                JOIN analysis.option_event event ON event.id = event_contract.event_id
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                JOIN catalog.option_contract contract ON contract.id = event_contract.contract_id
                JOIN raw.option_quote quote
                  ON quote.snapshot_id = %s AND quote.contract_id = event_contract.contract_id
                WHERE event_contract.event_id = %s AND event_contract.retired_at IS NULL
                  AND quote.available_at <= %s
                """,
                [capture["snapshot_id"], event_id, capture["finished_at"]],
            ).fetchall()
        event = RecoveryEventState(
            event_id=str(event_row["id"]),
            symbol=str(event_row["symbol"]),
            reference_price=float(event_row["reference_price"]),
            event_low=float(event_row["event_low"]),
            started_at=event_row["started_at"],
            spots=tuple(EventSpot(row["observed_at"], row["available_at"], float(row["price"])) for row in spots),
        )
        return event, dict(capture), [dict(row) for row in rows]

    def _lower_confidence_expectancy(self, family: str) -> float:
        # Before there are closed paper fills this is the neutral zero prior.
        # Once evidence exists, live ranking and replay use the same
        # independently computed cost-adjusted lower bound.
        from investment_panel.database.options_recovery_learning import RecoveryLearningRepository

        metric = RecoveryLearningRepository(self.runtime).metrics(family)
        return float(metric.get("lower_95_expectancy") or 0.0)

    def _risk_context(self, *, event_id: str, instrument_id: int, family: str) -> RecoveryRiskContext:
        with self.runtime.read(JOB_PROFILE) as connection:
            risk_row = connection.execute(
                """
                SELECT
                  coalesce(sum((ticket_snapshot->'risk'->>'total_risk')::numeric)
                    FILTER (WHERE status IN ('staged', 'entered', 'partial_exited')), 0) AS open_risk,
                  coalesce(sum((ticket_snapshot->'risk'->>'total_risk')::numeric)
                    FILTER (WHERE status IN ('staged', 'entered', 'partial_exited')
                            AND instrument_id = %s), 0) AS symbol_risk,
                  count(*) FILTER (WHERE status IN ('staged', 'entered', 'partial_exited')) AS positions,
                  bool_or(event_id = %s AND strategy_family = %s
                          AND status IN ('staged', 'entered', 'partial_exited')) AS existing
                FROM app.paper_order
                """,
                [instrument_id, event_id, family],
            ).fetchone()
            pnl = connection.execute(
                """
                SELECT coalesce(sum((details->>'net_pnl')::numeric), 0) AS value
                FROM app.trade_journal
                WHERE created_at >= date_trunc('day', now())
                  AND details ? 'net_pnl'
                """
            ).fetchone()
            active_orders = [dict(item) for item in connection.execute(
                """
                SELECT id, quantity, actual_fill_price, ticket_snapshot
                FROM app.paper_order
                WHERE status IN ('entered', 'partial_exited')
                  AND actual_fill_price IS NOT NULL
                """
            ).fetchall()]
            exit_rows = connection.execute(
                """
                SELECT details->>'paper_order_id' AS paper_order_id,
                       coalesce(sum(quantity), 0) AS quantity
                FROM app.trade_journal
                WHERE action LIKE 'paper_exit:%' AND details ? 'paper_order_id'
                GROUP BY details->>'paper_order_id'
                """
            ).fetchall()
            quoted_legs = connection.execute(
                """
                SELECT leg.paper_order_id, leg.contract_id, leg.side, quote.bid, quote.ask,
                       quote.bid_size, quote.ask_size
                FROM app.paper_order_leg leg
                JOIN app.paper_order paper ON paper.id = leg.paper_order_id
                LEFT JOIN LATERAL (
                  SELECT bid, ask, bid_size, ask_size
                  FROM raw.option_quote
                  WHERE contract_id = leg.contract_id
                  ORDER BY available_at DESC LIMIT 1
                ) quote ON true
                WHERE paper.status IN ('entered', 'partial_exited')
                """
            ).fetchall()
        exited = {str(row["paper_order_id"]): int(row["quantity"] or 0) for row in exit_rows}
        packages: dict[str, list[ExecutableLeg]] = {}
        for quoted_leg in quoted_legs:
            packages.setdefault(str(quoted_leg["paper_order_id"]), []).append(ExecutableLeg(
                str(quoted_leg["contract_id"]), str(quoted_leg["side"]), quoted_leg["bid"], quoted_leg["ask"], quoted_leg["bid_size"], quoted_leg["ask_size"],
            ))
        unrealized = 0.0
        for paper in active_orders:
            remaining = max(0, int(paper["quantity"]) - exited.get(str(paper["id"]), 0))
            legs = packages.get(str(paper["id"]), [])
            if not remaining or not legs:
                continue
            try:
                mark = executable_exit_price(legs)
            except ValueError:
                continue
            unrealized += (mark - float(paper["actual_fill_price"])) * 100.0 * remaining
            unrealized -= FEE_PER_CONTRACT_LEG * len(legs) * remaining * 2
        return RecoveryRiskContext(
            open_risk=float(risk_row["open_risk"] or 0),
            symbol_open_risk=float(risk_row["symbol_risk"] or 0),
            open_positions=int(risk_row["positions"] or 0),
            daily_realized_unrealized_pnl=float(pnl["value"] or 0) + unrealized,
            existing_event_family_position=bool(risk_row["existing"]),
        )

    def _upsert_signal(self, **values: Any) -> str:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                INSERT INTO analysis.option_event_signal
                    (event_id, event_contract_id, capture_id, decision_id, snapshot_id, contract_id,
                     strategy_key, strategy_revision_id, signal_at, available_at, selection_score,
                     lower_confidence_expectancy, maximum_loss, gate_result, ticket, status)
                VALUES (%(event_id)s, %(event_contract_id)s, %(capture_id)s, %(decision_id)s, %(snapshot_id)s,
                        %(contract_id)s, %(family)s, %(strategy_revision_id)s, %(signal_at)s, %(available_at)s,
                        %(selection_score)s, %(lower_confidence_expectancy)s, %(maximum_loss)s,
                        %(gate_result)s, %(ticket)s, %(status)s)
                ON CONFLICT (event_id, event_contract_id, capture_id, strategy_key, strategy_revision_id)
                DO UPDATE SET decision_id = EXCLUDED.decision_id, signal_at = EXCLUDED.signal_at,
                              available_at = EXCLUDED.available_at, selection_score = EXCLUDED.selection_score,
                              lower_confidence_expectancy = EXCLUDED.lower_confidence_expectancy,
                              maximum_loss = EXCLUDED.maximum_loss, gate_result = EXCLUDED.gate_result,
                              ticket = EXCLUDED.ticket, status = EXCLUDED.status, updated_at = now()
                RETURNING id
                """,
                {**values, "gate_result": Jsonb(values["gate_result"]), "ticket": Jsonb(values["ticket"])},
            ).fetchone()
        return str(row["id"])

    def _stage_order(self, signal: dict[str, Any], *, now: datetime) -> dict[str, Any]:
        ticket = dict(signal["ticket"] or {})
        unit = float((ticket.get("risk") or {}).get("one_unit_max_loss") or 0.0)
        risk = self._risk_context(
            event_id=str(signal["event_id"]),
            instrument_id=int(signal["instrument_id"]),
            family=str(signal["strategy_key"]),
        )
        decision = size_recovery_position(unit, risk)
        if decision.quantity <= 0:
            self._set_signal_status(str(signal["id"]), "risk_blocked", {"risk_blockers": list(decision.blockers)})
            return {"signal_id": str(signal["id"]), "status": "risk_blocked", "blockers": list(decision.blockers)}
        ticket = {**ticket, "risk": {**dict(ticket.get("risk") or {}), "total_risk": decision.total_risk}, "quantity": decision.quantity}
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ["recovery-paper-order"])
            prior = connection.execute(
                "SELECT id, status FROM app.paper_order WHERE event_id = %s AND strategy_family = %s",
                [signal["event_id"], signal["strategy_key"]],
            ).fetchone()
            if prior:
                return {"signal_id": str(signal["id"]), "status": str(prior["status"]), "paper_order_id": str(prior["id"])}
            key = f"recovery:{signal['id']}:v4"
            row = connection.execute(
                """
                INSERT INTO app.paper_order
                    (decision_id, instrument_id, side, quantity, limit_price, status, policy_result,
                     structure, idempotency_key, ticket_version, ticket_snapshot, intended_limit_price,
                     event_id, event_signal_id, strategy_family, objective_version, created_at)
                VALUES (%s, %s, 'buy', %s, %s, 'staged', %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s)
                RETURNING id
                """,
                [
                    signal["decision_id"], signal["instrument_id"], decision.quantity,
                    ticket.get("entry", {}).get("limit_price"),
                    Jsonb({"owner": "options_recovery", "live_order_submission": False}),
                    ticket.get("structure"), key, 4, Jsonb(ticket), ticket.get("entry", {}).get("limit_price"),
                    signal["event_id"], signal["id"], signal["strategy_key"], ticket.get("objective_version"), now,
                ],
            ).fetchone()
            for index, leg in enumerate(ticket.get("legs") or []):
                connection.execute(
                    """
                    INSERT INTO app.paper_order_leg
                        (paper_order_id, leg_index, contract_id, option_type, side, strike, bid, ask,
                         bid_size, ask_size, quote_time, open_interest, volume)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [row["id"], index, int(leg["contract_id"]), leg["option_type"], leg["side"], leg["strike"],
                     leg["bid"], leg["ask"], leg["bid_size"], leg["ask_size"], leg["quote_time"],
                     leg.get("open_interest"), leg.get("volume")],
                )
            connection.execute(
                """INSERT INTO app.alert (decision_id, instrument_id, alert_type, severity, title, detail)
                   VALUES (%s, %s, 'options_recovery_ticket', 'medium', %s, %s)""",
                [signal["decision_id"], signal["instrument_id"], f"Paper recovery ticket: {signal['symbol']} {signal['strategy_key']}", key],
            )
            _journal(connection, signal, action="paper_order_staged", quantity=decision.quantity,
                     price=ticket.get("entry", {}).get("limit_price"), key=key, details={"paper_order_id": str(row["id"])})
            connection.execute("UPDATE analysis.option_event_signal SET status = 'ticketed', ticket = %s, updated_at = now() WHERE id = %s", [Jsonb(ticket), signal["id"]])
        return {"signal_id": str(signal["id"]), "status": "staged", "paper_order_id": str(row["id"]), "quantity": decision.quantity}

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
