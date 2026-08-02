"""PostgreSQL-owned accounting for every external agent invocation."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


class AgentTelemetryRepository:
    """Record actual process invocations, independent of workflow adapters."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def start(
        self,
        *,
        workflow: str,
        provider: str,
        model: str,
        trigger: str,
        summary: dict[str, Any] | None = None,
    ) -> str:
        details = {"workflow": workflow, "invoked": True, **(summary or {})}
        with self.runtime.transaction(JOB_PROFILE) as connection:
            row = connection.execute(
                "INSERT INTO analysis.agent_run (provider, model, trigger, started_at, status, summary) "
                "VALUES (%s, %s, %s, now(), 'running', %s) RETURNING id",
                [provider, model, trigger, Jsonb(details)],
            ).fetchone()
        return str(row["id"])

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        usage = usage or {}
        details = {**(summary or {})}
        if error:
            details["error"] = error
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                "UPDATE analysis.agent_run SET status = %s, finished_at = now(), "
                "input_tokens = %s, output_tokens = %s, summary = summary || %s WHERE id = %s",
                [
                    status,
                    int(usage.get("input_tokens") or 0) or None,
                    int(usage.get("output_tokens") or 0) or None,
                    Jsonb(details),
                    run_id,
                ],
            )
