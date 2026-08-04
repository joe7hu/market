"""Cohort-scoped recovery-agent telemetry and provenance read models."""

from __future__ import annotations

from typing import Any

from investment_panel.core.options_recovery_agents import MUTATION_DRAFTER, RECOVERY_AGENT_ROLES
from investment_panel.database.options_recovery_agent_support import empty_telemetry, number, ratio, role
from investment_panel.database.runtime import JOB_PROFILE


RECOVERY_TASK_KINDS = tuple(f"options_recovery_{item}" for item in RECOVERY_AGENT_ROLES)


class RecoveryAgentReporting:
    """Mixin keeping agent reports inside the current valid recovery cohort."""

    def telemetry(self) -> dict[str, Any]:
        with self.runtime.read(JOB_PROFILE) as connection:
            cohort = self.cohorts.current(connection)
            if cohort is None:
                return empty_telemetry()
            batch = connection.execute(
                f"""
                SELECT count(*) AS total, count(*) FILTER (WHERE batch.status = 'failed') AS failed,
                       avg(extract(epoch FROM (batch.finished_at - batch.started_at))) FILTER (WHERE batch.finished_at IS NOT NULL) AS latency
                FROM analysis.option_event_agent_batch batch
                JOIN analysis.option_event event ON event.id = batch.event_id
                WHERE batch.cohort_id = %s::uuid
                  AND {self.cohorts.current_event_clause()}
                """,
                [cohort["id"]],
            ).fetchone()
            tasks = connection.execute(
                f"""
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE validation->>'evidence_valid' = 'true') AS evidence_valid,
                       count(*) FILTER (WHERE validation->>'mutation_status' LIKE 'rejected%%') AS rejected_mutation,
                       count(*) FILTER (WHERE task_kind = %s) AS mutation_tasks
                FROM analysis.agent_task
                WHERE task_kind = ANY(%s)
                  AND request->>'recovery_event_batch_id' IN (
                    SELECT batch.id::text
                    FROM analysis.option_event_agent_batch batch
                    JOIN analysis.option_event event ON event.id = batch.event_id
                    WHERE batch.cohort_id = %s::uuid
                      AND {self.cohorts.current_event_clause()}
                  )
                """,
                [f"options_recovery_{MUTATION_DRAFTER}", list(RECOVERY_TASK_KINDS), cohort["id"]],
            ).fetchone()
            usage = connection.execute(
                """
                SELECT coalesce(sum(input_tokens), 0) AS input_tokens, coalesce(sum(output_tokens), 0) AS output_tokens
                FROM analysis.agent_run
                WHERE trigger LIKE 'options_recovery:%%'
                  AND summary->>'cohort_id' = %s
                """,
                [str(cohort["id"])],
            ).fetchone()
            lift = connection.execute(
                f"""
                WITH event_returns AS (
                  SELECT observation.event_id, avg(observation.realized_return) AS realized_return
                  FROM analysis.option_opportunity_observation observation
                  JOIN analysis.option_event event ON event.id = observation.event_id
                  WHERE observation.realized_return IS NOT NULL AND observation.cohort_id = %s::uuid
                    AND {self.cohorts.current_event_clause()}
                  GROUP BY observation.event_id
                ), agent_events AS (
                  SELECT DISTINCT batch.event_id
                  FROM analysis.option_event_agent_batch batch
                  JOIN analysis.option_event event ON event.id = batch.event_id
                  WHERE batch.status = 'completed' AND batch.cohort_id = %s::uuid
                    AND {self.cohorts.current_event_clause()}
                )
                SELECT avg(realized_return) FILTER (WHERE event_id IN (SELECT event_id FROM agent_events)) AS with_agent,
                       avg(realized_return) FILTER (WHERE event_id NOT IN (SELECT event_id FROM agent_events)) AS deterministic_only,
                       count(*) FILTER (WHERE event_id IN (SELECT event_id FROM agent_events)) AS with_agent_count,
                       count(*) FILTER (WHERE event_id NOT IN (SELECT event_id FROM agent_events)) AS deterministic_only_count
                FROM event_returns
                """,
                [cohort["id"], cohort["id"]],
            ).fetchone()
        with_agent = number(lift["with_agent"])
        deterministic_only = number(lift["deterministic_only"])
        advisory_lift = with_agent - deterministic_only if with_agent is not None and deterministic_only is not None else None
        return {
            "cohort_id": str(cohort["id"]),
            "code_version": str(cohort["code_version"]),
            "batches": int(batch["total"] or 0),
            "agent_failure_rate": ratio(batch["failed"], batch["total"]),
            "evidence_validation_rate": ratio(tasks["evidence_valid"], tasks["total"]),
            "unsupported_proposal_rate": ratio(tasks["rejected_mutation"], tasks["mutation_tasks"]),
            "latency_seconds": number(batch["latency"]),
            "token_usage": {"input_tokens": int(usage["input_tokens"] or 0), "output_tokens": int(usage["output_tokens"] or 0)},
            "advisory_lift_vs_deterministic_only": advisory_lift,
            "advisory_lift_sample": {
                "with_agent_events": int(lift["with_agent_count"] or 0),
                "deterministic_only_events": int(lift["deterministic_only_count"] or 0),
            },
        }

    def provenance(self, *, event_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Bounded event/batch/task provenance for the product surface."""

        cohort_id = self.cohorts.current_id()
        if cohort_id is None:
            return []
        where = f"WHERE batch.cohort_id = %s::uuid AND {self.cohorts.current_event_clause(alias='event')}"
        values: list[Any] = [cohort_id]
        if event_id:
            where += " AND batch.event_id = %s"
            values.append(event_id)
        values.append(max(1, min(int(limit), 500)))
        with self.runtime.read(JOB_PROFILE) as connection:
            batches = [dict(row) for row in connection.execute(
                f"""
                SELECT batch.*, instrument.symbol
                FROM analysis.option_event_agent_batch batch
                JOIN analysis.option_event event ON event.id = batch.event_id
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                {where}
                ORDER BY batch.created_at DESC LIMIT %s
                """,
                values,
            ).fetchall()]
            if not batches:
                return []
            task_rows = [dict(row) for row in connection.execute(
                """
                SELECT id, task_kind, status, request, result, validation, created_at, updated_at
                FROM analysis.agent_task
                WHERE request->>'recovery_event_batch_id' = ANY(%s)
                ORDER BY created_at, id
                """,
                [[str(batch["id"]) for batch in batches]],
            ).fetchall()]
        by_batch: dict[str, list[dict[str, Any]]] = {}
        for task in task_rows:
            identifier = str((task.get("request") or {}).get("recovery_event_batch_id") or "")
            by_batch.setdefault(identifier, []).append({
                "id": str(task["id"]), "role": role(task), "status": task["status"],
                "result": task.get("result"), "validation": task.get("validation"),
                "created_at": task["created_at"], "updated_at": task["updated_at"],
            })
        return [{**batch, "id": str(batch["id"]), "event_id": str(batch["event_id"]), "tasks": by_batch.get(str(batch["id"]), [])} for batch in batches]
