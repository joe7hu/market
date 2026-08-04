"""Investment-facing read models for the forward options recovery program."""

from __future__ import annotations

from typing import Any, Iterable

from investment_panel.core.options_recovery_registry import strategies
from investment_panel.database.option_events import OptionEventRepository
from investment_panel.database.options_recovery_agents import RecoveryEventAgentRepository
from investment_panel.database.options_recovery_cohorts import RecoveryCohortRepository
from investment_panel.database.options_recovery_learning import RecoveryLearningRepository
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


class RecoveryReadRepository:
    """Bounded recovery product models; operational diagnostics stay in health()."""

    def __init__(self, runtime: DatabaseRuntime, *, recovery_paper_actions_enabled: bool = False) -> None:
        self.runtime = runtime
        self.cohorts = RecoveryCohortRepository(runtime)
        self.recovery_paper_actions_enabled = recovery_paper_actions_enabled

    def events(
        self,
        *,
        event_id: str | None = None,
        status: str | None = None,
        cohort: str | None = None,
        include_invalidated: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        values: list[Any] = []
        if cohort:
            # Audit access is deliberate: callers must name a cohort and
            # explicitly include invalidated rows (with either audit flag).
            conditions.append("(cohort.id::text = %s OR cohort.objective_version = %s)")
            values.extend([cohort, cohort])
        else:
            conditions.append(self.cohorts.current_event_clause())
        if not (include_invalidated or status == "invalidated"):
            conditions.append("event.status <> 'invalidated'")
        if event_id:
            conditions.append("event.id = %s")
            values.append(event_id)
        if status:
            conditions.append("event.status = %s")
            values.append(status)
        values.extend([max(1, min(int(limit), 250)), max(0, int(offset))])
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                f"""
                SELECT event.id::text AS event_id, instrument.symbol, event.event_type, event.status,
                       event.cohort_id::text AS cohort_id, cohort.objective_version AS cohort,
                       event.detected_at, event.started_at, event.reference_price, event.event_low,
                       event.severity_score, event.event_rank, event.material_evidence_count,
                       event.close_reason, event.closed_at, event.trigger_reason,
                       event.trigger_intraday_pct, event.trigger_one_day_pct, event.trigger_three_session_pct,
                       event.quote_age_minutes, event.reference_trading_date, event.reference_source_id,
                       event.reference_available_at, event.data_quality_status, event.priority_components,
                       event.capacity_defer_reason, event.invalidation_reason, event.invalidated_at,
                       (SELECT count(*) FROM analysis.option_event_capture capture
                        WHERE capture.event_id = event.id AND capture.status = 'complete') AS complete_captures,
                       (SELECT count(*) FROM analysis.option_event_signal signal
                        WHERE signal.event_id = event.id AND signal.status IN ('shadow', 'ticketed', 'entered', 'partial_exited')) AS active_signals,
                       (SELECT count(*) FROM app.paper_order paper
                        WHERE paper.event_id = event.id AND paper.status IN ('staged', 'entered', 'partial_exited')) AS open_paper_orders
                FROM analysis.option_event event
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                JOIN analysis.option_recovery_cohort cohort ON cohort.id = event.cohort_id
                WHERE {' AND '.join(conditions) if conditions else 'true'}
                ORDER BY CASE event.status WHEN 'active' THEN 0 WHEN 'deferred_capacity' THEN 1 ELSE 2 END,
                         event.started_at DESC
                LIMIT %s OFFSET %s
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def event_detail(
        self,
        event_id: str,
        *,
        cohort: str | None = None,
        include_invalidated: bool = False,
    ) -> dict[str, Any] | None:
        rows = self.events(
            event_id=event_id,
            cohort=cohort,
            include_invalidated=include_invalidated,
            limit=1,
        )
        if not rows:
            return None
        event = rows[0]
        with self.runtime.read(JOB_PROFILE) as connection:
            captures = [dict(row) for row in connection.execute(
                f"""
                SELECT id::text AS capture_id, scheduled_at, started_at, finished_at, status,
                       expected_contract_count, received_contract_count, completeness, continuity_pct,
                       canonical_continuity_pct, original_continuity_pct, details
                FROM analysis.option_event_capture WHERE event_id = %s
                ORDER BY scheduled_at DESC LIMIT 160
                """,
                [event_id],
            ).fetchall()]
            signals = [dict(row) for row in connection.execute(
                """
                SELECT signal.id::text AS signal_id, signal.decision_id::text AS decision_id,
                       signal.strategy_key AS family, signal.signal_at, signal.available_at,
                       signal.selection_score, signal.lower_confidence_expectancy,
                       signal.maximum_loss, signal.status, signal.ticket,
                       contract.expiration, contract.strike, contract.option_type,
                       contract.provider_symbols
                FROM analysis.option_event_signal signal
                JOIN catalog.option_contract contract ON contract.id = signal.contract_id
                WHERE signal.event_id = %s
                ORDER BY signal.available_at DESC, signal.id DESC LIMIT 100
                """,
                [event_id],
            ).fetchall()]
            opportunities = [dict(row) for row in connection.execute(
                """
                SELECT DISTINCT ON (observation.contract_id, observation.strategy_key)
                       observation.id::text AS observation_id, observation.strategy_key AS family,
                       observation.selection_stage, observation.miss_reason,
                       observation.outcome_classification, observation.return_1_session,
                       observation.return_3_session, observation.return_5_session,
                       observation.return_10_session, observation.time_to_2x_sessions,
                       observation.time_to_3x_sessions, observation.time_to_4x_sessions,
                       observation.executable_peak_return, observation.realized_return,
                       observation.mae, observation.giveback, observation.exit_efficiency,
                       observation.available_at, contract.expiration, contract.strike,
                       contract.option_type, contract.provider_symbols
                FROM analysis.option_opportunity_observation observation
                JOIN catalog.option_contract contract ON contract.id = observation.contract_id
                WHERE observation.event_id = %s
                ORDER BY observation.contract_id, observation.strategy_key, observation.available_at DESC
                LIMIT 200
                """,
                [event_id],
            ).fetchall()]
        return {
            "event": event,
            "captures": captures,
            "signals": signals,
            "opportunities": opportunities,
            "outcomes": _outcome_counts(opportunities),
            "agent_provenance": RecoveryEventAgentRepository(self.runtime).provenance(event_id=event_id, limit=24),
        }

    def funnel(self) -> dict[str, Any]:
        with self.runtime.read(JOB_PROFILE) as connection:
            cohort = self.cohorts.current(connection)
            if cohort is None:
                return {"stages": [{"stage": key, "count": 0} for key in (
                    "observed", "measurable", "signaled", "ticketed", "filled", "exited",
                )]}
            row = connection.execute(
                f"""
                SELECT count(*) AS observed,
                       count(*) FILTER (WHERE outcome_classification <> 'unmeasurable') AS measurable,
                       count(*) FILTER (WHERE signal_id IS NOT NULL) AS signaled,
                       count(*) FILTER (WHERE paper_order_id IS NOT NULL OR selection_stage IN ('ticketed', 'filled', 'exited')) AS ticketed,
                       count(*) FILTER (WHERE entry_fill_at IS NOT NULL) AS filled,
                       count(*) FILTER (WHERE outcome_classification = 'captured') AS exited
                FROM analysis.option_opportunity_observation observation
                JOIN analysis.option_event event ON event.id = observation.event_id
                WHERE observation.cohort_id = %s::uuid
                  AND {self.cohorts.current_event_clause()}
                """
                , [cohort["id"]]
            ).fetchone()
        return {"stages": [{"stage": key, "count": int(row[key] or 0)} for key in (
            "observed", "measurable", "signaled", "ticketed", "filled", "exited",
        )]}

    def opportunities(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self.runtime.read(JOB_PROFILE) as connection:
            cohort = self.cohorts.current(connection)
            if cohort is None:
                return []
            rows = connection.execute(
                f"""
                SELECT DISTINCT ON (observation.event_id, observation.contract_id, observation.strategy_key)
                       observation.id::text AS observation_id, observation.event_id::text AS event_id,
                       instrument.symbol, observation.strategy_key AS family,
                       observation.selection_stage, observation.miss_reason,
                       observation.outcome_classification, observation.data_status,
                       observation.return_1_session, observation.return_3_session,
                       observation.return_5_session, observation.return_10_session,
                       observation.time_to_2x_sessions, observation.time_to_3x_sessions,
                       observation.time_to_4x_sessions, observation.executable_peak_return,
                       observation.realized_return, observation.mae, observation.giveback,
                       observation.exit_efficiency, observation.available_at,
                       contract.expiration, contract.strike, contract.option_type, contract.provider_symbols
                FROM analysis.option_opportunity_observation observation
                JOIN analysis.option_event event ON event.id = observation.event_id
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                JOIN catalog.option_contract contract ON contract.id = observation.contract_id
                WHERE observation.cohort_id = %s::uuid
                  AND {self.cohorts.current_event_clause()}
                ORDER BY observation.event_id, observation.contract_id, observation.strategy_key,
                         observation.available_at DESC
                LIMIT %s
                """,
                [cohort["id"], max(1, min(int(limit), 500))],
            ).fetchall()
        return [dict(row) for row in rows]

    def family_performance(self) -> list[dict[str, Any]]:
        return [RecoveryLearningRepository(self.runtime).metrics(strategy.key) for strategy in strategies()]

    def agent_provenance(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return RecoveryEventAgentRepository(self.runtime).provenance(limit=limit)

    def health(self) -> dict[str, Any]:
        """The only recovery read model that contains capture/lease/storage diagnostics."""

        capture = OptionEventRepository(self.runtime).capture_health()
        program = self.cohorts.health(
            recovery_paper_actions_enabled=self.recovery_paper_actions_enabled,
        )
        with self.runtime.read(JOB_PROFILE) as connection:
            cohort = self.cohorts.current(connection)
            if cohort is None:
                return {
                    "capture": capture, "program": program,
                    "storage": {"events": 0, "captures": 0, "contracts": 0, "observations": 0, "recovery_paper_orders": 0},
                    "detector": {"runs": 0, "failed_runs": 0, "provider_failures": []},
                    "event_session_quality": [], "agent_telemetry": {},
                }
            storage = connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM analysis.option_event WHERE cohort_id = %s::uuid) AS events,
                  (SELECT count(*) FROM analysis.option_event_capture capture JOIN analysis.option_event event ON event.id = capture.event_id WHERE event.cohort_id = %s::uuid) AS captures,
                  (SELECT count(*) FROM analysis.option_event_contract contract JOIN analysis.option_event event ON event.id = contract.event_id WHERE event.cohort_id = %s::uuid) AS contracts,
                  (SELECT count(*) FROM analysis.option_opportunity_observation WHERE cohort_id = %s::uuid) AS observations,
                  (SELECT count(*) FROM app.paper_order WHERE cohort_id = %s::uuid) AS recovery_paper_orders
                """,
                [cohort["id"], cohort["id"], cohort["id"], cohort["id"], cohort["id"]],
            ).fetchone()
            detector = connection.execute(
                """
                SELECT count(*) AS runs, count(*) FILTER (WHERE status = 'failed') AS failed_runs,
                       max(quote_age_p95_minutes) AS worst_quote_age_p95,
                       coalesce(sum(coalesce((details->>'triggering_quote_count')::integer, 0)), 0) AS triggering_quotes,
                       coalesce(sum(coalesce((details->>'fresh_triggering_quote_count')::integer, 0)), 0) AS fresh_triggering_quotes
                FROM analysis.option_event_detector_run run
                WHERE run.cohort_id = %s::uuid
                """,
                [cohort["id"]],
            ).fetchone()
            failures = connection.execute(
                """
                SELECT coalesce(jsonb_agg(DISTINCT reason.value), '[]'::jsonb) AS provider_failures
                FROM analysis.option_event_detector_run run
                CROSS JOIN LATERAL jsonb_array_elements_text(run.failure_reasons) AS reason(value)
                WHERE run.cohort_id = %s::uuid
                """,
                [cohort["id"]],
            ).fetchone()
            quality = [dict(row) for row in connection.execute(
                """
                SELECT event_id::text AS event_id, trading_date, scheduled_slots, usable_slots,
                       contract_completeness, canonical_continuity, original_continuity,
                       capture_p95_latency_minutes, data_defects, qualification_result, qualification_reasons
                FROM analysis.option_recovery_event_session_quality
                WHERE cohort_id = %s::uuid
                ORDER BY trading_date DESC, computed_at DESC LIMIT 80
                """,
                [cohort["id"]],
            ).fetchall()]
        latest_program = dict((program.get("paper_staging") or {}).get("latest_program_session") or {})
        scheduled_runs = int(latest_program.get("detector_scheduled_runs") or 0)
        succeeded_runs = int(latest_program.get("detector_succeeded_runs") or 0)
        expected_symbols = int(latest_program.get("provider_expected_symbols") or 0)
        received_symbols = int(latest_program.get("provider_received_symbols") or 0)
        triggering_quotes = int(detector["triggering_quotes"] or 0)
        fresh_triggering_quotes = int(detector["fresh_triggering_quotes"] or 0)
        return {
            "capture": capture,
            "program": program,
            "storage": {key: int(storage[key] or 0) for key in storage.keys()},
            "detector": {
                "runs": int(detector["runs"] or 0),
                "failed_runs": int(detector["failed_runs"] or 0),
                "worst_quote_age_p95_minutes": detector["worst_quote_age_p95"],
                "scheduled_runs": scheduled_runs,
                "succeeded_runs": succeeded_runs,
                "run_coverage": succeeded_runs / scheduled_runs if scheduled_runs else None,
                "provider_expected_symbols": expected_symbols,
                "provider_received_symbols": received_symbols,
                "provider_response_coverage": received_symbols / expected_symbols if expected_symbols else None,
                "triggering_quotes": triggering_quotes,
                "fresh_triggering_quotes": fresh_triggering_quotes,
                "all_triggering_quotes_fresh": (
                    fresh_triggering_quotes == triggering_quotes if triggering_quotes else True
                ),
                "provider_failures": list(failures["provider_failures"] or []),
            },
            "event_session_quality": quality,
            "agent_telemetry": RecoveryEventAgentRepository(self.runtime).telemetry(),
        }

    def panel_models(self, names: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        requested = set(names)
        output: dict[str, list[dict[str, Any]]] = {}
        if "option_recovery_funnel" in requested:
            output["option_recovery_funnel"] = [self.funnel()]
        if "option_recovery_event" in requested:
            output["option_recovery_event"] = self.events(limit=100)
        if "option_recovery_opportunity" in requested:
            output["option_recovery_opportunity"] = self.opportunities(limit=250)
        if "option_recovery_family_performance" in requested:
            output["option_recovery_family_performance"] = self.family_performance()
        if "option_recovery_agent_provenance" in requested:
            output["option_recovery_agent_provenance"] = self.agent_provenance(limit=100)
        if "option_recovery_health" in requested:
            output["option_recovery_health"] = [self.health()]
        return output

    def ticket(self, decision_id: str) -> dict[str, Any] | None:
        with self.runtime.read(JOB_PROFILE) as connection:
            cohort = self.cohorts.current(connection)
            if cohort is None:
                return None
            row = connection.execute(
                """
                SELECT ticket FROM analysis.option_event_signal
                WHERE decision_id = %s::uuid AND (ticket->>'ticket_version') = '4'
                  AND cohort_id = %s::uuid
                ORDER BY available_at DESC LIMIT 1
                """,
                [decision_id, cohort["id"]],
            ).fetchone()
        return dict(row["ticket"] or {}) if row else None


def _outcome_counts(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    names = ("captured", "missed", "unfilled", "unmeasurable", "observing")
    return {name: sum(str(row.get("outcome_classification") or "") == name for row in rows) for name in names}
