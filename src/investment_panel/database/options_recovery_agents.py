"""PostgreSQL queue, validation, and telemetry for recovery event agents.

This owner deliberately stages advisory work separately from the 15-minute
collector.  A Codex outage can fail a batch, but never capture, select, ticket,
size, manage, or promote an options recovery position.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from investment_panel.core.options_recovery_agents import (
    MUTATION_DRAFTER,
    RECOVERY_AGENT_ROLES,
    normalize_recovery_agent_output,
    validate_evidence,
)
from investment_panel.core.options_recovery_registry import validate_mutation
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


MATERIAL_SPOT_MOVE = 0.02
MATERIAL_IV_CHANGE = 0.10
RECOVERY_TASK_KINDS = tuple(f"options_recovery_{role}" for role in RECOVERY_AGENT_ROLES)
_NEW_YORK = ZoneInfo("America/New_York")


class RecoveryEventAgentRepository:
    """Queue bounded advisory batches and reject authority-bearing outputs."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def queue_if_material(
        self,
        event_id: str,
        *,
        capture_id: str | None = None,
        now: datetime | None = None,
        model: str = "gpt-5.6-luna",
        reasoning_effort: str = "high",
        debounce_minutes: int = 30,
        max_batches_per_symbol_per_day: int = 2,
        max_tasks: int = 12,
        preopen: bool = False,
    ) -> dict[str, Any]:
        """Queue one event batch only when its deterministic fingerprint changed."""

        reference = _utc(now) or datetime.now(UTC)
        task_limit = max(0, min(int(max_tasks), 12))
        if task_limit < 1:
            return {"status": "skipped", "reason": "event_agent_task_limit_zero", "event_id": event_id}
        with self.runtime.transaction(JOB_PROFILE) as connection:
            context = self._event_context(connection, event_id, capture_id)
            if context is None:
                return {"status": "skipped", "reason": "event_not_active", "event_id": event_id}
            if int(context["complete_capture_count"] or 0) < 2:
                return {"status": "skipped", "reason": "event_not_established", "event_id": event_id}
            prior = connection.execute(
                """
                SELECT * FROM analysis.option_event_agent_batch
                WHERE event_id = %s AND status IN ('queued', 'running', 'completed')
                ORDER BY created_at DESC LIMIT 1
                """,
                [event_id],
            ).fetchone()
            if preopen and connection.execute(
                """
                SELECT EXISTS (
                  SELECT 1 FROM analysis.option_event_agent_batch
                  WHERE event_id = %s AND trigger = 'preopen_review'
                    AND (created_at AT TIME ZONE 'America/New_York')::date = %s
                ) AS present
                """,
                [event_id, reference.astimezone(_NEW_YORK).date()],
            ).fetchone()["present"]:
                return {"status": "skipped", "reason": "preopen_review_already_queued", "event_id": event_id}
            trigger, reasons = _agent_trigger(
                context,
                dict(prior["fingerprint"] or {}) if prior else None,
                preopen=preopen,
            )
            if trigger is None:
                return {"status": "skipped", "reason": "no_material_fingerprint_change", "event_id": event_id}
            recent = connection.execute(
                """
                SELECT batch.created_at
                FROM analysis.option_event_agent_batch batch
                JOIN analysis.option_event prior_event ON prior_event.id = batch.event_id
                WHERE prior_event.instrument_id = %s AND batch.status IN ('queued', 'running', 'completed')
                ORDER BY batch.created_at DESC LIMIT 1
                """,
                [context["instrument_id"]],
            ).fetchone()
            if recent and (reference - recent["created_at"]).total_seconds() < max(0, debounce_minutes) * 60:
                return {"status": "skipped", "reason": "event_agent_debounced", "event_id": event_id}
            daily = int(connection.execute(
                """
                SELECT count(*) AS count
                FROM analysis.option_event_agent_batch batch
                JOIN analysis.option_event prior_event ON prior_event.id = batch.event_id
                WHERE prior_event.instrument_id = %s
                  AND (batch.created_at AT TIME ZONE 'America/New_York')::date = %s
                """,
                [context["instrument_id"], reference.astimezone(_NEW_YORK).date()],
            ).fetchone()["count"] or 0)
            if daily >= max(0, max_batches_per_symbol_per_day):
                return {"status": "skipped", "reason": "event_agent_daily_batch_cap", "event_id": event_id}
            fingerprint = _fingerprint(context)
            fingerprint_key = _fingerprint_key(trigger, fingerprint)
            task_count = min(task_limit, len(RECOVERY_AGENT_ROLES))
            batch = connection.execute(
                """
                INSERT INTO analysis.option_event_agent_batch
                    (event_id, capture_id, trigger, fingerprint_key, fingerprint, provider, model,
                     reasoning_effort, status, task_count, telemetry)
                VALUES (%s, %s, %s, %s, %s, 'codex', %s, %s, 'queued', %s, %s)
                ON CONFLICT (event_id, fingerprint_key) DO NOTHING
                RETURNING id
                """,
                [
                    event_id, context.get("capture_id"), trigger, fingerprint_key, Jsonb(fingerprint),
                    model, reasoning_effort, task_count,
                    Jsonb({"reasons": reasons, "authority": "advisory_only"}),
                ],
            ).fetchone()
            if batch is None:
                return {"status": "skipped", "reason": "duplicate_event_fingerprint", "event_id": event_id}
            tasks: list[dict[str, Any]] = []
            for role in RECOVERY_AGENT_ROLES[:task_count]:
                request = {
                    "recovery_event_batch_id": str(batch["id"]),
                    "event_id": event_id,
                    "symbol": context["symbol"],
                    "role": role,
                    "trigger": trigger,
                    "event": _event_payload(context),
                    "authority": "advisory_only",
                    "forbidden": [
                        "prices", "outcomes", "ticket_quantities", "execution_readiness",
                        "risk_gates", "paper_orders", "promotion",
                    ],
                }
                task = connection.execute(
                    """
                    INSERT INTO analysis.agent_task (task_kind, status, request)
                    VALUES (%s, 'queued', %s) RETURNING id
                    """,
                    [f"options_recovery_{role}", Jsonb(request)],
                ).fetchone()
                tasks.append({"id": str(task["id"]), "role": role})
        return {
            "status": "queued", "event_id": event_id, "batch_id": str(batch["id"]),
            "trigger": trigger, "reasons": reasons, "tasks": tasks,
        }

    def queue_preopen_reviews(
        self,
        *,
        now: datetime | None = None,
        model: str = "gpt-5.6-luna",
        reasoning_effort: str = "high",
        debounce_minutes: int = 30,
        max_batches_per_symbol_per_day: int = 2,
        max_tasks: int = 12,
    ) -> list[dict[str, Any]]:
        """Queue one advisory review for each still-active event before open."""

        reference = _utc(now) or datetime.now(UTC)
        local = reference.astimezone(_NEW_YORK)
        if not (4 <= local.hour < 10):
            return [{"status": "skipped", "reason": "outside_preopen_window"}]
        with self.runtime.read(JOB_PROFILE) as connection:
            event_ids = [str(row["id"]) for row in connection.execute(
                "SELECT id FROM analysis.option_event WHERE status = 'active' ORDER BY started_at"
            ).fetchall()]
        return [
            self.queue_if_material(
                event_id,
                now=reference,
                model=model,
                reasoning_effort=reasoning_effort,
                debounce_minutes=debounce_minutes,
                max_batches_per_symbol_per_day=max_batches_per_symbol_per_day,
                max_tasks=max_tasks,
                preopen=True,
            )
            for event_id in event_ids
        ]

    def claim_next(self) -> dict[str, Any] | None:
        """Reserve one batch and its task set for a single Codex invocation."""

        with self.runtime.transaction(JOB_PROFILE) as connection:
            batch = connection.execute(
                """
                SELECT * FROM analysis.option_event_agent_batch
                WHERE status = 'queued'
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED LIMIT 1
                """
            ).fetchone()
            if batch is None:
                return None
            tasks = [dict(row) for row in connection.execute(
                """
                SELECT id, task_kind, request FROM analysis.agent_task
                WHERE request->>'recovery_event_batch_id' = %s AND status = 'queued'
                ORDER BY created_at, id FOR UPDATE
                """,
                [str(batch["id"])],
            ).fetchall()]
            if not tasks:
                connection.execute(
                    """UPDATE analysis.option_event_agent_batch
                       SET status = 'skipped', finished_at = now(), error = 'no queued tasks'
                       WHERE id = %s""",
                    [batch["id"]],
                )
                return None
            run = connection.execute(
                """
                INSERT INTO analysis.agent_run (provider, model, trigger, started_at, status, summary)
                VALUES ('codex', %s, %s, now(), 'running', %s) RETURNING id
                """,
                [batch["model"], f"options_recovery:{batch['trigger']}", Jsonb({
                    "workflow": "options_recovery_event_batch", "batch_id": str(batch["id"]),
                    "authority": "advisory_only", "task_count": len(tasks),
                })],
            ).fetchone()
            connection.execute(
                """UPDATE analysis.agent_task SET status = 'running', agent_run_id = %s, updated_at = now()
                   WHERE id = ANY(%s)""",
                [run["id"], [task["id"] for task in tasks]],
            )
            connection.execute(
                """UPDATE analysis.option_event_agent_batch
                   SET status = 'running', agent_run_id = %s, started_at = now() WHERE id = %s""",
                [run["id"], batch["id"]],
            )
        return {
            "batch": dict(batch), "run_id": str(run["id"]),
            "tasks": [
                {"id": str(task["id"]), "role": _role(task), "request": dict(task["request"] or {})}
                for task in tasks
            ],
        }

    def complete(self, claim: dict[str, Any], response: Any, *, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Persist only validated advisory output; strategy mutations stay offline."""

        batch = dict(claim["batch"])
        tasks = list(claim["tasks"])
        outputs = normalize_recovery_agent_output(response, expected_tasks=tasks)
        token_counts = _token_counts(meta)
        accepted = failed = rejected = evidence_valid = 0
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for task in tasks:
                task_id = str(task["id"])
                output = outputs.get(task_id)
                if output is None:
                    failed += 1
                    connection.execute(
                        """UPDATE analysis.agent_task SET status = 'failed', validation = %s, updated_at = now()
                           WHERE id = %s""",
                        [Jsonb({"status": "missing_agent_output", "authority": "advisory_only"}), task_id],
                    )
                    continue
                persisted, validation = _validate_output_for_persistence(output)
                accepted += 1
                rejected += int(validation["mutation_status"].startswith("rejected"))
                evidence_valid += int(bool(validation["evidence_valid"]))
                connection.execute(
                    """
                    UPDATE analysis.agent_task
                    SET status = 'completed', result = %s, validation = %s, updated_at = now()
                    WHERE id = %s
                    """,
                    [Jsonb(persisted), Jsonb(validation), task_id],
                )
            status = "completed" if accepted else "failed"
            telemetry = {
                "accepted": accepted,
                "failed": failed,
                "unsupported_proposals": rejected,
                "evidence_validated": evidence_valid,
                "token_usage": token_counts,
                "authority": "advisory_only",
            }
            connection.execute(
                """UPDATE analysis.agent_run
                   SET status = %s, finished_at = now(), input_tokens = %s, output_tokens = %s,
                       summary = summary || %s
                   WHERE id = %s""",
                [status, token_counts["input_tokens"], token_counts["output_tokens"], Jsonb(telemetry), claim["run_id"]],
            )
            connection.execute(
                """UPDATE analysis.option_event_agent_batch
                   SET status = %s, finished_at = now(), telemetry = telemetry || %s WHERE id = %s""",
                [status, Jsonb(telemetry), batch["id"]],
            )
        return {"status": status, "batch_id": str(batch["id"]), **telemetry}

    def fail(self, claim: dict[str, Any], error: Exception | str, *, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Close a failed batch without propagating failure into deterministic work."""

        message = str(error)[:2_000]
        token_counts = _token_counts(meta)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """UPDATE analysis.agent_task
                   SET status = 'failed', validation = %s, updated_at = now()
                   WHERE request->>'recovery_event_batch_id' = %s AND status = 'running'""",
                [Jsonb({"status": "agent_failed", "authority": "advisory_only", "error": message}), str(claim["batch"]["id"])],
            )
            connection.execute(
                """UPDATE analysis.agent_run
                   SET status = 'failed', finished_at = now(), input_tokens = %s, output_tokens = %s,
                       summary = summary || %s WHERE id = %s""",
                [token_counts["input_tokens"], token_counts["output_tokens"], Jsonb({"error": message}), claim["run_id"]],
            )
            connection.execute(
                """UPDATE analysis.option_event_agent_batch
                   SET status = 'failed', finished_at = now(), error = %s, telemetry = telemetry || %s
                   WHERE id = %s""",
                [message, Jsonb({"token_usage": token_counts, "authority": "advisory_only"}), claim["batch"]["id"]],
            )
        return {"status": "failed", "batch_id": str(claim["batch"]["id"]), "error": message}

    def telemetry(self) -> dict[str, Any]:
        """Report reliability, evidence quality, usage, and observational advisory lift."""

        with self.runtime.read(JOB_PROFILE) as connection:
            batch = connection.execute(
                """
                SELECT count(*) AS total, count(*) FILTER (WHERE status = 'failed') AS failed,
                       avg(extract(epoch FROM (finished_at - started_at))) FILTER (WHERE finished_at IS NOT NULL) AS latency
                FROM analysis.option_event_agent_batch
                """
            ).fetchone()
            tasks = connection.execute(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE validation->>'evidence_valid' = 'true') AS evidence_valid,
                       count(*) FILTER (WHERE validation->>'mutation_status' LIKE 'rejected%%') AS rejected_mutation,
                       count(*) FILTER (WHERE task_kind = %s) AS mutation_tasks
                FROM analysis.agent_task
                WHERE task_kind = ANY(%s)
                """,
                [f"options_recovery_{MUTATION_DRAFTER}", list(RECOVERY_TASK_KINDS)],
            ).fetchone()
            usage = connection.execute(
                """
                SELECT coalesce(sum(input_tokens), 0) AS input_tokens, coalesce(sum(output_tokens), 0) AS output_tokens
                FROM analysis.agent_run WHERE trigger LIKE 'options_recovery:%%'
                """
            ).fetchone()
            lift = connection.execute(
                """
                WITH event_returns AS (
                  SELECT observation.event_id, avg(observation.realized_return) AS realized_return
                  FROM analysis.option_opportunity_observation observation
                  WHERE observation.realized_return IS NOT NULL
                  GROUP BY observation.event_id
                ), agent_events AS (
                  SELECT DISTINCT event_id FROM analysis.option_event_agent_batch WHERE status = 'completed'
                )
                SELECT avg(realized_return) FILTER (WHERE event_id IN (SELECT event_id FROM agent_events)) AS with_agent,
                       avg(realized_return) FILTER (WHERE event_id NOT IN (SELECT event_id FROM agent_events)) AS deterministic_only,
                       count(*) FILTER (WHERE event_id IN (SELECT event_id FROM agent_events)) AS with_agent_count,
                       count(*) FILTER (WHERE event_id NOT IN (SELECT event_id FROM agent_events)) AS deterministic_only_count
                FROM event_returns
                """
            ).fetchone()
        with_agent = _number(lift["with_agent"])
        deterministic_only = _number(lift["deterministic_only"])
        advisory_lift = with_agent - deterministic_only if with_agent is not None and deterministic_only is not None else None
        return {
            "batches": int(batch["total"] or 0),
            "agent_failure_rate": _ratio(batch["failed"], batch["total"]),
            "evidence_validation_rate": _ratio(tasks["evidence_valid"], tasks["total"]),
            "unsupported_proposal_rate": _ratio(tasks["rejected_mutation"], tasks["mutation_tasks"]),
            "latency_seconds": _number(batch["latency"]),
            "token_usage": {"input_tokens": int(usage["input_tokens"] or 0), "output_tokens": int(usage["output_tokens"] or 0)},
            "advisory_lift_vs_deterministic_only": advisory_lift,
            "advisory_lift_sample": {
                "with_agent_events": int(lift["with_agent_count"] or 0),
                "deterministic_only_events": int(lift["deterministic_only_count"] or 0),
            },
        }

    def provenance(self, *, event_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Bounded event/batch/task provenance for the product surface."""

        where = "WHERE batch.event_id = %s" if event_id else ""
        values: list[Any] = [event_id] if event_id else []
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
                "id": str(task["id"]), "role": _role(task), "status": task["status"],
                "result": task.get("result"), "validation": task.get("validation"),
                "created_at": task["created_at"], "updated_at": task["updated_at"],
            })
        return [{**batch, "id": str(batch["id"]), "event_id": str(batch["event_id"]), "tasks": by_batch.get(str(batch["id"]), [])} for batch in batches]

    @staticmethod
    def _event_context(connection: Any, event_id: str, capture_id: str | None) -> dict[str, Any] | None:
        event = connection.execute(
            """
            SELECT event.id, event.instrument_id, event.started_at, event.reference_price, event.event_low,
                   event.material_evidence_count, event.provenance, instrument.symbol
            FROM analysis.option_event event
            JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
            WHERE event.id = %s AND event.status = 'active'
            """,
            [event_id],
        ).fetchone()
        if event is None:
            return None
        if capture_id:
            capture = connection.execute(
                """
                SELECT id, snapshot_id, finished_at FROM analysis.option_event_capture
                WHERE event_id = %s AND id = %s AND status = 'complete'
                """,
                [event_id, capture_id],
            ).fetchone()
        else:
            capture = connection.execute(
                """
                SELECT id, snapshot_id, finished_at FROM analysis.option_event_capture
                WHERE event_id = %s AND status = 'complete'
                ORDER BY finished_at DESC LIMIT 1
                """,
                [event_id],
            ).fetchone()
        if capture is None:
            return None
        complete_count = connection.execute(
            "SELECT count(*) AS count FROM analysis.option_event_capture WHERE event_id = %s AND status = 'complete'",
            [event_id],
        ).fetchone()
        spot = connection.execute(
            """
            SELECT price, available_at FROM analysis.option_event_spot
            WHERE event_id = %s AND available_at <= %s
            ORDER BY available_at DESC LIMIT 1
            """,
            [event_id, capture["finished_at"]],
        ).fetchone()
        iv = connection.execute(
            """
            SELECT avg(quote.provider_iv) AS avg_iv
            FROM raw.option_quote quote
            JOIN analysis.option_event_contract contract ON contract.contract_id = quote.contract_id
            WHERE contract.event_id = %s AND quote.snapshot_id = %s AND quote.provider_iv IS NOT NULL
            """,
            [event_id, capture["snapshot_id"]],
        ).fetchone()
        signals = connection.execute(
            """
            SELECT DISTINCT ON (strategy_key) strategy_key, status
            FROM analysis.option_event_signal
            WHERE event_id = %s
            ORDER BY strategy_key, available_at DESC, id DESC
            """,
            [event_id],
        ).fetchall()
        return {
            **dict(event), "capture_id": str(capture["id"]), "capture_finished_at": capture["finished_at"],
            "complete_capture_count": int(complete_count["count"] or 0),
            "underlying_price": _number(spot["price"]) if spot else None,
            "underlying_available_at": spot["available_at"] if spot else None,
            "avg_iv": _number(iv["avg_iv"]),
            "signal_families": sorted(f"{row['strategy_key']}:{row['status']}" for row in signals),
        }


def _agent_trigger(context: dict[str, Any], previous: dict[str, Any] | None, *, preopen: bool) -> tuple[str | None, list[str]]:
    if preopen:
        return "preopen_review", ["still_active_preopen_review"]
    if previous is None:
        return "event_established", ["two_complete_event_strip_captures"]
    reasons: list[str] = []
    current_price, previous_price = _number(context.get("underlying_price")), _number(previous.get("underlying_price"))
    if current_price is not None and previous_price not in (None, 0) and abs(current_price / previous_price - 1.0) >= MATERIAL_SPOT_MOVE:
        reasons.append("underlying_move_2pct")
    current_iv, previous_iv = _number(context.get("avg_iv")), _number(previous.get("avg_iv"))
    if current_iv is not None and previous_iv not in (None, 0) and abs(current_iv / previous_iv - 1.0) >= MATERIAL_IV_CHANGE:
        reasons.append("material_iv_change")
    if int(context.get("material_evidence_count") or 0) > int(previous.get("material_evidence_count") or 0):
        reasons.append("new_material_evidence")
    if list(context.get("signal_families") or []) != list(previous.get("signal_families") or []):
        reasons.append("signal_family_transition")
    return (reasons[0] if reasons else None), reasons


def _fingerprint(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "capture_id": str(context["capture_id"]),
        "underlying_price": _number(context.get("underlying_price")),
        "avg_iv": _number(context.get("avg_iv")),
        "material_evidence_count": int(context.get("material_evidence_count") or 0),
        "signal_families": list(context.get("signal_families") or []),
    }


def _fingerprint_key(trigger: str, fingerprint: dict[str, Any]) -> str:
    raw = json.dumps({"trigger": trigger, **fingerprint}, sort_keys=True, default=str, separators=(",", ":"))
    return sha256(raw.encode("utf-8")).hexdigest()


def _event_payload(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": context["symbol"], "started_at": _iso(context.get("started_at")),
        "reference_price": _number(context.get("reference_price")), "event_low": _number(context.get("event_low")),
        "underlying_price": _number(context.get("underlying_price")), "avg_iv": _number(context.get("avg_iv")),
        "material_evidence_count": int(context.get("material_evidence_count") or 0),
        "signal_families": list(context.get("signal_families") or []),
    }


def _validate_output_for_persistence(output: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence, evidence_valid = validate_evidence(output["evidence"])
    persisted = {**output, "evidence": evidence, "mutation": None}
    mutation_status = "none"
    mutation = output.get("mutation")
    if mutation is not None:
        try:
            parameters = validate_mutation(str(mutation.get("strategy_key") or ""), dict(mutation.get("changes") or {}))
        except ValueError as exc:
            # Do not retain unsupported keys in the result payload: no agent
            # proposal exists until it compiles through the shared registry.
            mutation_status = "rejected_unsupported_mutation"
            mutation_error = str(exc)
        else:
            persisted["mutation"] = {
                "strategy_key": str(mutation["strategy_key"]), "changes": parameters,
                "status": "offline_proposal_only",
            }
            mutation_status = "offline_validated"
            mutation_error = None
    else:
        mutation_error = None
    validation = {
        "status": "accepted", "authority": "advisory_only", "evidence_valid": evidence_valid,
        "evidence_count": len(evidence), "mutation_status": mutation_status,
        "mutation_error": mutation_error,
    }
    return persisted, validation


def _role(task: dict[str, Any]) -> str:
    request = dict(task.get("request") or {})
    return str(request.get("role") or str(task.get("task_kind") or "").removeprefix("options_recovery_"))


def _token_counts(meta: dict[str, Any] | None) -> dict[str, int]:
    usage = dict((meta or {}).get("usage") or {})
    return {
        "input_tokens": max(0, int(usage.get("input_tokens") or 0)),
        "output_tokens": max(0, int(usage.get("output_tokens") or 0)),
    }


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ratio(numerator: Any, denominator: Any) -> float | None:
    count = int(denominator or 0)
    return int(numerator or 0) / count if count else None


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None
