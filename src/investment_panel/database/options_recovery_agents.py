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
    RECOVERY_AGENT_ROLES,
    normalize_recovery_agent_output,
    validate_evidence,
)
from investment_panel.core.options_recovery_registry import validate_mutation
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.database.options_recovery_agent_reporting import RecoveryAgentReporting
from investment_panel.database.options_recovery_agent_support import number as _number
from investment_panel.database.options_recovery_agent_support import role as _role
from investment_panel.database.options_recovery_cohorts import (
    CURRENT_CODE_VERSION,
    RecoveryCohortRepository,
)


MATERIAL_SPOT_MOVE = 0.02
MATERIAL_IV_CHANGE = 0.10
_NEW_YORK = ZoneInfo("America/New_York")


class RecoveryEventAgentRepository(RecoveryAgentReporting):
    """Queue bounded advisory batches and reject authority-bearing outputs."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime
        self.cohorts = RecoveryCohortRepository(runtime)

    def queue_if_material(
        self,
        event_id: str,
        *,
        capture_id: str | None = None,
        now: datetime | None = None,
        provider: str = "codex",
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
                f"""
                SELECT * FROM analysis.option_event_agent_batch
                WHERE event_id = %s AND cohort_id = %s::uuid
                  AND status IN ('queued', 'running', 'completed')
                ORDER BY created_at DESC LIMIT 1
                """,
                [event_id, context["cohort_id"]],
            ).fetchone()
            if preopen and connection.execute(
                f"""
                SELECT EXISTS (
                  SELECT 1 FROM analysis.option_event_agent_batch
                  WHERE event_id = %s AND trigger = 'preopen_review'
                    AND cohort_id = %s::uuid
                    AND (created_at AT TIME ZONE 'America/New_York')::date = %s
                ) AS present
                """,
                [event_id, context["cohort_id"], reference.astimezone(_NEW_YORK).date()],
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
                f"""
                SELECT batch.created_at
                FROM analysis.option_event_agent_batch batch
                JOIN analysis.option_event prior_event ON prior_event.id = batch.event_id
                WHERE prior_event.instrument_id = %s AND batch.status IN ('queued', 'running', 'completed')
                  AND batch.cohort_id = %s::uuid
                ORDER BY batch.created_at DESC LIMIT 1
                """,
                [context["instrument_id"], context["cohort_id"]],
            ).fetchone()
            if recent and (reference - recent["created_at"]).total_seconds() < max(0, debounce_minutes) * 60:
                return {"status": "skipped", "reason": "event_agent_debounced", "event_id": event_id}
            daily = int(connection.execute(
                """
                SELECT count(*) AS count
                FROM analysis.option_event_agent_batch batch
                JOIN analysis.option_event prior_event ON prior_event.id = batch.event_id
                WHERE prior_event.instrument_id = %s
                  AND batch.cohort_id = %s::uuid
                  AND (batch.created_at AT TIME ZONE 'America/New_York')::date = %s
                """,
                [context["instrument_id"], context["cohort_id"], reference.astimezone(_NEW_YORK).date()],
            ).fetchone()["count"] or 0)
            if daily >= max(0, max_batches_per_symbol_per_day):
                return {"status": "skipped", "reason": "event_agent_daily_batch_cap", "event_id": event_id}
            fingerprint = _fingerprint(context)
            fingerprint_key = _fingerprint_key(trigger, fingerprint)
            task_count = min(task_limit, len(RECOVERY_AGENT_ROLES))
            batch = connection.execute(
                f"""
                INSERT INTO analysis.option_event_agent_batch
                    (event_id, capture_id, cohort_id, trigger, fingerprint_key, fingerprint, provider, model,
                     reasoning_effort, status, task_count, telemetry)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued', %s, %s)
                ON CONFLICT (event_id, fingerprint_key) DO NOTHING
                RETURNING id
                """,
                [
                    event_id, context.get("capture_id"), context["cohort_id"], trigger, fingerprint_key, Jsonb(fingerprint),
                    provider, model, reasoning_effort, task_count,
                    Jsonb({
                        "reasons": reasons, "authority": "advisory_only", "attempts": 0,
                        "cohort_id": str(context["cohort_id"]), "code_version": CURRENT_CODE_VERSION,
                    }),
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
                    "evidence_bundle": list(context.get("evidence_bundle") or []),
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
        provider: str = "codex",
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
                f"SELECT id FROM analysis.option_event event WHERE status = 'active' "
                f"AND {self.cohorts.current_event_clause()} ORDER BY started_at"
            ).fetchall()]
        return [
            self.queue_if_material(
                event_id,
                now=reference,
                provider=provider,
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

        cohort_id = self.cohorts.current_id()
        if cohort_id is None:
            return None
        with self.runtime.transaction(JOB_PROFILE) as connection:
            batch = connection.execute(
                f"""
                SELECT batch.*
                FROM analysis.option_event_agent_batch batch
                JOIN analysis.option_event event ON event.id = batch.event_id
                WHERE batch.status = 'queued' AND batch.cohort_id = %s::uuid
                  AND {self.cohorts.current_event_clause(alias='event')}
                ORDER BY batch.created_at
                FOR UPDATE SKIP LOCKED LIMIT 1
                """
                , [cohort_id]
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
                VALUES (%s, %s, %s, now(), 'running', %s) RETURNING id
                """,
                [batch["provider"], batch["model"], f"options_recovery:{batch['trigger']}", Jsonb({
                    "workflow": "options_recovery_event_batch", "batch_id": str(batch["id"]),
                    "authority": "advisory_only", "task_count": len(tasks),
                    "cohort_id": str(batch["cohort_id"]), "code_version": CURRENT_CODE_VERSION,
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
                bundle = list((task.get("request") or {}).get("evidence_bundle") or [])
                persisted, validation = _validate_output_for_persistence(
                    output, evidence_bundle=bundle,
                )
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
                "cohort_id": str(batch["cohort_id"]),
                "code_version": CURRENT_CODE_VERSION,
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
        """Retry only transient Codex-process failures, at most twice.

        Claim identity and task rows stay stable across retries.  Schema,
        authority, and output-validation failures are terminal because retrying
        them could turn an advisory bug into repeated misleading evidence.
        """

        batch = dict(claim["batch"])
        message = str(error)[:2_000]
        token_counts = _token_counts(meta)
        attempts = max(0, int((batch.get("telemetry") or {}).get("attempts") or 0))
        retryable = _is_transient_codex_failure(message)
        retry = retryable and attempts < 2
        telemetry = {
            "token_usage": token_counts, "authority": "advisory_only",
            "cohort_id": str(batch["cohort_id"]), "code_version": CURRENT_CODE_VERSION,
            "attempts": attempts + 1, "retryable": retryable,
        }
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """UPDATE analysis.agent_run
                   SET status = 'failed', finished_at = now(), input_tokens = %s, output_tokens = %s,
                       summary = summary || %s WHERE id = %s""",
                [token_counts["input_tokens"], token_counts["output_tokens"], Jsonb({"error": message, **telemetry}), claim["run_id"]],
            )
            if retry:
                connection.execute(
                    """UPDATE analysis.agent_task
                       SET status = 'queued', agent_run_id = NULL,
                           validation = %s, updated_at = now()
                       WHERE request->>'recovery_event_batch_id' = %s AND status = 'running'""",
                    [Jsonb({"status": "retrying_transient_codex_failure", "error": message}), str(batch["id"])],
                )
                connection.execute(
                    """UPDATE analysis.option_event_agent_batch
                       SET status = 'queued', agent_run_id = NULL, started_at = NULL, finished_at = NULL,
                           error = %s, telemetry = telemetry || %s
                       WHERE id = %s""",
                    [message, Jsonb(telemetry), batch["id"]],
                )
                return {
                    "status": "retrying", "batch_id": str(batch["id"]), "error": message,
                    "attempt": attempts + 1,
                }
            connection.execute(
                """UPDATE analysis.agent_task
                   SET status = 'failed', validation = %s, updated_at = now()
                   WHERE request->>'recovery_event_batch_id' = %s AND status = 'running'""",
                [Jsonb({"status": "agent_failed", "authority": "advisory_only", "error": message}), str(batch["id"])],
            )
            connection.execute(
                """UPDATE analysis.option_event_agent_batch
                   SET status = 'failed', finished_at = now(), error = %s, telemetry = telemetry || %s
                   WHERE id = %s""",
                [message, Jsonb(telemetry), batch["id"]],
            )
        return {"status": "failed", "batch_id": str(batch["id"]), "error": message, "attempt": attempts + 1}

    def _event_context(self, connection: Any, event_id: str, capture_id: str | None) -> dict[str, Any] | None:
        event = connection.execute(
            f"""
            SELECT event.id, event.instrument_id, event.started_at, event.reference_price, event.event_low,
                   event.cohort_id, event.material_evidence_count, event.provenance, instrument.symbol
            FROM analysis.option_event event
            JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
            WHERE event.id = %s AND event.status = 'active'
              AND {self.cohorts.current_event_clause()}
            """,
            [event_id],
        ).fetchone()
        if event is None:
            return None
        if capture_id:
            capture = connection.execute(
                """
                SELECT id, snapshot_id, capture_generation_id, finished_at
                FROM analysis.option_event_capture
                WHERE event_id = %s AND id = %s AND status = 'complete'
                """,
                [event_id, capture_id],
            ).fetchone()
        else:
            capture = connection.execute(
                """
                SELECT id, snapshot_id, capture_generation_id, finished_at
                FROM analysis.option_event_capture
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
            WHERE contract.event_id = %s AND quote.snapshot_id = %s
              AND quote.capture_generation_id = %s AND quote.provider_iv IS NOT NULL
            """,
            [event_id, capture["snapshot_id"], capture.get("capture_generation_id")],
        ).fetchone()
        signals = connection.execute(
            """
            SELECT DISTINCT ON (strategy_key) strategy_key, status
            FROM analysis.option_event_signal
            WHERE event_id = %s AND cohort_id = %s::uuid
            ORDER BY strategy_key, available_at DESC, id DESC
            """,
            [event_id, event["cohort_id"]],
        ).fetchall()
        evidence_rows = connection.execute(
            """
            SELECT ('source_signal:' || signal.id::text) AS evidence_id,
                   item.source_id AS source, item.url, coalesce(signal.thesis, item.title, '') AS claim
            FROM analysis.source_signal signal
            JOIN raw.content_item item ON item.id = signal.content_item_id
            WHERE signal.instrument_id = %s AND signal.observed_at <= %s
            ORDER BY signal.observed_at DESC, signal.id DESC LIMIT 24
            """,
            [event["instrument_id"], capture["finished_at"]],
        ).fetchall()
        return {
            **dict(event), "capture_id": str(capture["id"]), "capture_finished_at": capture["finished_at"],
            "complete_capture_count": int(complete_count["count"] or 0),
            "underlying_price": _number(spot["price"]) if spot else None,
            "underlying_available_at": spot["available_at"] if spot else None,
            "avg_iv": _number(iv["avg_iv"]),
            "signal_families": sorted(f"{row['strategy_key']}:{row['status']}" for row in signals),
            "evidence_bundle": [dict(row) for row in evidence_rows],
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


def _validate_output_for_persistence(
    output: dict[str, Any],
    *,
    evidence_bundle: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence, proposals, evidence_valid = validate_evidence(
        output["evidence"], evidence_bundle=evidence_bundle,
    )
    persisted = {
        **output, "evidence": evidence, "unverified_evidence_proposals": proposals,
        "mutation": None,
    }
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
        "evidence_count": len(evidence), "unverified_evidence_proposal_count": len(proposals),
        "mutation_status": mutation_status,
        "mutation_error": mutation_error,
    }
    return persisted, validation


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


def _is_transient_codex_failure(message: str) -> bool:
    value = message.lower()
    # Never retried: schema/authority/output errors are deterministic defects.
    if any(token in value for token in (
        "schema", "validation", "authority", "forbidden", "unsupported",
        "malformed", "does not match", "invalid json",
    )):
        return False
    return any(token in value for token in (
        "timeout", "timed out", "connection", "connection reset", "broken pipe",
        "process exited", "process failed", "temporar", "rate limit", "econn",
    ))


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None
