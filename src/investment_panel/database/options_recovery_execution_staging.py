"""Current-quote paper-staging gate for recovery execution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from investment_panel.core.decision import MARKET_TZ
from investment_panel.core.options_recovery_registry import contract_gate
from investment_panel.database.options_recovery_execution_support import (
    contract_quote as _contract_quote,
    utc as _utc,
)
from investment_panel.database.runtime import JOB_PROFILE


class RecoveryOrderStaging:
    """Mixin that selects only current, executable recovery shadow signals."""

    def stage_qualified_orders(
        self,
        event_id: str,
        *,
        now: datetime | None = None,
        enabled: bool = False,
    ) -> dict[str, Any]:
        """Stage only a current-cohort signal after the global canary is green."""

        reference = _utc(now) or datetime.now(UTC)
        eligibility = self.cohorts.program_eligibility(
            recovery_paper_actions_enabled=enabled, now=reference,
        )
        if not eligibility.eligible:
            status = "disabled" if not enabled else "blocked"
            return {
                "status": status, "event_id": event_id, "orders": [],
                "blockers": list(eligibility.blockers), "program": eligibility.as_dict(),
            }
        cohort = eligibility.cohort
        assert cohort is not None
        session_start = reference.astimezone(MARKET_TZ).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).astimezone(UTC)
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = [dict(row) for row in connection.execute(
                f"""
                SELECT DISTINCT ON (signal.strategy_key)
                       signal.id, signal.decision_id, signal.strategy_key, signal.contract_id,
                       signal.event_contract_id, signal.cohort_id,
                       signal.available_at AS signal_available_at,
                       signal.lower_confidence_expectancy,
                       event.id AS event_id, event.cohort_id, event.started_at, event.instrument_id,
                       event.reference_price, event.event_low, event.reference_available_at,
                       event.reference_source_id, event.quote_age_minutes, instrument.symbol,
                       contract.expiration, contract.strike, contract.option_type, contract.provider_symbols,
                       quote.bid, quote.ask, quote.bid_size, quote.ask_size, quote.open_interest,
                       quote.provider_delta, quote.volume, quote.observed_at, quote.available_at
                FROM analysis.option_event_signal signal
                JOIN analysis.option_event event ON event.id = signal.event_id
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                JOIN analysis.option_event_capture capture ON capture.id = signal.capture_id
                JOIN analysis.option_event_contract event_contract
                  ON event_contract.id = signal.event_contract_id
                 AND event_contract.retired_at IS NULL
                JOIN catalog.option_contract contract ON contract.id = signal.contract_id
                JOIN LATERAL (
                  SELECT quote.bid, quote.ask, quote.bid_size, quote.ask_size, quote.open_interest,
                         quote.provider_delta, quote.volume, quote.observed_at, quote.available_at
                  FROM raw.option_quote quote
                  JOIN raw.option_capture_generation generation
                    ON generation.id = quote.capture_generation_id
                  WHERE quote.contract_id = signal.contract_id
                    AND quote.available_at >= %s AND quote.available_at <= %s
                    AND generation.capture_state IN ('complete', 'partial')
                    AND generation.capture_finished_at <= %s
                    AND quote.bid > 0 AND quote.ask >= quote.bid
                    AND coalesce(quote.bid_size, 0) > 0 AND coalesce(quote.ask_size, 0) > 0
                  ORDER BY quote.available_at DESC, quote.observed_at DESC LIMIT 1
                ) quote ON true
                WHERE signal.event_id = %s AND signal.status = 'shadow'
                  AND signal.cohort_id = %s::uuid
                  AND signal.available_at >= %s - interval '20 minutes'
                  AND signal.available_at <= %s
                  AND capture.status IN ('complete', 'partial')
                  AND capture.finished_at IS NOT NULL AND capture.finished_at <= %s
                  AND event.status = 'active'
                  AND {self.cohorts.current_event_clause()}
                  AND event.reference_price > 0
                  AND event.reference_source_id IS NOT NULL
                  AND event.reference_available_at IS NOT NULL
                  AND event.quote_age_minutes IS NOT NULL AND event.quote_age_minutes <= 10.0
                  AND signal.capture_id = (
                    SELECT latest.id
                    FROM analysis.option_event_capture latest
                    WHERE latest.event_id = event.id
                      AND latest.status IN ('complete', 'partial')
                      AND latest.finished_at IS NOT NULL AND latest.finished_at <= %s
                    ORDER BY latest.scheduled_at DESC, latest.id DESC
                    LIMIT 1
                  )
                ORDER BY signal.strategy_key, signal.available_at DESC,
                         signal.selection_score DESC NULLS LAST, signal.id DESC
                """,
                [session_start, reference, reference, event_id, cohort["id"], reference, reference, reference, reference],
            ).fetchall()]
        orders: list[dict[str, Any]] = []
        for signal in rows:
            quote = _contract_quote(signal)
            gate = contract_gate(quote, family=str(signal["strategy_key"]), as_of=reference) if quote else None
            if gate is None or not gate.eligible:
                orders.append({
                    "signal_id": str(signal["id"]), "status": "blocked",
                    "blockers": list(gate.blockers) if gate else ["executable_current_contract_quote_required"],
                })
                continue
            orders.append(self._stage_order(signal, now=reference, program=eligibility))
        return {
            "status": "ok", "event_id": event_id, "orders": orders,
            "program": eligibility.as_dict(),
        }
