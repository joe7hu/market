"""PostgreSQL option-agent task queue and execution contract."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
import subprocess
from typing import Any, Sequence
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.agent_context import option_opportunity_context, ticker_context
from investment_panel.database.agent_candidate_queue import current_candidate_payloads
from investment_panel.database.agent_process import (
    agent_env,
    agent_error_meta,
    command_args as _command_args,
    jsonable as _jsonable,
    market_day_start_utc,
    validate_result as _validate_result,
)
from investment_panel.database.option_thesis_materialization import (
    accept_agent_task_result,
    option_thesis_materialization_summary,
)
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE

class AgentRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def queue_thesis(
        self,
        ticker: str,
        *,
        prompt: str = "",
        trigger: str = "ondemand",
        context: dict[str, Any] | None = None,
        context_sources: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        symbol = str(ticker).strip().upper()
        if not symbol:
            raise ValueError("ticker is required")
        with self.runtime.transaction() as connection:
            instrument = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = %s", [symbol]).fetchone()
            published_decision_id = str((context or {}).get("decision_id") or "").strip() or None
            decision = connection.execute(
                """
                SELECT decision.id, decision.state, decision.score, decision.reasons,
                       decision.blockers, decision.as_of
                FROM analysis.decision decision
                JOIN catalog.instrument candidate ON candidate.id = decision.instrument_id
                WHERE candidate.symbol = %s
                  AND (CAST(%s AS uuid) IS NULL OR decision.id = CAST(%s AS uuid))
                ORDER BY decision.as_of DESC, decision.score DESC NULLS LAST LIMIT 1
                """,
                [symbol, published_decision_id, published_decision_id],
            ).fetchone()
            if published_decision_id and decision is None:
                raise ValueError(f"published decision not found for {symbol}: {published_decision_id}")
            existing = connection.execute(
                """
                SELECT id, request, status FROM analysis.agent_task
                WHERE task_kind = 'option_thesis'
                  AND request->>'ticker' = %s AND request->>'trigger' = %s
                  AND (
                      status IN ('queued', 'running')
                      OR (%s = 'scheduled' AND created_at::date = (now() AT TIME ZONE 'America/New_York')::date)
                  )
                ORDER BY created_at DESC LIMIT 1
                """,
                [symbol, trigger, trigger],
            ).fetchone()
            if existing:
                return {"request_id": str(existing["id"]), "status": existing["status"], **dict(existing["request"])}
            resolved_context = ticker_context(connection, symbol, context_sources=context_sources)
            if context:
                resolved_context["option_opportunity"] = option_opportunity_context(context)
            request = {
                "ticker": symbol,
                "trigger": trigger,
                "custom_prompt": prompt,
                "instrument_id": instrument["id"] if instrument else None,
                "decision": (
                    dict(decision)
                    if decision and (context_sources or {}).get("decision", True)
                    else {}
                ),
                "context": _jsonable(resolved_context),
                "authority": "hypothesis_only",
            }
            row = connection.execute(
                """
                INSERT INTO analysis.agent_task (decision_id, task_kind, status, request)
                VALUES (%s, 'option_thesis', 'queued', %s)
                RETURNING id
                """,
                [decision["id"] if decision else None, Jsonb(_jsonable(request))],
            ).fetchone()
        return {"request_id": str(row["id"]), "status": "queued", **request}

    def queue_current_candidates(
        self,
        *,
        limit: int = 8,
        trigger: str = "scheduled",
        context_sources: dict[str, bool] | None = None,
    ) -> int:
        """Queue the current ranked option candidates with their published context."""
        queued = 0
        for context in current_candidate_payloads(self.runtime, limit=limit):
            symbol = str(context.get("ticker") or context.get("symbol") or "").upper()
            if not symbol:
                continue
            result = self.queue_thesis(
                symbol, trigger=trigger, context=context, context_sources=context_sources,
            )
            if result.get("status") == "queued":
                queued += 1
        return queued

    def queue_postmortem(self, decision_id: str | UUID, *, reason: str) -> dict[str, Any]:
        with self.runtime.transaction() as connection:
            decision = connection.execute(
                """
                SELECT decision.id, decision.decision_key, decision.state, decision.score,
                       decision.reasons, decision.blockers, instrument.symbol AS ticker,
                       outcome.maturity_state, outcome.observed_through, outcome.current_return,
                       outcome.return_1d, outcome.return_5d, outcome.return_20d,
                       outcome.return_60d, outcome.peak_return, outcome.max_drawdown
                FROM analysis.decision decision
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                LEFT JOIN analysis.option_outcome outcome ON outcome.decision_id = decision.id
                WHERE decision.id = %s
                """,
                [decision_id],
            ).fetchone()
            if decision is None:
                raise ValueError(f"decision not found: {decision_id}")
            request = {"decision_id": str(decision["id"]), "reason": reason, "decision": dict(decision), "authority": "proposal_only"}
            row = connection.execute(
                "INSERT INTO analysis.agent_task (decision_id, task_kind, status, request) "
                "VALUES (%s, 'option_postmortem', 'queued', %s) RETURNING id",
                [decision["id"], Jsonb(_jsonable(request))],
            ).fetchone()
        return {"request_id": str(row["id"]), "status": "queued", **request}

    def queue_current_postmortems(self, *, limit: int = 4) -> int:
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT outcome.decision_id, outcome.maturity_state
                FROM analysis.option_outcome outcome
                WHERE outcome.maturity_state IN ('mature', 'expired')
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis.agent_task task
                      WHERE task.task_kind = 'option_postmortem'
                        AND task.request->>'decision_id' = outcome.decision_id::text
                        AND task.status IN ('queued', 'running', 'completed')
                  )
                ORDER BY outcome.updated_at DESC LIMIT %s
                """,
                [limit],
            ).fetchall()
        for row in rows:
            self.queue_postmortem(
                row["decision_id"], reason=f"terminal outcome: {row['maturity_state']}",
            )
        return len(rows)

    def submit(self, task_kind: str, payload: dict[str, Any]) -> str:
        with self.runtime.transaction() as connection:
            return self._submit_in_transaction(connection, task_kind, payload)

    def submit_postmortem(self, payload: dict[str, Any]) -> tuple[str, dict[str, int]]:
        """Accept a postmortem and materialize its proposal in one commit."""
        from investment_panel.database.strategy_learning import StrategyLearningRepository

        with self.runtime.transaction(JOB_PROFILE) as connection:
            task_id = self._submit_in_transaction(connection, "option_postmortem", payload)
            evaluations = StrategyLearningRepository(self.runtime).materialize_postmortem_in_transaction(
                connection, task_id, payload
            )
        return task_id, evaluations

    @staticmethod
    def _submit_in_transaction(
        connection: Any, task_kind: str, payload: dict[str, Any]
    ) -> str:
        if task_kind not in {"option_thesis", "option_postmortem"}:
            raise ValueError("unsupported agent task kind")
        request_id = str(
            payload.get("request_id")
            or (payload.get("request") or {}).get("request_id")
            or payload.get("task_id")
            or ""
        )
        if not request_id:
            raise ValueError("request_id is required")
        _validate_result(task_kind, payload)
        row = accept_agent_task_result(connection, task_id=request_id, task_kind=task_kind, result=payload)
        if row is None:
            raise ValueError(f"agent request not found: {request_id}")
        return str(row["id"])

    def rows(self, model_name: str) -> list[dict[str, Any]]:
        rows, _ = self.rows_page(model_name)
        return rows

    def rows_page(
        self,
        model_name: str,
        *,
        limit: int | None = None,
        created_before: datetime | None = None,
        after_created_at: datetime | None = None,
        after_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        specs = {
            "agent_thesis_request": ("option_thesis", "request"),
            "agent_thesis": ("option_thesis", "result"),
            "agent_thesis_validation": ("option_thesis", "validation"),
            "agent_postmortem_request": ("option_postmortem", "request"),
            "agent_postmortem": ("option_postmortem", "result"),
        }
        task_kind, field = specs[model_name]
        with self.runtime.read() as connection:
            availability_field = {
                "request": "created_at",
                "result": "result_available_at",
                "validation": "validation_available_at",
            }[field]
            cutoff_clause = (
                f" AND {availability_field} <= %s"
                if created_before is not None
                else ""
            )
            parameters: list[Any] = [task_kind]
            if created_before is not None:
                parameters.append(created_before)
            count = connection.execute(
                f"SELECT count(*) AS count FROM analysis.agent_task "
                f"WHERE task_kind = %s AND {field} IS NOT NULL{cutoff_clause}",
                parameters,
            ).fetchone()["count"]
            page_clause = cutoff_clause
            if after_created_at is not None and after_id is not None:
                page_clause += " AND (created_at, id) < (%s, %s)"
                parameters.extend([after_created_at, after_id])
            bounded = " LIMIT %s" if limit is not None else ""
            parameters = list(parameters)
            if limit is not None:
                parameters.append(limit)
            rows = connection.execute(
                f"SELECT id, status, created_at, updated_at, {field} AS payload "
                "FROM analysis.agent_task WHERE task_kind = %s "
                f"AND {field} IS NOT NULL{page_clause} "
                f"ORDER BY created_at DESC, id DESC{bounded}",
                parameters,
            ).fetchall()
        return ([
            {
                **dict(row["payload"] or {}),
                "request_id": str(row["id"]),
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ], int(count))

    def run_queued(
        self,
        command: str,
        *,
        limit: int = 10,
        timeout_seconds: int = 180,
        trigger: str | None = None,
        run_trigger: str | None = None,
        provider: str = "external",
        model: str = "configured-command",
        reasoning_effort: str = "",
        max_runs_per_day: int = 0,
        consolidated: bool = False,
        kind_limits: dict[str, int] | None = None,
        task_kinds: Sequence[str] = ("option_thesis", "option_postmortem"),
    ) -> dict[str, Any]:
        self.recover_stale_tasks(stale_after=timedelta(seconds=max(300, timeout_seconds + 60)))
        if not command.strip():
            return {"status": "skipped", "reason": "agent command is not configured", "completed": 0, "failed": 0}
        if not consolidated:
            return self._run_queued_separate(
                command, limit=limit, timeout_seconds=timeout_seconds, trigger=trigger,
                run_trigger=run_trigger, provider=provider, model=model, reasoning_effort=reasoning_effort,
                task_kinds=task_kinds,
            )
        completed = failed = 0
        errors: list[str] = []
        with self.runtime.transaction(JOB_PROFILE) as connection:
            effective_trigger = run_trigger or trigger or "scheduled"
            if effective_trigger == "scheduled" and max_runs_per_day > 0:
                connection.execute("SELECT pg_advisory_xact_lock(hashtext('market-option-agent-scheduled'))")
                daily_runs = connection.execute(
                    "SELECT count(*) AS count FROM analysis.agent_run "
                    "WHERE trigger = 'scheduled' "
                    "AND (started_at AT TIME ZONE 'America/New_York')::date = "
                    "(now() AT TIME ZONE 'America/New_York')::date",
                ).fetchone()
                if int(daily_runs["count"] or 0) >= max_runs_per_day:
                    return {
                        "status": "skipped", "reason": "daily_run_cap",
                        "completed": 0, "failed": 0,
                    }
            if kind_limits:
                tasks = []
                for kind in task_kinds:
                    cap = max(0, int(kind_limits.get(kind, 0)))
                    if not cap:
                        continue
                    tasks.extend(connection.execute(
                        "SELECT id, task_kind, request FROM analysis.agent_task "
                        "WHERE status = 'queued' AND task_kind = %s "
                        "AND (CAST(%s AS text) IS NULL OR request->>'trigger' = %s) "
                        "ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED",
                        [kind, trigger, trigger, cap],
                    ).fetchall())
                tasks.sort(key=lambda task: str(task["id"]))
            else:
                tasks = connection.execute(
                    "SELECT id, task_kind, request FROM analysis.agent_task "
                    "WHERE status = 'queued' AND task_kind = ANY(%s) "
                    "AND (CAST(%s AS text) IS NULL OR request->>'trigger' = %s) "
                    "ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED",
                    [list(task_kinds), trigger, trigger, limit],
                ).fetchall()
            if not tasks:
                return {
                    "status": "skipped", "reason": "no_open_tasks",
                    "completed": 0, "failed": 0,
                }
            run = connection.execute(
                """
                INSERT INTO analysis.agent_run (provider, model, trigger, started_at, status, summary)
                VALUES (%s, %s, %s, now(), 'running', %s) RETURNING id
                """,
                [provider, model, effective_trigger, Jsonb({"workflow": "option_agent", "invoked": True})],
            ).fetchone()
            if tasks:
                connection.execute(
                    "UPDATE analysis.agent_task SET agent_run_id = %s, status = 'running', updated_at = now() "
                    "WHERE id = ANY(%s)",
                    [run["id"], [task["id"] for task in tasks]],
                )

        meta: dict[str, Any] = {}
        if tasks:
            payload = _batch_payload(tasks)
            try:
                child_env = os.environ.copy()
                child_env.update(agent_env(
                    provider=provider, model=model, reasoning_effort=reasoning_effort,
                    timeout_seconds=timeout_seconds,
                ))
                process = subprocess.run(
                    _command_args(command), input=json.dumps(payload), text=True,
                    capture_output=True, timeout=timeout_seconds, check=False, env=child_env,
                )
                if process.returncode != 0:
                    meta = agent_error_meta(process.stderr or process.stdout or "")
                    raise RuntimeError((process.stderr or process.stdout or f"exit {process.returncode}")[-2000:])
                output = json.loads(process.stdout)
                meta = output.get("_meta") if isinstance(output.get("_meta"), dict) else {}
                completed, failed, errors = self._dispatch_batch(tasks, output)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                with self.runtime.transaction() as connection:
                    connection.execute(
                        "UPDATE analysis.agent_task SET status = 'failed', validation = %s, updated_at = now() "
                        "WHERE id = ANY(%s)",
                        [Jsonb({"status": "failed", "error": error}), [task["id"] for task in tasks]],
                    )
                failed = len(tasks)
                errors = [f"{task['id']}: {error}" for task in tasks]
        usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
        thesis_attempted = sum(str(task["task_kind"]) == "option_thesis" for task in tasks)
        postmortem_attempted = len(tasks) - thesis_attempted
        thesis_accepted = 0
        postmortem_accepted = 0
        if tasks:
            with self.runtime.read() as connection:
                accepted = connection.execute(
                    "SELECT task_kind, count(*) AS count FROM analysis.agent_task "
                    "WHERE id = ANY(%s) AND status = 'completed' GROUP BY task_kind",
                    [[task["id"] for task in tasks]],
                ).fetchall()
            accepted_by_kind = {str(row["task_kind"]): int(row["count"]) for row in accepted}
            thesis_accepted = accepted_by_kind.get("option_thesis", 0)
            postmortem_accepted = accepted_by_kind.get("option_postmortem", 0)
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE analysis.agent_run SET status = %s, finished_at = now(), input_tokens = %s, "
                "output_tokens = %s, summary = %s WHERE id = %s",
                ["succeeded" if failed == 0 else "partial" if completed else "failed",
                 int(usage.get("input_tokens") or 0) or None, int(usage.get("output_tokens") or 0) or None,
                 Jsonb({"workflow": "option_agent", "invoked": True,
                        "completed": completed, "failed": failed, "errors": errors, "batch_size": len(tasks),
                        "thesis_attempted": thesis_attempted, "thesis_accepted": thesis_accepted,
                        "postmortem_attempted": postmortem_attempted, "postmortem_accepted": postmortem_accepted}), run["id"]],
            )
        return {"status": "ok" if failed == 0 else "partial" if completed else "failed", "run_id": str(run["id"]), "completed": completed, "failed": failed, "errors": errors}

    def _run_queued_separate(
        self, command: str, *, limit: int, timeout_seconds: int, trigger: str | None,
        run_trigger: str | None,
        provider: str, model: str, reasoning_effort: str, task_kinds: Sequence[str],
    ) -> dict[str, Any]:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            run = connection.execute(
                "INSERT INTO analysis.agent_run (provider, model, trigger, started_at, status) "
                "VALUES (%s, %s, %s, now(), 'running') RETURNING id",
                [provider, model, run_trigger or trigger or "scheduled"],
            ).fetchone()
        completed = failed = 0
        errors: list[str] = []
        child_env = os.environ.copy()
        child_env.update(agent_env(
            provider=provider, model=model, reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
        ))
        for _ in range(limit):
            with self.runtime.transaction(JOB_PROFILE) as connection:
                task = connection.execute(
                    "SELECT id, task_kind, request FROM analysis.agent_task "
                    "WHERE status = 'queued' AND task_kind = ANY(%s) "
                    "AND (CAST(%s AS text) IS NULL OR request->>'trigger' = %s) "
                    "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED",
                    [list(task_kinds), trigger, trigger],
                ).fetchone()
                if task is not None:
                    connection.execute(
                        "UPDATE analysis.agent_task SET agent_run_id = %s, status = 'running', updated_at = now() WHERE id = %s",
                        [run["id"], task["id"]],
                    )
            if task is None:
                break
            try:
                process = subprocess.run(
                    _command_args(command), input=json.dumps(_jsonable(dict(task["request"]))),
                    text=True, capture_output=True, timeout=timeout_seconds, check=False, env=child_env,
                )
                if process.returncode != 0:
                    raise RuntimeError((process.stderr or process.stdout or f"exit {process.returncode}")[-2000:])
                result = json.loads(process.stdout)
                _validate_result(str(task["task_kind"]), result)
                with self.runtime.transaction() as connection:
                    accept_agent_task_result(
                        connection, task_id=str(task["id"]), task_kind=str(task["task_kind"]), result=result,
                    )
                    if str(task["task_kind"]) == "option_postmortem":
                        from investment_panel.database.strategy_learning import StrategyLearningRepository
                        StrategyLearningRepository(self.runtime).materialize_postmortem_in_transaction(
                            connection, str(task["id"]), result
                        )
                completed += 1
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                with self.runtime.transaction() as connection:
                    connection.execute(
                        "UPDATE analysis.agent_task SET status = 'failed', validation = %s, updated_at = now() WHERE id = %s",
                        [Jsonb({"status": "failed", "error": error}), task["id"]],
                    )
                failed += 1
                errors.append(f"{task['id']}: {error}")
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE analysis.agent_run SET status = %s, finished_at = now(), summary = %s WHERE id = %s",
                ["succeeded" if failed == 0 else "partial" if completed else "failed",
                 Jsonb({"completed": completed, "failed": failed, "errors": errors}), run["id"]],
            )
        return {"status": "ok" if failed == 0 else "partial" if completed else "failed", "run_id": str(run["id"]), "completed": completed, "failed": failed, "errors": errors}

    def _dispatch_batch(self, tasks: Sequence[Any], output: dict[str, Any]) -> tuple[int, int, list[str]]:
        by_kind = {
            "option_thesis": list(output.get("thesis") or []),
            "option_postmortem": list(output.get("postmortem") or []),
        }
        offsets = {kind: 0 for kind in by_kind}
        completed = failed = 0
        errors: list[str] = []
        for task in tasks:
            kind = str(task["task_kind"])
            index = offsets[kind]
            offsets[kind] += 1
            try:
                result = by_kind[kind][index]
                if not isinstance(result, dict):
                    raise ValueError("agent result must be an object")
                _validate_result(kind, result)
                with self.runtime.transaction() as connection:
                    accept_agent_task_result(
                        connection, task_id=str(task["id"]), task_kind=kind, result=result,
                    )
                    if kind == "option_postmortem":
                        from investment_panel.database.strategy_learning import StrategyLearningRepository
                        StrategyLearningRepository(self.runtime).materialize_postmortem_in_transaction(
                            connection, str(task["id"]), result
                        )
                completed += 1
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                with self.runtime.transaction() as connection:
                    connection.execute(
                        "UPDATE analysis.agent_task SET status = 'failed', validation = %s, updated_at = now() WHERE id = %s",
                        [Jsonb({"status": "failed", "error": error}), task["id"]],
                    )
                failed += 1
                errors.append(f"{task['id']}: {error}")
        return completed, failed, errors

    def recover_stale_tasks(self, *, stale_after: timedelta = timedelta(minutes=10)) -> int:
        cutoff = datetime.now(UTC) - stale_after
        with self.runtime.transaction(JOB_PROFILE) as connection:
            stale = connection.execute(
                "SELECT id, agent_run_id FROM analysis.agent_task "
                "WHERE status = 'running' AND updated_at < %s FOR UPDATE SKIP LOCKED",
                [cutoff],
            ).fetchall()
            if not stale:
                return 0
            task_ids = [row["id"] for row in stale]
            run_ids = list({row["agent_run_id"] for row in stale if row["agent_run_id"] is not None})
            connection.execute(
                "UPDATE analysis.agent_task SET status = 'queued', agent_run_id = NULL, "
                "validation = %s, updated_at = now() WHERE id = ANY(%s)",
                [Jsonb({"status": "requeued", "reason": "stale_running_lease"}), task_ids],
            )
            if run_ids:
                connection.execute(
                    "UPDATE analysis.agent_run SET status = 'failed', finished_at = now(), "
                    "summary = summary || %s WHERE id = ANY(%s) AND status = 'running'",
                    [Jsonb({"error": "worker lease expired; tasks requeued"}), run_ids],
                )
        return len(task_ids)

    def overview(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        today_start = market_day_start_utc(now)
        with self.runtime.read() as connection:
            queue_rows = connection.execute(
                "SELECT task_kind, count(*) AS count, min(created_at) AS oldest "
                "FROM analysis.agent_task WHERE status IN ('queued', 'running') GROUP BY task_kind"
            ).fetchall()
            runs = connection.execute(
                """
                SELECT id, coalesce(summary->>'workflow', 'option_agent') AS workflow,
                       provider, model, trigger,
                       NULL::text AS ticker, started_at, finished_at, input_tokens,
                       output_tokens, cost_usd, status, summary
                FROM analysis.agent_run
                UNION ALL
                SELECT id, 'thesis_monitor' AS workflow, 'codex' AS provider, model,
                       trigger, input_symbol AS ticker, started_at, finished_at,
                       input_tokens, output_tokens, cost_usd, status,
                       jsonb_build_object(
                           'completed', CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END,
                           'failed', CASE WHEN status IN ('failed', 'timeout') THEN 1 ELSE 0 END,
                           'error', error
                       ) AS summary
                FROM app.thesis_automation_run
                ORDER BY started_at DESC LIMIT 50
                """
            ).fetchall()
            workflow_rows = connection.execute(
                """
                WITH workflow_runs AS (
                    SELECT coalesce(summary->>'workflow', 'option_agent') AS workflow, status
                    FROM analysis.agent_run
                    UNION ALL
                    SELECT 'thesis_monitor' AS workflow, status FROM app.thesis_automation_run
                )
                SELECT workflow, count(*) AS runs,
                       count(*) FILTER (WHERE status = 'succeeded') AS succeeded,
                       count(*) FILTER (WHERE status IN ('failed', 'timeout', 'partial')) AS failed,
                       count(*) FILTER (WHERE status = 'running') AS running
                FROM workflow_runs GROUP BY workflow ORDER BY workflow
                """
            ).fetchall()
            costs = connection.execute(
                """
                WITH all_runs AS (
                    SELECT started_at, input_tokens, output_tokens, cost_usd FROM analysis.agent_run
                    UNION ALL
                    SELECT started_at, input_tokens, output_tokens, cost_usd FROM app.thesis_automation_run
                )
                SELECT
                    count(*) FILTER (WHERE started_at >= %s) AS today_runs,
                    coalesce(sum(input_tokens) FILTER (WHERE started_at >= %s), 0) AS today_input,
                    coalesce(sum(output_tokens) FILTER (WHERE started_at >= %s), 0) AS today_output,
                    coalesce(sum(cost_usd) FILTER (WHERE started_at >= %s), 0) AS today_cost,
                    count(*) FILTER (WHERE started_at >= %s) AS week_runs,
                    coalesce(sum(input_tokens) FILTER (WHERE started_at >= %s), 0) AS week_input,
                    coalesce(sum(output_tokens) FILTER (WHERE started_at >= %s), 0) AS week_output,
                    coalesce(sum(cost_usd) FILTER (WHERE started_at >= %s), 0) AS week_cost
                FROM all_runs
                """,
                [
                    today_start,
                    today_start,
                    today_start,
                    today_start,
                    now - timedelta(days=7),
                    now - timedelta(days=7),
                    now - timedelta(days=7),
                    now - timedelta(days=7),
                ],
            ).fetchone()
            materialization = option_thesis_materialization_summary(connection)
        queue = {str(row["task_kind"]): int(row["count"]) for row in queue_rows}
        oldest = min((row["oldest"] for row in queue_rows if row["oldest"]), default=None)
        normalized_runs = []
        for row in runs:
            item = dict(row)
            summary = dict(item.pop("summary") or {})
            item.update({
                "id": str(item["id"]),
                "est_cost_usd": float(item.pop("cost_usd") or 0),
                "tokens_estimated": item.get("provider") == "codex",
                "thesis_attempted": int(summary.get("thesis_attempted") or (1 if item["workflow"] == "thesis_monitor" else 0)),
                "thesis_accepted": int(summary.get("thesis_accepted") or (summary.get("completed") if item["workflow"] == "thesis_monitor" else 0) or 0),
                "postmortem_attempted": int(summary.get("postmortem_attempted") or 0),
                "postmortem_accepted": int(summary.get("postmortem_accepted") or 0),
                "error": summary.get("error") or next(iter(summary.get("errors") or []), None),
            })
            normalized_runs.append(item)
        workflow_counts = {
            str(row["workflow"]): {
                "runs": int(row["runs"]), "succeeded": int(row["succeeded"]),
                "failed": int(row["failed"]), "running": int(row["running"]),
            }
            for row in workflow_rows
        }
        return {
            "queue": {
                "thesis_open": queue.get("option_thesis", 0),
                "postmortem_open": queue.get("option_postmortem", 0),
                "total_open": sum(queue.values()),
                "oldest_open_at": oldest,
            },
            "runs": normalized_runs,
            "workflows": workflow_counts,
            "materialization": {
                **materialization,
            },
            "cost": {
                "today": {"runs": int(costs["today_runs"]), "input_tokens": int(costs["today_input"]), "output_tokens": int(costs["today_output"]), "est_cost_usd": float(costs["today_cost"])},
                "last_7d": {"runs": int(costs["week_runs"]), "input_tokens": int(costs["week_input"]), "output_tokens": int(costs["week_output"]), "est_cost_usd": float(costs["week_cost"])},
            },
        }


def _batch_payload(tasks: Sequence[Any]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {"thesis": [], "postmortem": []}
    for task in tasks:
        stored = dict(task["request"] or {})
        request = {
            "request": {
                **{key: value for key, value in stored.items() if key not in {"context", "custom_prompt"}},
                "request_id": str(task["id"]),
            },
            "prompt": str(stored.get("custom_prompt") or ""),
            "context": stored.get("context") or stored.get("decision") or {},
            "guardrails": {"authority": stored.get("authority") or "advisory_only"},
        }
        key = "thesis" if str(task["task_kind"]) == "option_thesis" else "postmortem"
        grouped[key].append(_jsonable(request))
    return {
        **grouped,
        "guardrails": {
            "authority": "hypothesis_only",
            "deterministic_code_owns": ["facts", "math", "validation", "scoring", "promotion"],
            "forbidden": ["trade_execution", "silent_strategy_promotion", "invented_evidence"],
        },
    }
