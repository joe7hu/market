"""Investment-facing read models for the forward options recovery program."""

from __future__ import annotations

from typing import Any, Iterable

from investment_panel.core.options_recovery_registry import strategies
from investment_panel.database.option_events import OptionEventRepository
from investment_panel.database.options_recovery_agents import RecoveryEventAgentRepository
from investment_panel.database.options_recovery_learning import RecoveryLearningRepository
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


class RecoveryReadRepository:
    """Bounded recovery product models; operational diagnostics stay in health()."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def events(
        self,
        *,
        event_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions = ["true"]
        values: list[Any] = []
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
                       event.detected_at, event.started_at, event.reference_price, event.event_low,
                       event.severity_score, event.event_rank, event.material_evidence_count,
                       event.close_reason, event.closed_at,
                       (SELECT count(*) FROM analysis.option_event_capture capture
                        WHERE capture.event_id = event.id AND capture.status = 'complete') AS complete_captures,
                       (SELECT count(*) FROM analysis.option_event_signal signal
                        WHERE signal.event_id = event.id AND signal.status IN ('shadow', 'ticketed', 'entered', 'partial_exited')) AS active_signals,
                       (SELECT count(*) FROM app.paper_order paper
                        WHERE paper.event_id = event.id AND paper.status IN ('staged', 'entered', 'partial_exited')) AS open_paper_orders
                FROM analysis.option_event event
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                WHERE {' AND '.join(conditions)}
                ORDER BY CASE event.status WHEN 'active' THEN 0 WHEN 'deferred_capacity' THEN 1 ELSE 2 END,
                         event.started_at DESC
                LIMIT %s OFFSET %s
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def event_detail(self, event_id: str) -> dict[str, Any] | None:
        rows = self.events(event_id=event_id, limit=1)
        if not rows:
            return None
        event = rows[0]
        with self.runtime.read(JOB_PROFILE) as connection:
            captures = [dict(row) for row in connection.execute(
                """
                SELECT id::text AS capture_id, scheduled_at, started_at, finished_at, status,
                       expected_contract_count, received_contract_count, completeness, continuity_pct
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
            row = connection.execute(
                """
                SELECT count(*) AS observed,
                       count(*) FILTER (WHERE outcome_classification <> 'unmeasurable') AS measurable,
                       count(*) FILTER (WHERE signal_id IS NOT NULL) AS signaled,
                       count(*) FILTER (WHERE paper_order_id IS NOT NULL OR selection_stage IN ('ticketed', 'filled', 'exited')) AS ticketed,
                       count(*) FILTER (WHERE entry_fill_at IS NOT NULL) AS filled,
                       count(*) FILTER (WHERE outcome_classification = 'captured') AS exited
                FROM analysis.option_opportunity_observation
                """
            ).fetchone()
        return {"stages": [{"stage": key, "count": int(row[key] or 0)} for key in (
            "observed", "measurable", "signaled", "ticketed", "filled", "exited",
        )]}

    def opportunities(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
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
                ORDER BY observation.event_id, observation.contract_id, observation.strategy_key,
                         observation.available_at DESC
                LIMIT %s
                """,
                [max(1, min(int(limit), 500))],
            ).fetchall()
        return [dict(row) for row in rows]

    def family_performance(self) -> list[dict[str, Any]]:
        return [RecoveryLearningRepository(self.runtime).metrics(strategy.key) for strategy in strategies()]

    def agent_provenance(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return RecoveryEventAgentRepository(self.runtime).provenance(limit=limit)

    def health(self) -> dict[str, Any]:
        """The only recovery read model that contains capture/lease/storage diagnostics."""

        capture = OptionEventRepository(self.runtime).capture_health()
        with self.runtime.read(JOB_PROFILE) as connection:
            storage = connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM analysis.option_event) AS events,
                  (SELECT count(*) FROM analysis.option_event_capture) AS captures,
                  (SELECT count(*) FROM analysis.option_event_contract) AS contracts,
                  (SELECT count(*) FROM analysis.option_opportunity_observation) AS observations,
                  (SELECT count(*) FROM app.paper_order WHERE event_id IS NOT NULL) AS recovery_paper_orders
                """
            ).fetchone()
        return {"capture": capture, "storage": {key: int(storage[key] or 0) for key in storage.keys()}}

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
            row = connection.execute(
                """
                SELECT ticket FROM analysis.option_event_signal
                WHERE decision_id = %s::uuid AND (ticket->>'ticket_version') = '4'
                ORDER BY available_at DESC LIMIT 1
                """,
                [decision_id],
            ).fetchone()
        return dict(row["ticket"] or {}) if row else None


def _outcome_counts(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    names = ("captured", "missed", "unfilled", "unmeasurable", "observing")
    return {name: sum(str(row.get("outcome_classification") or "") == name for row in rows) for name in names}
