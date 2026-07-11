"""PostgreSQL option-agent task queue and execution contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import shlex
import subprocess
from typing import Any, Sequence
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


class AgentRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def queue_thesis(self, ticker: str, *, prompt: str = "", trigger: str = "ondemand") -> dict[str, Any]:
        symbol = str(ticker).strip().upper()
        if not symbol:
            raise ValueError("ticker is required")
        with self.runtime.transaction() as connection:
            instrument = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = %s", [symbol]).fetchone()
            decision = connection.execute(
                """
                SELECT decision.id, decision.state, decision.score, decision.reasons,
                       decision.blockers, decision.as_of
                FROM analysis.decision decision
                JOIN catalog.instrument candidate ON candidate.id = decision.instrument_id
                WHERE candidate.symbol = %s
                ORDER BY decision.as_of DESC, decision.score DESC NULLS LAST LIMIT 1
                """,
                [symbol],
            ).fetchone()
            existing = connection.execute(
                """
                SELECT id, request, status FROM analysis.agent_task
                WHERE task_kind = 'option_thesis' AND status IN ('queued', 'running')
                  AND request->>'ticker' = %s AND request->>'trigger' = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                [symbol, trigger],
            ).fetchone()
            if existing:
                return {"request_id": str(existing["id"]), "status": existing["status"], **dict(existing["request"])}
            request = {
                "ticker": symbol,
                "trigger": trigger,
                "custom_prompt": prompt,
                "instrument_id": instrument["id"] if instrument else None,
                "decision": dict(decision) if decision else {},
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

    def queue_postmortem(self, decision_id: str | UUID, *, reason: str) -> dict[str, Any]:
        with self.runtime.transaction() as connection:
            decision = connection.execute(
                "SELECT id, decision_key, state, score, reasons, blockers FROM analysis.decision WHERE id = %s",
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

    def submit(self, task_kind: str, payload: dict[str, Any]) -> str:
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
        with self.runtime.transaction() as connection:
            row = connection.execute(
                """
                UPDATE analysis.agent_task
                SET status = 'completed', result = %s,
                    validation = %s, updated_at = now()
                WHERE id = %s AND task_kind = %s
                RETURNING id
                """,
                [Jsonb(_jsonable(payload)), Jsonb({"status": "accepted", "authority": "advisory_only"}), request_id, task_kind],
            ).fetchone()
        if row is None:
            raise ValueError(f"agent request not found: {request_id}")
        return str(row["id"])

    def rows(self, model_name: str) -> list[dict[str, Any]]:
        specs = {
            "agent_thesis_request": ("option_thesis", "request"),
            "agent_thesis": ("option_thesis", "result"),
            "agent_thesis_validation": ("option_thesis", "validation"),
            "agent_postmortem_request": ("option_postmortem", "request"),
            "agent_postmortem": ("option_postmortem", "result"),
        }
        task_kind, field = specs[model_name]
        with self.runtime.read() as connection:
            rows = connection.execute(
                f"SELECT id, status, created_at, updated_at, {field} AS payload "
                "FROM analysis.agent_task WHERE task_kind IN (%s, %s) "
                f"AND {field} IS NOT NULL ORDER BY created_at DESC",
                [task_kind, f"legacy_agent_{'thesis' if task_kind == 'option_thesis' else 'postmortem'}"],
            ).fetchall()
        return [
            {
                "request_id": str(row["id"]),
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                **dict(row["payload"] or {}),
            }
            for row in rows
        ]

    def run_queued(
        self,
        command: str,
        *,
        limit: int = 10,
        timeout_seconds: int = 180,
        trigger: str | None = None,
        provider: str = "external",
        model: str = "configured-command",
        task_kinds: Sequence[str] = ("option_thesis", "option_postmortem"),
    ) -> dict[str, Any]:
        if not command.strip():
            return {"status": "skipped", "reason": "agent command is not configured", "completed": 0, "failed": 0}
        with self.runtime.transaction(JOB_PROFILE) as connection:
            run = connection.execute(
                """
                INSERT INTO analysis.agent_run (provider, model, trigger, started_at, status)
                VALUES (%s, %s, %s, now(), 'running') RETURNING id
                """,
                [provider, model, trigger or "scheduled"],
            ).fetchone()
            tasks = connection.execute(
                """
                SELECT id, task_kind, request FROM analysis.agent_task
                WHERE status = 'queued' AND task_kind = ANY(%s)
                  AND (CAST(%s AS text) IS NULL OR request->>'trigger' = %s)
                ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED
                """,
                [list(task_kinds), trigger, trigger, limit],
            ).fetchall()
            if tasks:
                connection.execute(
                    "UPDATE analysis.agent_task SET agent_run_id = %s, status = 'running', updated_at = now() "
                    "WHERE id = ANY(%s)",
                    [run["id"], [task["id"] for task in tasks]],
                )
        completed = failed = 0
        errors: list[str] = []
        for task in tasks:
            try:
                process = subprocess.run(
                    shlex.split(command),
                    input=json.dumps(_jsonable(dict(task["request"]))),
                    text=True,
                    capture_output=True,
                    timeout=timeout_seconds,
                    check=False,
                )
                if process.returncode != 0:
                    raise RuntimeError((process.stderr or process.stdout or f"exit {process.returncode}")[-2000:])
                result = json.loads(process.stdout)
                _validate_result(str(task["task_kind"]), result)
                with self.runtime.transaction() as connection:
                    connection.execute(
                        "UPDATE analysis.agent_task SET status = 'completed', result = %s, validation = %s, updated_at = now() WHERE id = %s",
                        [Jsonb(_jsonable(result)), Jsonb({"status": "accepted", "authority": "advisory_only"}), task["id"]],
                    )
                completed += 1
            except Exception as exc:
                with self.runtime.transaction() as connection:
                    connection.execute(
                        "UPDATE analysis.agent_task SET status = 'failed', validation = %s, updated_at = now() WHERE id = %s",
                        [Jsonb({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}), task["id"]],
                    )
                failed += 1
                errors.append(f"{task['id']}: {exc}")
        with self.runtime.transaction() as connection:
            connection.execute(
                "UPDATE analysis.agent_run SET status = %s, finished_at = now(), summary = %s WHERE id = %s",
                ["succeeded" if failed == 0 else "partial" if completed else "failed", Jsonb({"completed": completed, "failed": failed, "errors": errors}), run["id"]],
            )
        return {"status": "ok" if failed == 0 else "partial" if completed else "failed", "run_id": str(run["id"]), "completed": completed, "failed": failed, "errors": errors}

    def overview(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self.runtime.read() as connection:
            queue_rows = connection.execute(
                "SELECT task_kind, count(*) AS count, min(created_at) AS oldest "
                "FROM analysis.agent_task WHERE status IN ('queued', 'running') GROUP BY task_kind"
            ).fetchall()
            runs = connection.execute(
                "SELECT id, provider, model, trigger, started_at, finished_at, input_tokens, "
                "output_tokens, cost_usd, status, summary FROM analysis.agent_run "
                "ORDER BY started_at DESC LIMIT 50"
            ).fetchall()
            costs = connection.execute(
                """
                SELECT
                    count(*) FILTER (WHERE started_at >= %s) AS today_runs,
                    coalesce(sum(input_tokens) FILTER (WHERE started_at >= %s), 0) AS today_input,
                    coalesce(sum(output_tokens) FILTER (WHERE started_at >= %s), 0) AS today_output,
                    coalesce(sum(cost_usd) FILTER (WHERE started_at >= %s), 0) AS today_cost,
                    count(*) FILTER (WHERE started_at >= %s) AS week_runs,
                    coalesce(sum(input_tokens) FILTER (WHERE started_at >= %s), 0) AS week_input,
                    coalesce(sum(output_tokens) FILTER (WHERE started_at >= %s), 0) AS week_output,
                    coalesce(sum(cost_usd) FILTER (WHERE started_at >= %s), 0) AS week_cost
                FROM analysis.agent_run
                """,
                [
                    now.replace(hour=0, minute=0, second=0, microsecond=0),
                    now.replace(hour=0, minute=0, second=0, microsecond=0),
                    now.replace(hour=0, minute=0, second=0, microsecond=0),
                    now.replace(hour=0, minute=0, second=0, microsecond=0),
                    now - timedelta(days=7),
                    now - timedelta(days=7),
                    now - timedelta(days=7),
                    now - timedelta(days=7),
                ],
            ).fetchone()
        queue = {str(row["task_kind"]): int(row["count"]) for row in queue_rows}
        oldest = min((row["oldest"] for row in queue_rows if row["oldest"]), default=None)
        return {
            "queue": {
                "thesis_open": queue.get("option_thesis", 0),
                "postmortem_open": queue.get("option_postmortem", 0),
                "total_open": sum(queue.values()),
                "oldest_open_at": oldest,
            },
            "runs": [{**dict(row), "id": str(row["id"])} for row in runs],
            "cost": {
                "today": {"runs": costs["today_runs"], "input_tokens": costs["today_input"], "output_tokens": costs["today_output"], "est_cost_usd": float(costs["today_cost"])},
                "last_7d": {"runs": costs["week_runs"], "input_tokens": costs["week_input"], "output_tokens": costs["week_output"], "est_cost_usd": float(costs["week_cost"])},
            },
        }


def _validate_result(task_kind: str, payload: dict[str, Any]) -> None:
    if task_kind == "option_thesis":
        thesis = str(payload.get("core_thesis") or payload.get("thesis") or "").strip()
        if not thesis:
            raise ValueError("agent thesis requires core_thesis")
    elif task_kind == "option_postmortem":
        if not str(payload.get("failure_type") or payload.get("outcome_type") or "").strip():
            raise ValueError("agent postmortem requires failure_type or outcome_type")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value
