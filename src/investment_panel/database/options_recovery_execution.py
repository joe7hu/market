"""PostgreSQL execution owner for forward-only recovery signals and paper orders."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.core.decision import MARKET_TZ
from investment_panel.core.options_recovery import (
    FEE_PER_CONTRACT_LEG,
    ExecutableLeg,
    executable_exit_price,
)
from investment_panel.core.options_recovery_paper import (
    RecoveryRiskContext,
    RecoveryRiskPolicy,
    missing_recovery_risk_policy,
    size_recovery_position,
)
from investment_panel.core.options_recovery_registry import (
    EventSpot,
    RankedRecoveryCandidate,
    RecoveryContractQuote,
    RecoveryEventState,
    contract_gate,
    rank_candidate,
    signal_for,
    strategies,
)
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.options_recovery_cohorts import (
    CURRENT_CODE_VERSION,
    CURRENT_OBJECTIVE_VERSION,
    RecoveryCohortRepository,
)
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.database.options_recovery_execution_support import (
    contract_quote as _contract_quote,
    decision_key as _decision_key,
    invalidated as _invalidated,
    journal as _journal,
    midpoint as _mid,
    number as _number,
    one_unit_ticket as _one_unit_ticket,
    select_published as _select_published,
    selection_inputs as _selection_inputs,
    utc as _utc,
)
from investment_panel.database.options_recovery_execution_lifecycle import RecoveryOrderLifecycle
from investment_panel.database.options_recovery_execution_staging import RecoveryOrderStaging
from investment_panel.database.options_paper_ledger import acquire_shared_sleeve_lock, shared_sleeve_blockers


CODE_VERSION = CURRENT_CODE_VERSION


class RecoveryExecutionRepository(RecoveryOrderLifecycle, RecoveryOrderStaging):
    """Select, ticket, paper-stage, and manage recovery positions deterministically."""

    def __init__(self, runtime: DatabaseRuntime, *, risk_policy: RecoveryRiskPolicy | None = None) -> None:
        self.runtime = runtime
        self.analysis = AnalysisRepository(runtime)
        self.cohorts = RecoveryCohortRepository(runtime)
        # No constructor fallback may authorize a recovery sleeve.  Production
        # jobs inject the typed configuration; ad-hoc callers remain blocked.
        self.risk_policy = risk_policy or missing_recovery_risk_policy()

    def evaluate_capture(
        self,
        event_id: str,
        *,
        capture_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Score one completed event-strip capture and create at most two tickets."""

        reference = _utc(now) or datetime.now(UTC)
        cohort_id = self.cohorts.current_id()
        if cohort_id is None or not self.cohorts.event_is_current_valid(event_id):
            return {"status": "skipped", "reason": "event_not_in_current_valid_cohort", "event_id": event_id}
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
                    risk_policy=self.risk_policy,
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
            feature_versions={"option": CURRENT_OBJECTIVE_VERSION},
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
                        "details": {"objective_version": CURRENT_OBJECTIVE_VERSION, "event_id": event_id,
                                    "cohort_code_version": CODE_VERSION},
                    },
                    strategy_revision_id=strategy_id,
                )
                risk = self._risk_context(
                    event_id=event_id,
                    instrument_id=int(source["instrument_id"]),
                    family=candidate.family,
                    now=reference,
                )
                risk_decision = size_recovery_position(candidate.maximum_loss, risk, self.risk_policy)
                ticket = _one_unit_ticket(
                    event=event,
                    family=candidate.family,
                    quote=candidate.quote,
                    decision_id=str(decision_id),
                    quantity=risk_decision.quantity,
                    created_at=reference,
                    blockers=[*candidate.gate.blockers, *risk_decision.blockers],
                    lower_confidence_expectancy=candidate.lower_confidence_expectancy,
                    risk_policy=self.risk_policy,
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
                    cohort_id=cohort_id,
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

    def manage_event_orders(self, event_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Advance local paper orders using only post-ticket, same-contract captures."""

        reference = _utc(now) or datetime.now(UTC)
        with self.runtime.read(JOB_PROFILE) as connection:
            orders = [dict(row) for row in connection.execute(
                f"""
                SELECT paper.*, event.started_at, event.reference_price, event.event_low,
                       signal.strategy_key, signal.id AS signal_id
                FROM app.paper_order paper
                JOIN analysis.option_event event ON event.id = paper.event_id
                JOIN analysis.option_event_signal signal ON signal.id = paper.event_signal_id
                WHERE paper.event_id = %s
                  AND paper.status IN ('staged', 'entered', 'partial_exited')
                  AND paper.cohort_id = event.cohort_id
                  AND {self.cohorts.current_event_clause()}
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
                f"""
                SELECT event.id, event.started_at, event.reference_price, event.event_low,
                       event.cohort_id, event.reference_available_at, event.quote_age_minutes,
                       instrument.symbol
                FROM analysis.option_event event
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                WHERE event.id = %s AND event.status = 'active'
                  AND {self.cohorts.current_event_clause()}
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
                       event_contract.ladder_slot_key,
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
                 AND quote.capture_generation_id = %s
                WHERE event_contract.event_id = %s AND event_contract.retired_at IS NULL
                  AND quote.available_at <= %s
                """,
                [capture["snapshot_id"], capture["capture_generation_id"], event_id, capture["finished_at"]],
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

    def _risk_context(
        self,
        *,
        event_id: str,
        instrument_id: int,
        family: str,
        now: datetime | None = None,
    ) -> RecoveryRiskContext:
        cohort_id = self.cohorts.current_id()
        if cohort_id is None:
            return RecoveryRiskContext()
        reference = _utc(now) or datetime.now(UTC)
        day_start = reference.astimezone(MARKET_TZ).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).astimezone(UTC)
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
                WHERE created_at <= %s
                """,
                [instrument_id, event_id, family, reference],
            ).fetchone()
            pnl = connection.execute(
                """
                SELECT coalesce(sum((details->>'net_pnl')::numeric), 0) AS value
                FROM app.trade_journal
                WHERE created_at >= %s AND created_at <= %s
                  AND details ? 'net_pnl'
                  AND (details->>'net_pnl') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                """
                , [day_start, reference]
            ).fetchone()
            active_orders = [dict(item) for item in connection.execute(
                """
                SELECT id, quantity, actual_fill_price, ticket_snapshot
                FROM app.paper_order
                WHERE status IN ('entered', 'partial_exited')
                  AND actual_fill_price IS NOT NULL
                  AND cohort_id = %s::uuid
                  AND created_at <= %s
                """
                , [cohort_id, reference]
            ).fetchall()]
            exit_rows = connection.execute(
                """
                SELECT details->>'paper_order_id' AS paper_order_id,
                       coalesce(sum(quantity), 0) AS quantity
                FROM app.trade_journal
                WHERE action LIKE 'paper_exit:%%' AND details ? 'paper_order_id'
                  AND created_at <= %s
                  AND details->>'objective_version' = %s
                  AND details->>'cohort_id' = %s
                GROUP BY details->>'paper_order_id'
                """
                , [reference, CURRENT_OBJECTIVE_VERSION, cohort_id]
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
                    AND available_at <= %s
                  ORDER BY available_at DESC LIMIT 1
                ) quote ON true
                WHERE paper.status IN ('entered', 'partial_exited')
                  AND paper.cohort_id = %s::uuid
                  AND paper.created_at <= %s
                """
                , [reference, cohort_id, reference]
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
                     lower_confidence_expectancy, maximum_loss, gate_result, ticket, status, cohort_id,
                     objective_version)
                VALUES (%(event_id)s, %(event_contract_id)s, %(capture_id)s, %(decision_id)s, %(snapshot_id)s,
                        %(contract_id)s, %(family)s, %(strategy_revision_id)s, %(signal_at)s, %(available_at)s,
                        %(selection_score)s, %(lower_confidence_expectancy)s, %(maximum_loss)s,
                        %(gate_result)s, %(ticket)s, %(status)s, %(cohort_id)s, %(objective_version)s)
                ON CONFLICT (event_id, event_contract_id, capture_id, strategy_key, strategy_revision_id)
                DO UPDATE SET decision_id = EXCLUDED.decision_id, signal_at = EXCLUDED.signal_at,
                              available_at = EXCLUDED.available_at, selection_score = EXCLUDED.selection_score,
                              lower_confidence_expectancy = EXCLUDED.lower_confidence_expectancy,
                              maximum_loss = EXCLUDED.maximum_loss, gate_result = EXCLUDED.gate_result,
                              ticket = EXCLUDED.ticket, status = EXCLUDED.status,
                              cohort_id = EXCLUDED.cohort_id, objective_version = EXCLUDED.objective_version,
                              updated_at = now()
                RETURNING id
                """,
                {
                    **values,
                    "objective_version": CURRENT_OBJECTIVE_VERSION,
                    "gate_result": Jsonb(values["gate_result"]),
                    "ticket": Jsonb(values["ticket"]),
                },
            ).fetchone()
        return str(row["id"])

    def _stage_order(self, signal: dict[str, Any], *, now: datetime, program: Any) -> dict[str, Any]:
        quote = _contract_quote(signal)
        if quote is None:
            return {
                "signal_id": str(signal["id"]), "status": "blocked",
                "blockers": ["executable_current_contract_quote_required"],
            }
        gate = contract_gate(quote, family=str(signal["strategy_key"]), as_of=now)
        if not gate.eligible:
            return {
                "signal_id": str(signal["id"]), "status": "blocked",
                "blockers": list(gate.blockers),
            }
        event = RecoveryEventState(
            event_id=str(signal["event_id"]), symbol=str(signal["symbol"]),
            reference_price=float(signal["reference_price"]), event_low=float(signal["event_low"]),
            started_at=signal["started_at"], spots=(),
        )
        # Rebuild the order snapshot from the current-session executable quote;
        # a shadow ticket is audit evidence, not permission to stage at an old
        # premium or use stale unit-risk sizing.
        ticket = _one_unit_ticket(
            event=event, family=str(signal["strategy_key"]), quote=quote,
            decision_id=str(signal["decision_id"]), created_at=now,
            lower_confidence_expectancy=signal.get("lower_confidence_expectancy"),
            risk_policy=self.risk_policy,
        )
        unit = float((ticket.get("risk") or {}).get("one_unit_max_loss") or 0.0)
        risk = self._risk_context(
            event_id=str(signal["event_id"]),
            instrument_id=int(signal["instrument_id"]),
            family=str(signal["strategy_key"]),
            now=now,
        )
        decision = size_recovery_position(unit, risk, self.risk_policy)
        if decision.quantity <= 0:
            self._set_signal_status(str(signal["id"]), "risk_blocked", {"risk_blockers": list(decision.blockers)})
            return {"signal_id": str(signal["id"]), "status": "risk_blocked", "blockers": list(decision.blockers)}
        ticket = {
            **ticket,
            "risk": {
                **dict(ticket.get("risk") or {}), **self.risk_policy.snapshot(),
                "total_risk": decision.total_risk,
            },
            "quantity": decision.quantity,
        }
        with self.runtime.transaction(JOB_PROFILE) as connection:
            acquire_shared_sleeve_lock(connection)
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ["recovery-paper-order"])
            session_start = now.astimezone(MARKET_TZ).replace(
                hour=0, minute=0, second=0, microsecond=0,
            ).astimezone(UTC)
            current = connection.execute(
                f"""
                SELECT 1
                FROM analysis.option_event_signal signal
                JOIN analysis.option_event event ON event.id = signal.event_id
                JOIN analysis.option_event_capture capture ON capture.id = signal.capture_id
                WHERE signal.id = %s AND signal.status = 'shadow'
                  AND signal.cohort_id = event.cohort_id
                  AND signal.available_at >= %s - interval '20 minutes'
                  AND signal.available_at <= %s
                  AND capture.status IN ('complete', 'partial')
                  AND capture.finished_at IS NOT NULL AND capture.finished_at <= %s
                  AND event.status = 'active'
                  AND {self.cohorts.current_event_clause()}
                  AND signal.capture_id = (
                    SELECT latest.id
                    FROM analysis.option_event_capture latest
                    WHERE latest.event_id = event.id
                      AND latest.status IN ('complete', 'partial')
                      AND latest.finished_at IS NOT NULL AND latest.finished_at <= %s
                    ORDER BY latest.scheduled_at DESC, latest.id DESC
                    LIMIT 1
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM raw.option_quote quote
                    JOIN raw.option_capture_generation generation
                      ON generation.id = quote.capture_generation_id
                    WHERE quote.contract_id = signal.contract_id
                      AND quote.available_at >= %s AND quote.available_at <= %s
                      AND generation.capture_state IN ('complete', 'partial')
                      AND generation.capture_finished_at <= %s
                      AND quote.bid > 0 AND quote.ask >= quote.bid
                      AND coalesce(quote.bid_size, 0) > 0 AND coalesce(quote.ask_size, 0) > 0
                  )
                FOR UPDATE OF signal, event
                """,
                [signal["id"], now, now, now, now, session_start, now, now],
            ).fetchone()
            if current is None:
                return {
                    "signal_id": str(signal["id"]), "status": "stale",
                    "blockers": ["signal_superseded_or_event_no_longer_stageable"],
                }
            prior = connection.execute(
                "SELECT id, status FROM app.paper_order WHERE event_id = %s AND strategy_family = %s",
                [signal["event_id"], signal["strategy_key"]],
            ).fetchone()
            if prior:
                return {"signal_id": str(signal["id"]), "status": str(prior["status"]), "paper_order_id": str(prior["id"])}
            shared_blockers = shared_sleeve_blockers(
                connection,
                now=now,
                lane="recovery",
                sleeve_capital=self.risk_policy.sleeve_capital,
                daily_loss_halt_pct=self.risk_policy.daily_loss_halt_pct,
                max_open_positions=self.risk_policy.max_open_positions,
            )
            if shared_blockers:
                connection.execute(
                    "UPDATE analysis.option_event_signal SET status = 'risk_blocked', gate_result = gate_result || %s, updated_at = now() WHERE id = %s",
                    [Jsonb({"risk_blockers": shared_blockers}), signal["id"]],
                )
                return {"signal_id": str(signal["id"]), "status": "risk_blocked", "blockers": shared_blockers}
            key = f"recovery:{signal['id']}:v4"
            row = connection.execute(
                """
                INSERT INTO app.paper_order
                    (decision_id, instrument_id, side, quantity, limit_price, status, policy_result,
                     policy_snapshot, lane, structure, idempotency_key, ticket_version, ticket_snapshot, intended_limit_price,
                     event_id, event_signal_id, strategy_family, objective_version, cohort_id, created_at)
                VALUES (%s, %s, 'buy', %s, %s, 'staged', %s, %s, 'recovery', %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                [
                    signal["decision_id"], signal["instrument_id"], decision.quantity,
                    ticket.get("entry", {}).get("limit_price"),
                    Jsonb({
                        "owner": "options_recovery", "live_order_submission": False,
                        "program_eligibility": _json_document(program.as_dict()),
                        "risk_policy": self.risk_policy.snapshot(),
                    }),
                    Jsonb(self.risk_policy.snapshot()), ticket.get("structure"), key, 4, Jsonb(ticket), ticket.get("entry", {}).get("limit_price"),
                    signal["event_id"], signal["id"], signal["strategy_key"], ticket.get("objective_version"),
                    signal["cohort_id"], now,
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


def _json_document(value: Any) -> Any:
    """Make database-sourced IDs and timestamps safe for a JSONB snapshot."""

    return json.loads(json.dumps(value, default=str))
