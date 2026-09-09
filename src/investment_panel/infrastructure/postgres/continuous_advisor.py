"""PostgreSQL persistence for the continuous advisor.

The provider remains behind the existing advisory seam.  This repository only
owns immutable packet/response/claim lineage and the mutable agent-task lease.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json
from typing import Any, Mapping
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.core.continuous_advisor import MIN_TEST_MATCHES, response_claims, score_claims
from investment_panel.infrastructure.postgres.instruments import canonical_symbol
from investment_panel.infrastructure.postgres.migrations import HEAD_REVISION
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.infrastructure.postgres.thesis import save_thesis_on_connection


CONTINUOUS_ADVISOR_LEASE_MINUTES = 15


class ContinuousAdvisorRepository:
    """Store append-only advisor lineage and single-flight task leases."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def ensure_prompt_version(self, prompt: Mapping[str, Any]) -> str:
        version = str(prompt.get("version") or "").strip()
        if not version:
            raise ValueError("prompt version is required")
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                "SELECT analysis.write_continuous_advisor_prompt(%s) AS version",
                [Jsonb(_jsonable({
                    "version": version,
                    "parent_version": prompt.get("parent_version"),
                    "template": prompt.get("template") or {},
                    "approved_change_set": prompt.get("approved_change_set") or [],
                    "mutation_rationale": prompt.get("mutation_rationale"),
                }))],
            )
        return version

    def active_prompt_version(self, default_version: str) -> str:
        with self.runtime.read(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                SELECT decision, candidate_prompt_version, previous_active_prompt_version
                FROM analysis.continuous_advisor_promotion_decision
                ORDER BY created_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
        if not row:
            return str(default_version)
        decision = str(row["decision"])
        selected = str(
            row["candidate_prompt_version"] if decision == "activate"
            else row["previous_active_prompt_version"] or default_version
        )
        if selected == str(default_version):
            return selected
        with self.runtime.read(JOB_PROFILE) as connection:
            descendant = connection.execute(
                """
                WITH RECURSIVE lineage(version) AS (
                    SELECT %s::text
                    UNION
                    SELECT prompt.version
                    FROM analysis.continuous_advisor_prompt_version prompt
                    JOIN lineage ON lineage.version = prompt.parent_version
                )
                SELECT 1 FROM lineage WHERE version = %s::text LIMIT 1
                """,
                [str(default_version), selected],
            ).fetchone()
        return selected if descendant else str(default_version)

    def prompt_template(self, version: str) -> dict[str, Any]:
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                WITH RECURSIVE lineage(version, parent_version, template, depth, path) AS (
                    SELECT prompt.version, prompt.parent_version, prompt.template, 0,
                           ARRAY[prompt.version]::text[]
                    FROM analysis.continuous_advisor_prompt_version prompt
                    WHERE prompt.version = %s
                    UNION ALL
                    SELECT parent.version, parent.parent_version, parent.template,
                           lineage.depth + 1, lineage.path || parent.version
                    FROM analysis.continuous_advisor_prompt_version parent
                    JOIN lineage ON lineage.parent_version = parent.version
                    WHERE NOT parent.version = ANY(lineage.path)
                )
                SELECT template FROM lineage ORDER BY depth DESC
                """,
                [version],
            ).fetchall()
        merged: dict[str, Any] = {}
        for row in rows:
            template = row["template"]
            if isinstance(template, dict):
                merged.update(template)
        return _jsonable(merged)

    def store_packet(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        symbol = canonical_symbol(packet.get("symbol"))
        with self.runtime.transaction(JOB_PROFILE) as connection:
            instrument = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = %s", [symbol]
            ).fetchone()
            if instrument is None:
                raise ValueError(f"packet instrument is not in the monitored catalog: {symbol}")
            row = connection.execute(
                "SELECT analysis.write_continuous_advisor_packet(%s) AS id",
                [Jsonb(_jsonable({
                    "instrument_id": instrument["id"],
                    "symbol": symbol,
                    "cutoff": packet["cutoff"],
                    "slot_start": packet["slot_start"],
                    "fingerprint": packet["fingerprint"],
                    "prompt_version": packet["prompt_version"],
                    "source_refs": packet.get("source_references") or [],
                    "blockers": packet.get("blockers") or [],
                    "packet": dict(packet),
                }))],
            ).fetchone()
            if row is None:  # pragma: no cover - protected by the unique constraint
                raise RuntimeError("continuous advisor packet was not persisted")
            stored = connection.execute(
                """
                SELECT id, symbol, cutoff, slot_start, fingerprint, prompt_version,
                       source_refs, blockers, packet
                FROM analysis.continuous_advisor_packet
                WHERE id = %s
                """,
                [row["id"]],
            ).fetchone()
            if stored is None:  # pragma: no cover - the row is locked by this transaction
                raise RuntimeError("continuous advisor packet disappeared after persistence")
        stored_packet = _jsonable(stored["packet"] or {})
        if not isinstance(stored_packet, dict):  # pragma: no cover - constrained by the JSON contract
            raise RuntimeError("continuous advisor packet has invalid stored JSON")
        stored_packet.update(
            {
                "symbol": stored["symbol"],
                "cutoff": _jsonable(stored["cutoff"]),
                "slot_start": _jsonable(stored["slot_start"]),
                "fingerprint": stored["fingerprint"],
                "prompt_version": stored["prompt_version"],
                "source_references": _jsonable(stored["source_refs"] or []),
                "blockers": _jsonable(stored["blockers"] or []),
            }
        )
        return {**stored_packet, "id": str(stored["id"])}

    def should_review(
        self,
        packet: Mapping[str, Any],
        *,
        now: datetime,
        stale_after_minutes: int,
    ) -> tuple[bool, str]:
        symbol = canonical_symbol(packet.get("symbol"))
        with self.runtime.read(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                SELECT p.fingerprint, r.status, r.finished_at
                FROM analysis.continuous_advisor_response r
                JOIN analysis.continuous_advisor_packet p ON p.id = r.packet_id
                WHERE r.symbol = %s AND r.prompt_version = %s
                ORDER BY r.finished_at DESC NULLS LAST, r.created_at DESC
                LIMIT 1
                """,
                [symbol, str(packet.get("prompt_version") or "")],
            ).fetchone()
        if row is None:
            return True, "first_review"
        if str(row["fingerprint"]) != str(packet.get("fingerprint")):
            return True, "evidence_changed"
        finished = _as_datetime(row["finished_at"])
        if finished is None or now - finished >= timedelta(minutes=max(1, stale_after_minutes)):
            return True, "stale_review"
        if str(row["status"]) != "succeeded":
            return True, "previous_advisory_failure"
        catalysts = ((packet.get("evidence") or {}).get("catalysts") or [])
        for catalyst in catalysts if isinstance(catalysts, list) else []:
            if not isinstance(catalyst, dict):
                continue
            starts_at = _as_datetime(catalyst.get("starts_at"))
            if starts_at and now <= starts_at <= now + timedelta(hours=2) and finished < starts_at:
                return True, "catalyst_due"
        return False, "unchanged"

    def claim_review(
        self,
        packet: Mapping[str, Any],
        *,
        request: Mapping[str, Any],
        provider: str,
        model: str,
        reasoning_effort: str,
        lease_minutes: int = CONTINUOUS_ADVISOR_LEASE_MINUTES,
    ) -> dict[str, Any]:
        """Claim a packet slot through analysis.agent_task exactly once."""

        packet_id = str(packet["id"])
        symbol = canonical_symbol(packet.get("symbol"))
        slot_start = str(packet["slot_start"])
        prompt_version = str(packet["prompt_version"])
        now = datetime.now(UTC)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                "SELECT id FROM analysis.continuous_advisor_packet WHERE id = %s FOR UPDATE",
                [packet_id],
            )
            existing = connection.execute(
                """
                SELECT id, status, updated_at, agent_run_id
                FROM analysis.agent_task
                WHERE task_kind = 'continuous_advisor'
                  AND request->>'packet_id' = %s
                  AND request->>'slot_start' = %s
                  AND prompt_version = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                [packet_id, slot_start, prompt_version],
            ).fetchone()
            if existing:
                status = str(existing["status"])
                updated = _as_datetime(existing["updated_at"])
                if status in {"queued", "running"} and updated and now - updated < timedelta(minutes=max(1, lease_minutes)):
                    return {"task_id": str(existing["id"]), "agent_run_id": str(existing["agent_run_id"] or ""), "created": False, "reason": "lease_active", "status": status}
                if status in {"completed", "skipped", "failed", "invalid"}:
                    return {"task_id": str(existing["id"]), "agent_run_id": str(existing["agent_run_id"] or ""), "created": False, "reason": "slot_already_recorded", "status": status}
                if status in {"queued", "running"}:
                    connection.execute(
                        """
                        UPDATE analysis.agent_task
                        SET status = 'failed', updated_at = now(), validation_status = 'failed',
                            validation_detail = jsonb_build_object('reason', 'lease_expired')
                        WHERE id = %s
                        """,
                        [existing["id"]],
                    )
                    if existing["agent_run_id"]:
                        connection.execute(
                            "UPDATE analysis.agent_run SET status = 'failed', finished_at = now(), validation_status = 'failed', validation_detail = jsonb_build_object('reason', 'lease_expired') WHERE id = %s",
                            [existing["agent_run_id"]],
                        )
            agent_run = connection.execute(
                """
                INSERT INTO analysis.agent_run (
                    provider, model, trigger, started_at, status, summary,
                    evidence_fingerprint, prompt_version, schema_version, baseline_version
                )
                VALUES (%s, %s, 'continuous_advisor', now(), 'running', %s, %s, %s, %s, %s)
                RETURNING id
                """,
                [
                    provider, model,
                    Jsonb(_jsonable({"workflow": "continuous_advisor", "symbol": symbol, "packet_id": packet_id})),
                    packet.get("fingerprint"), prompt_version, "continuous-advisor.v1", HEAD_REVISION,
                ],
            ).fetchone()
            task = connection.execute(
                """
                INSERT INTO analysis.agent_task (
                    agent_run_id, task_kind, status, request, provider, model,
                    evidence_fingerprint, prompt_version, schema_version, baseline_version,
                    validation_status, validation_detail, created_at, updated_at
                )
                VALUES (%s, 'continuous_advisor', 'running', %s, %s, %s, %s, %s, %s, %s, 'pending', '{}', now(), now())
                RETURNING id
                """,
                [
                    agent_run["id"], Jsonb(_jsonable(dict(request))), provider, model,
                    packet.get("fingerprint"), prompt_version, "continuous-advisor.v1", HEAD_REVISION,
                ],
            ).fetchone()
        return {"task_id": str(task["id"]), "agent_run_id": str(agent_run["id"]), "created": True, "reason": "claimed", "status": "running"}

    def finish_review(
        self,
        task_id: str,
        *,
        status: str,
        response: Mapping[str, Any] | None = None,
        validation: Mapping[str, Any] | None = None,
        error: str | None = None,
        usage: Mapping[str, Any] | None = None,
        cost_usd: float | None = None,
        latency_ms: int | None = None,
        lease_minutes: int = CONTINUOUS_ADVISOR_LEASE_MINUTES,
        thesis_update: Mapping[str, Any] | None = None,
        expected_thesis_revision: int | None = None,
    ) -> dict[str, Any]:
        status = str(status)
        if status not in {"succeeded", "failed", "skipped", "invalid"}:
            raise ValueError(f"invalid continuous advisor response status: {status}")
        output = dict(response or {})
        validation_value = dict(validation or {})
        if error:
            validation_value["error"] = str(error)[:2_000]
        usage = dict(usage or {})
        with self.runtime.transaction(JOB_PROFILE) as connection:
            task = connection.execute(
                "SELECT id, agent_run_id, provider, model, prompt_version, status, updated_at, created_at, request FROM analysis.agent_task WHERE id = %s FOR UPDATE",
                [task_id],
            ).fetchone()
            if task is None:
                raise ValueError(f"continuous advisor task not found: {task_id}")
            now = datetime.now(UTC)
            updated = _as_datetime(task["updated_at"])
            if str(task["status"]) != "running":
                return {"task_id": task_id, "status": "ignored", "reason": "task_lease_lost", "ignored": True}
            if updated is None or now - updated >= timedelta(minutes=max(1, lease_minutes)):
                connection.execute(
                    """
                    UPDATE analysis.agent_task
                    SET status = 'failed', validation_status = 'failed',
                        validation_detail = jsonb_build_object('reason', 'lease_expired'), updated_at = now()
                    WHERE id = %s AND status = 'running'
                    """,
                    [task_id],
                )
                if task["agent_run_id"]:
                    connection.execute(
                        """
                        UPDATE analysis.agent_run
                        SET status = 'failed', finished_at = now(), validation_status = 'failed',
                            validation_detail = jsonb_build_object('reason', 'lease_expired')
                        WHERE id = %s AND status = 'running'
                        """,
                        [task["agent_run_id"]],
                    )
                return {"task_id": task_id, "status": "ignored", "reason": "lease_expired", "ignored": True}
            existing = connection.execute(
                "SELECT id FROM analysis.continuous_advisor_response WHERE task_id = %s",
                [task_id],
            ).fetchone()
            if existing:
                return {"response_id": str(existing["id"]), "task_id": task_id, "status": status, "duplicate": True}
            packet_id = str((task["request"] or {}).get("packet_id") or "")
            if not packet_id:
                raise ValueError("continuous advisor task has no packet reference")
            started = _as_datetime(task["created_at"]) or datetime.now(UTC)
            finished = datetime.now(UTC)
            latency = latency_ms if latency_ms is not None else max(0, int((finished - started).total_seconds() * 1_000))
            response_row = connection.execute(
                "SELECT analysis.write_continuous_advisor_response(%s) AS id",
                [Jsonb(_jsonable({
                    "packet_id": packet_id,
                    "task_id": task_id,
                    "agent_run_id": task["agent_run_id"],
                    "symbol": (task["request"] or {}).get("symbol"),
                    "prompt_version": task["prompt_version"],
                    "provider": task["provider"],
                    "model": task["model"],
                    "reasoning_effort": (task["request"] or {}).get("reasoning_effort") or usage.get("reasoning_effort"),
                    "status": status,
                    "response": output,
                    "validation": validation_value,
                    "started_at": started,
                    "finished_at": finished,
                    "latency_ms": latency,
                    "input_tokens": _int_or_none(usage.get("input_tokens")),
                    "output_tokens": _int_or_none(usage.get("output_tokens")),
                    "cost_usd": cost_usd,
                }))],
            ).fetchone()
            if response_row is None:  # pragma: no cover - the writer returns an id
                raise RuntimeError("continuous advisor response was not persisted")
            response_id = str(response_row["id"])
            if status == "succeeded":
                for claim in response_claims(output):
                    claim_key = str(claim.get("claim_key") or claim.get("claim_id") or "").strip()
                    if not claim_key:
                        continue
                    connection.execute(
                        "SELECT analysis.write_continuous_advisor_claim(%s) AS id",
                        [Jsonb(_jsonable({
                            "response_id": response_id,
                            "packet_id": packet_id,
                            "symbol": str((task["request"] or {}).get("symbol") or "").upper(),
                            "claim_key": claim_key,
                            "claim_kind": claim.get("claim_kind"),
                            "horizon": claim.get("horizon"),
                            "statement": claim.get("statement") or claim.get("condition"),
                            "direction": claim.get("direction"),
                            "probability": float(claim.get("probability") or 0),
                            "target": claim.get("target"),
                            "evidence_refs": claim.get("evidence_refs") or [],
                            "claim": claim,
                        }))],
                    )
                if thesis_update is not None:
                    save_thesis_on_connection(
                        connection,
                        str((task["request"] or {}).get("symbol") or ""),
                        dict(thesis_update),
                        expected_revision=expected_thesis_revision,
                    )
            task_status = "completed" if status == "succeeded" else status
            connection.execute(
                """
                UPDATE analysis.agent_task
                SET status = %s, result = %s, validation = %s, validation_status = %s,
                    validation_detail = %s, updated_at = now(), result_available_at = now(),
                    validation_available_at = now(), latency_ms = %s, input_tokens = %s,
                    output_tokens = %s, cost_usd = %s
                WHERE id = %s
                """,
                [
                    task_status, Jsonb(_jsonable(output)), Jsonb(_jsonable(validation_value)),
                    "passed" if status == "succeeded" else status, Jsonb(_jsonable(validation_value)),
                    latency, _int_or_none(usage.get("input_tokens")), _int_or_none(usage.get("output_tokens")), cost_usd, task_id,
                ],
            )
            connection.execute(
                """
                UPDATE analysis.agent_run
                SET status = %s, finished_at = now(), input_tokens = %s, output_tokens = %s,
                    cost_usd = %s, latency_ms = %s, validation_status = %s, validation_detail = %s
                WHERE id = %s
                """,
                [
                    "succeeded" if status == "succeeded" else status,
                    _int_or_none(usage.get("input_tokens")), _int_or_none(usage.get("output_tokens")),
                    cost_usd, latency, "passed" if status == "succeeded" else status,
                    Jsonb(_jsonable(validation_value)), task["agent_run_id"],
                ],
            )
        return {"response_id": response_id, "task_id": task_id, "status": status, "duplicate": False}

    def create_health_alert(self, symbol: str, detail: str) -> None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            instrument = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = %s", [canonical_symbol(symbol)]
            ).fetchone()
            connection.execute(
                "INSERT INTO app.alert (instrument_id, alert_type, severity, title, detail) VALUES (%s, 'continuous_advisor_health', 'warning', 'Continuous advisor failed', %s)",
                [instrument["id"] if instrument else None, str(detail)[:1_000]],
            )

    def list_runs(self, *, symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        normalized = canonical_symbol(symbol) if symbol else None
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT task.id AS task_id, task.status AS task_status, task.provider, task.model,
                       task.prompt_version, task.evidence_fingerprint, task.created_at,
                       task.updated_at, task.validation_status, task.validation_detail,
                       task.latency_ms, task.input_tokens, task.output_tokens, task.cost_usd,
                       task.request->>'symbol' AS symbol, task.request->>'packet_id' AS packet_id,
                       packet.cutoff, packet.slot_start, response.id AS response_id,
                       response.status AS response_status, response.finished_at,
                       response.response, response.validation
                FROM analysis.agent_task task
                LEFT JOIN analysis.continuous_advisor_packet packet ON packet.id::text = task.request->>'packet_id'
                LEFT JOIN analysis.continuous_advisor_response response ON response.task_id = task.id
                WHERE task.task_kind = 'continuous_advisor'
                  AND (%s::text IS NULL OR task.request->>'symbol' = %s::text)
                ORDER BY task.created_at DESC, task.id DESC LIMIT %s
                """,
                [normalized, normalized, max(1, min(200, int(limit)))],
            ).fetchall()
        return [_jsonable(dict(row)) for row in rows]

    def ticker_briefs(
        self, *, symbols: list[str] | None = None, prompt_version: str | None = None
    ) -> list[dict[str, Any]]:
        normalized = sorted({canonical_symbol(symbol) for symbol in symbols or []}) or None
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                WITH latest_success AS (
                    SELECT DISTINCT ON (response.symbol)
                           response.symbol, response.id AS response_id, response.task_id,
                           response.prompt_version, response.finished_at, response.response,
                           response.validation, packet.id AS packet_id, packet.cutoff,
                           packet.fingerprint, packet.source_refs, packet.blockers,
                           packet.packet->'freshness' AS freshness
                    FROM analysis.continuous_advisor_response response
                    JOIN analysis.continuous_advisor_packet packet ON packet.id = response.packet_id
                    WHERE response.status = 'succeeded'
                      AND (%s::text[] IS NULL OR response.symbol = ANY(%s::text[]))
                      AND (%s::text IS NULL OR response.prompt_version = %s::text)
                    ORDER BY response.symbol, response.finished_at DESC NULLS LAST, response.id DESC
                ), latest_run AS (
                    SELECT DISTINCT ON (response.symbol)
                           response.symbol, response.id AS latest_response_id,
                           response.task_id AS latest_task_id,
                           response.prompt_version AS latest_prompt_version,
                           response.status AS latest_response_status,
                           response.validation AS latest_validation,
                           response.finished_at AS latest_finished_at,
                           packet.id AS latest_packet_id, packet.cutoff AS latest_cutoff,
                           packet.fingerprint AS latest_fingerprint,
                           packet.source_refs AS latest_source_refs,
                           packet.blockers AS latest_blockers,
                           packet.packet->'freshness' AS latest_freshness
                    FROM analysis.continuous_advisor_response response
                    JOIN analysis.continuous_advisor_packet packet ON packet.id = response.packet_id
                    WHERE (%s::text[] IS NULL OR response.symbol = ANY(%s::text[]))
                      AND (%s::text IS NULL OR response.prompt_version = %s::text)
                    ORDER BY response.symbol, response.finished_at DESC NULLS LAST, response.id DESC
                )
                SELECT latest_run.symbol,
                       coalesce(latest_success.task_id, latest_run.latest_task_id) AS task_id,
                       coalesce(latest_success.prompt_version, latest_run.latest_prompt_version) AS prompt_version,
                       coalesce(latest_success.finished_at, latest_run.latest_finished_at) AS finished_at,
                       coalesce(latest_success.response_id, latest_run.latest_response_id) AS response_id,
                       coalesce(latest_success.response, '{}'::jsonb) AS response,
                       coalesce(latest_success.validation, '{}'::jsonb) AS validation,
                       coalesce(latest_success.packet_id, latest_run.latest_packet_id) AS packet_id,
                       coalesce(latest_success.cutoff, latest_run.latest_cutoff) AS cutoff,
                       coalesce(latest_success.fingerprint, latest_run.latest_fingerprint) AS fingerprint,
                       coalesce(latest_success.source_refs, latest_run.latest_source_refs) AS source_refs,
                       coalesce(latest_success.blockers, latest_run.latest_blockers) AS blockers,
                       coalesce(latest_success.freshness, latest_run.latest_freshness) AS freshness,
                       CASE WHEN latest_success.response_id IS NULL
                            THEN latest_run.latest_response_status ELSE 'succeeded' END AS response_status,
                       latest_run.latest_response_id, latest_run.latest_response_status,
                       latest_run.latest_validation, latest_run.latest_finished_at,
                       latest_run.latest_blockers
                FROM latest_run
                LEFT JOIN latest_success ON latest_success.symbol = latest_run.symbol
                """,
                [normalized, normalized, prompt_version, prompt_version, normalized, normalized, prompt_version, prompt_version],
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            response = dict(row["response"] or {})
            response_status = str(row["response_status"] or "unknown")
            validation = dict(row["validation"] or {})
            blockers = list(row["blockers"] or [])
            latest_status = str(row["latest_response_status"] or response_status)
            blockers.extend(row["latest_blockers"] or [])
            if latest_status != "succeeded":
                blockers.append(f"latest_run_{latest_status}")
                latest_validation = dict(row["latest_validation"] or {})
                if latest_validation.get("reason"):
                    blockers.append(str(latest_validation["reason"]))
            if response_status != "succeeded" and validation.get("reason"):
                blockers.append(str(validation["reason"]))
            output.append({
                "symbol": row["symbol"],
                "verdict": {
                    "thesis": ((response.get("thesis") or {}).get("core_thesis") or "") if response_status == "succeeded" else "",
                    "countercase": response.get("countercase") or ("No verdict recorded for the latest cycle." if response_status != "succeeded" else ""),
                    "forecasts": [
                        {key: claim.get(key) for key in ("claim_key", "statement", "horizon", "direction", "probability")}
                        for claim in response.get("forecasts") or [] if isinstance(claim, dict) and response_status == "succeeded"
                    ],
                    "invalidations": response.get("invalidations") or [] if response_status == "succeeded" else [],
                    "change_since_prior": response.get("change_since_prior") or ("Latest cycle failed or was skipped." if response_status != "succeeded" else ""),
                    "next_review_trigger": response.get("next_review_trigger") or ("Resolve the latest advisor blocker" if response_status != "succeeded" else ""),
                    "next_review_at": response.get("next_review_at"),
                    "outcome_date": response.get("outcome_date"),
                    "evidence_freshness": _jsonable(row["freshness"] or {}),
                    "blockers": sorted(set(_jsonable(blockers))),
                },
                "provenance": {
                    "packet_id": str(row["packet_id"]),
                    "response_id": str(row["response_id"]) if row["response_id"] is not None else None,
                    "task_id": str(row["task_id"]),
                    "response_status": response_status,
                    "cutoff": row["cutoff"],
                    "fingerprint": row["fingerprint"],
                    "prompt_version": row["prompt_version"],
                    "source_refs": _jsonable(row["source_refs"] or []),
                    "latest_response_id": str(row["latest_response_id"]) if row["latest_response_id"] is not None else None,
                    "latest_response_status": latest_status,
                    "latest_finished_at": row["latest_finished_at"],
                },
            })
        return _jsonable(output)

    def replay_scorecards(self, *, prompt_version: str | None = None) -> list[dict[str, Any]]:
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                       SELECT response.id AS response_id, response.prompt_version, response.latency_ms, response.cost_usd AS cost,
                       packet.cutoff, claim.id AS claim_id, claim.claim_key, claim.claim_kind,
                       claim.horizon, claim.direction, claim.probability,
                       outcome.status, outcome.correct, outcome.excess_return,
                       outcome.invalidation_correct, outcome.evidence_valid
                FROM analysis.continuous_advisor_forecast_claim claim
                JOIN analysis.continuous_advisor_response response ON response.id = claim.response_id
                JOIN analysis.continuous_advisor_packet packet ON packet.id = claim.packet_id
                LEFT JOIN analysis.continuous_advisor_forecast_outcome outcome ON outcome.claim_id = claim.id
                WHERE (%s::text IS NULL OR response.prompt_version = %s::text)
                ORDER BY response.prompt_version, claim.created_at, claim.claim_key
                """,
                [prompt_version, prompt_version],
            ).fetchall()
            response_rows = connection.execute(
                """
                SELECT prompt_version, count(*) AS response_count,
                       max(created_at) AS last_response_at,
                       count(*) FILTER (WHERE status = 'succeeded' AND validation @> '{\"schema_valid\": true}'::jsonb) AS schema_valid_count,
                       count(*) FILTER (WHERE status = 'succeeded' AND validation @> '{\"evidence_valid\": true}'::jsonb) AS evidence_valid_count
                FROM analysis.continuous_advisor_response
                WHERE (%s::text IS NULL OR prompt_version = %s::text)
                GROUP BY prompt_version
                """,
                [prompt_version, prompt_version],
            ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for raw in rows:
            row = dict(raw)
            version = str(row["prompt_version"])
            bucket = grouped.setdefault(version, {"claims": [], "outcomes": [], "latency": [], "cost": 0.0, "responses_seen": set(), "observations": []})
            bucket["claims"].append({"claim_id": str(row["claim_id"]), "claim_key": row["claim_key"], "claim_kind": row["claim_kind"], "horizon": row["horizon"], "direction": row["direction"], "probability": row["probability"]})
            if row["status"]:
                outcome = {"claim_id": str(row["claim_id"]), "claim_key": row["claim_key"], "status": row["status"], "correct": row["correct"], "excess_return": row["excess_return"], "invalidation_correct": row["invalidation_correct"], "evidence_valid": row["evidence_valid"]}
                bucket["outcomes"].append(outcome)
                bucket["observations"].append({"claim": bucket["claims"][-1], "outcome": outcome, "cutoff": row["cutoff"]})
            response_id = str(row["response_id"])
            if response_id not in bucket["responses_seen"] and row["latency_ms"] is not None:
                bucket["latency"].append(float(row["latency_ms"]))
            if response_id not in bucket["responses_seen"]:
                bucket["cost"] += float(row["cost"] or 0)
                bucket["responses_seen"].add(response_id)
        for raw in response_rows:
            row = dict(raw)
            bucket = grouped.setdefault(str(row["prompt_version"]), {"claims": [], "outcomes": [], "latency": [], "cost": 0.0, "responses_seen": set(), "observations": []})
            bucket["response_count"] = int(row["response_count"] or 0)
            bucket["last_response_at"] = row["last_response_at"]
            bucket["schema_valid_count"] = int(row["schema_valid_count"] or 0)
            bucket["evidence_valid_count"] = int(row["evidence_valid_count"] or 0)
        scorecards = []
        for version, bucket in sorted(grouped.items()):
            scorecard = {"prompt_version": version, **score_claims(bucket["claims"], bucket["outcomes"], latency_ms=bucket["latency"], token_cost_usd=bucket["cost"])}
            response_count = int(bucket.get("response_count") or 0)
            scorecard["response_count"] = response_count
            scorecard["last_response_at"] = bucket.get("last_response_at")
            scorecard["schema_validity_rate"] = (int(bucket.get("schema_valid_count") or 0) / response_count) if response_count else 0.0
            scorecard["response_evidence_validity_rate"] = (int(bucket.get("evidence_valid_count") or 0) / response_count) if response_count else 0.0
            if response_count:
                scorecard["evidence_validity_rate"] = scorecard["response_evidence_validity_rate"]
            observations = sorted(bucket["observations"], key=lambda item: str(item["cutoff"]))
            holdout_count = max(1, len(observations) // 5) if observations else 0
            holdout = observations[-holdout_count:] if holdout_count else []
            holdout_score = score_claims(
                [item["claim"] for item in holdout], [item["outcome"] for item in holdout]
            )
            scorecard["walk_forward_scorecard"] = {
                "status": "pass" if holdout_score["matched_outcomes"] >= MIN_TEST_MATCHES else "pending",
                "holdout": holdout_score,
                "holdout_cutoff": holdout[0]["cutoff"] if holdout else None,
            }
            if observations:
                scorecard["cutoff_start"] = observations[0]["cutoff"]
                scorecard["cutoff_end"] = observations[-1]["cutoff"]
            scorecards.append(scorecard)
        return scorecards

    def matched_cohort_scorecards(self, active_prompt_version: str, candidate_prompt_version: str) -> dict[str, Any]:
        """Score active and challenger responses on identical frozen packets."""

        versions = [str(active_prompt_version), str(candidate_prompt_version)]
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT response.prompt_version, response.id AS response_id,
                       response.latency_ms, response.cost_usd, packet.cutoff,
                       packet.packet - 'prompt_version' - 'fingerprint' AS frozen_packet,
                       claim.id AS claim_id, claim.claim_key, claim.claim_kind,
                       claim.horizon, claim.direction, claim.probability, outcome.status,
                       outcome.correct, outcome.excess_return,
                       outcome.invalidation_correct, outcome.evidence_valid
                FROM analysis.continuous_advisor_forecast_claim claim
                JOIN analysis.continuous_advisor_response response ON response.id = claim.response_id
                JOIN analysis.continuous_advisor_packet packet ON packet.id = claim.packet_id
                LEFT JOIN analysis.continuous_advisor_forecast_outcome outcome ON outcome.claim_id = claim.id
                WHERE response.prompt_version = ANY(%s::text[])
                  AND response.status = 'succeeded'
                ORDER BY packet.cutoff ASC, response.prompt_version, claim.created_at, claim.claim_key
                """,
                [versions],
            ).fetchall()
        buckets: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in rows:
            row = dict(raw)
            version = str(row["prompt_version"])
            frozen_key = json.dumps(_jsonable(row["frozen_packet"] or {}), sort_keys=True, separators=(",", ":"))
            key = (version, frozen_key)
            bucket = buckets.setdefault(
                key,
                {"cutoff": row["cutoff"], "claims": [], "outcomes": [], "responses": {}},
            )
            claim = {
                "claim_id": str(row["claim_id"]),
                "claim_key": row["claim_key"],
                "claim_kind": row["claim_kind"],
                "horizon": row["horizon"],
                "direction": row["direction"],
                "probability": row["probability"],
            }
            bucket["claims"].append(claim)
            if row["status"]:
                bucket["outcomes"].append({
                    "claim_id": str(row["claim_id"]),
                    "claim_key": row["claim_key"],
                    "status": row["status"],
                    "correct": row["correct"],
                    "excess_return": row["excess_return"],
                    "invalidation_correct": row["invalidation_correct"],
                    "evidence_valid": row["evidence_valid"],
                })
            response_id = str(row["response_id"])
            bucket["responses"].setdefault(response_id, {
                "latency_ms": row["latency_ms"],
                "cost_usd": float(row["cost_usd"] or 0),
            })

        active_keys = {frozen for version, frozen in buckets if version == versions[0]}
        candidate_keys = {frozen for version, frozen in buckets if version == versions[1]}
        common_keys = sorted(
            active_keys & candidate_keys,
            key=lambda frozen: str(buckets[(versions[0], frozen)]["cutoff"]),
        )
        holdout_count = max(1, len(common_keys) // 5) if common_keys else 0
        holdout_keys = common_keys[-holdout_count:] if holdout_count else []

        def score(version: str, keys: list[str]) -> dict[str, Any]:
            claims: list[dict[str, Any]] = []
            outcomes: list[dict[str, Any]] = []
            latencies: list[float] = []
            cost = 0.0
            for frozen in keys:
                bucket = buckets[(version, frozen)]
                claims.extend(bucket["claims"])
                outcomes.extend(bucket["outcomes"])
                for response in bucket["responses"].values():
                    if response["latency_ms"] is not None:
                        latencies.append(float(response["latency_ms"]))
                    cost += float(response["cost_usd"] or 0)
            return score_claims(claims, outcomes, latency_ms=latencies, token_cost_usd=cost)

        active_full = score(versions[0], common_keys)
        candidate_full = score(versions[1], common_keys)
        active_holdout = score(versions[0], holdout_keys)
        candidate_holdout = score(versions[1], holdout_keys)
        return _jsonable({
            "status": "pass" if common_keys else "pending",
            "common_frozen_packets": len(common_keys),
            "common_cutoffs": [buckets[(versions[0], frozen)]["cutoff"] for frozen in common_keys],
            "holdout_cutoff": buckets[(versions[0], holdout_keys[0])]["cutoff"] if holdout_keys else None,
            "active": {"full": active_full, "holdout": active_holdout},
            "candidate": {"full": candidate_full, "holdout": candidate_holdout},
        })

    def record_outcome(self, claim_id: str, outcome: Mapping[str, Any]) -> str:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            claim = connection.execute(
                "SELECT response_id, packet_id, symbol FROM analysis.continuous_advisor_forecast_claim WHERE id = %s",
                [claim_id],
            ).fetchone()
            if claim is None:
                raise ValueError(f"forecast claim not found: {claim_id}")
            row = connection.execute(
                "SELECT analysis.write_continuous_advisor_outcome(%s) AS id",
                [Jsonb(_jsonable({
                    "claim_id": claim_id,
                    "status": outcome.get("status", "unresolvable"),
                    "measured_through": outcome.get("measured_through"),
                    "actual_return": outcome.get("actual_return"),
                    "excess_return": outcome.get("excess_return"),
                    "actual_direction": outcome.get("actual_direction"),
                    "correct": outcome.get("correct"),
                    "calibration_error": outcome.get("calibration_error"),
                    "invalidation_actual": outcome.get("invalidation_actual"),
                    "invalidation_correct": outcome.get("invalidation_correct"),
                    "evidence_valid": bool(outcome.get("evidence_valid", False)),
                    "metadata": outcome.get("metadata") or {},
                }))],
            ).fetchone()
            if row is None:
                row = connection.execute("SELECT id FROM analysis.continuous_advisor_forecast_outcome WHERE claim_id = %s", [claim_id]).fetchone()
        return str(row["id"])

    def unresolved_claims(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT claim.id AS claim_id, claim.claim_key, claim.claim_kind,
                       claim.horizon, claim.statement, claim.direction, claim.probability,
                       claim.target, claim.evidence_refs, claim.claim,
                       packet.id AS packet_id, packet.symbol, packet.cutoff,
                       packet.packet, response.id AS response_id
                FROM analysis.continuous_advisor_forecast_claim claim
                JOIN analysis.continuous_advisor_response response ON response.id = claim.response_id
                JOIN analysis.continuous_advisor_packet packet ON packet.id = claim.packet_id
                LEFT JOIN analysis.continuous_advisor_forecast_outcome outcome ON outcome.claim_id = claim.id
                WHERE response.status = 'succeeded' AND outcome.id IS NULL
                ORDER BY packet.cutoff ASC, claim.created_at ASC, claim.id ASC
                LIMIT %s
                """,
                [max(1, min(2_000, int(limit)))],
            ).fetchall()
        return [_jsonable(dict(row)) for row in rows]

    def quote_at_or_after(
        self,
        symbol: str,
        observed_from: datetime,
        *,
        available_by: datetime,
        observed_to: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Read confirmed historical quote provenance at the replay cutoff."""

        normalized = canonical_symbol(symbol)
        with self.runtime.read(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                WITH confirmed_quote AS MATERIALIZED (
                    SELECT DISTINCT ON (fact.id) fact.*
                    FROM (
                        SELECT * FROM raw.quote
                        UNION ALL
                        SELECT * FROM raw.quote_history
                    ) fact
                    JOIN raw.quote_fact_availability availability
                      ON availability.fact_id = fact.id
                     AND availability.fact_available_at = fact.available_at
                    JOIN ingest.run price_run
                      ON price_run.id = availability.ingest_run_id
                     AND price_run.status IN ('succeeded', 'partial')
                     AND price_run.finished_at IS NOT NULL
                    JOIN catalog.instrument instrument ON instrument.id = fact.instrument_id
                    WHERE instrument.symbol = %s
                      AND fact.available_at <= %s
                      AND price_run.finished_at <= %s
                    ORDER BY fact.id, fact.available_at DESC
                )
                SELECT quote.price, quote.observed_at, quote.available_at
                FROM confirmed_quote quote
                WHERE quote.observed_at >= %s
                  AND (%s::timestamptz IS NULL OR quote.observed_at <= %s)
                  AND quote.available_at <= %s
                ORDER BY quote.observed_at ASC, quote.available_at ASC, quote.source_id, quote.id
                LIMIT 1
                """,
                [normalized, available_by, available_by, observed_from, observed_to, observed_to, available_by],
            ).fetchone()
        return _jsonable(dict(row)) if row else None

    def quote_crossing_at_or_after(
        self,
        symbol: str,
        observed_from: datetime,
        *,
        observed_to: datetime,
        available_by: datetime,
        below: bool,
        threshold: float,
    ) -> dict[str, Any] | None:
        """Find the first confirmed threshold crossing in the full replay window."""

        normalized = canonical_symbol(symbol)
        with self.runtime.read(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                WITH confirmed_quote AS MATERIALIZED (
                    SELECT DISTINCT ON (fact.id) fact.*
                    FROM (
                        SELECT * FROM raw.quote
                        UNION ALL
                        SELECT * FROM raw.quote_history
                    ) fact
                    JOIN raw.quote_fact_availability availability
                      ON availability.fact_id = fact.id
                     AND availability.fact_available_at = fact.available_at
                    JOIN ingest.run price_run
                      ON price_run.id = availability.ingest_run_id
                     AND price_run.status IN ('succeeded', 'partial')
                     AND price_run.finished_at IS NOT NULL
                    JOIN catalog.instrument instrument ON instrument.id = fact.instrument_id
                    WHERE instrument.symbol = %s
                      AND fact.available_at <= %s
                      AND price_run.finished_at <= %s
                    ORDER BY fact.id, fact.available_at DESC
                )
                SELECT quote.price, quote.observed_at, quote.available_at
                FROM confirmed_quote quote
                WHERE quote.observed_at >= %s
                  AND quote.observed_at <= %s
                  AND quote.available_at <= %s
                  AND CASE WHEN %s::boolean
                           THEN quote.price < %s
                           ELSE quote.price > %s
                      END
                ORDER BY quote.observed_at ASC, quote.available_at ASC, quote.source_id, quote.id
                LIMIT 1
                """,
                [
                    normalized, available_by, available_by, observed_from, observed_to,
                    available_by, below, threshold, threshold,
                ],
            ).fetchone()
        return _jsonable(dict(row)) if row else None

    def quote_at_or_before(self, symbol: str, observed_to: datetime, *, available_by: datetime) -> dict[str, Any] | None:
        """Read the latest quote known by the replay cutoff from stored provenance."""

        normalized = canonical_symbol(symbol)
        with self.runtime.read(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                SELECT quote.price, quote.observed_at, quote.available_at
                FROM catalog.instrument instrument
                JOIN LATERAL raw.confirmed_quote_at(%s, ARRAY[instrument.id]) quote ON true
                WHERE instrument.symbol = %s
                  AND quote.observed_at <= %s
                  AND quote.available_at <= %s
                ORDER BY quote.observed_at DESC, quote.available_at DESC, quote.source_id DESC, quote.id DESC
                LIMIT 1
                """,
                [available_by, normalized, observed_to, available_by],
            ).fetchone()
        return _jsonable(dict(row)) if row else None

    def record_cohort(self, cohort: Mapping[str, Any]) -> str:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            row = connection.execute(
                "SELECT analysis.write_continuous_advisor_cohort(%s) AS id",
                [Jsonb(_jsonable({
                    "cohort_key": cohort["cohort_key"],
                    "active_prompt_version": cohort["active_prompt_version"],
                    "candidate_prompt_version": cohort["candidate_prompt_version"],
                    "cutoff_start": cohort.get("cutoff_start"),
                    "cutoff_end": cohort.get("cutoff_end"),
                    "holdout_cutoff": cohort.get("holdout_cutoff"),
                    "matched_outcomes": int(cohort.get("matched_outcomes") or 0),
                    "scorecard": cohort.get("scorecard") or {},
                    "walk_forward_scorecard": cohort.get("walk_forward_scorecard") or {},
                }))],
            ).fetchone()
        return str(row["id"])

    def run_detail(self, task_id: str) -> dict[str, Any] | None:
        try:
            task_uuid = UUID(str(task_id))
        except (ValueError, TypeError, AttributeError):
            return None
        with self.runtime.read(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                SELECT task.id AS task_id, task.status AS task_status, task.provider, task.model,
                       task.prompt_version, task.evidence_fingerprint, task.request, task.result,
                       task.validation_status, task.validation_detail, task.created_at, task.updated_at,
                       task.latency_ms, task.input_tokens, task.output_tokens, task.cost_usd,
                       packet.id AS packet_id, packet.symbol, packet.cutoff, packet.slot_start,
                       packet.fingerprint, packet.source_refs, packet.blockers, packet.packet,
                       response.id AS response_id, response.status AS response_status,
                       response.response, response.validation, response.reasoning_effort,
                       response.started_at, response.finished_at
                FROM analysis.agent_task task
                JOIN analysis.continuous_advisor_packet packet ON packet.id::text = task.request->>'packet_id'
                LEFT JOIN analysis.continuous_advisor_response response ON response.task_id = task.id
                WHERE task.id = %s AND task.task_kind = 'continuous_advisor'
                """,
                [task_uuid],
            ).fetchone()
            if row is None:
                return None
            claims = connection.execute(
                """
                SELECT id AS claim_id, claim_key, claim_kind, horizon, statement, direction,
                       probability, target, evidence_refs, claim
                FROM analysis.continuous_advisor_forecast_claim
                WHERE response_id = %s ORDER BY created_at, claim_key, id
                """,
                [row["response_id"]],
            ).fetchall() if row["response_id"] else []
        item = dict(row)
        packet = {key: item.pop(key) for key in ("packet_id", "symbol", "cutoff", "slot_start", "fingerprint", "source_refs", "blockers", "packet")}
        response = {key: item.pop(key) for key in ("response_id", "response_status", "response", "validation", "reasoning_effort", "started_at", "finished_at")}
        return _jsonable({"run": item, "packet": packet, "response": response, "claims": [dict(claim) for claim in claims]})

    def record_promotion(self, decision: Mapping[str, Any]) -> str:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            row = connection.execute(
                "SELECT analysis.write_continuous_advisor_promotion(%s) AS id",
                [Jsonb(_jsonable({
                    "candidate_prompt_version": decision["candidate_prompt_version"],
                    "previous_active_prompt_version": decision.get("previous_active_prompt_version"),
                    "decision": decision["decision"],
                    "reason": str(decision.get("reason") or "")[:2_000],
                    "scorecard": decision.get("scorecard") or {},
                }))],
            ).fetchone()
        return str(row["id"])

    def prompt_status(self, default_version: str, *, scheduler: Mapping[str, Any] | None = None) -> dict[str, Any]:
        active = self.active_prompt_version(default_version)
        with self.runtime.read(JOB_PROFILE) as connection:
            challenger = connection.execute(
                """
                SELECT version, parent_version, template, approved_change_set, mutation_rationale, created_at
                FROM analysis.continuous_advisor_prompt_version candidate
                WHERE parent_version = %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM analysis.continuous_advisor_promotion_decision decision
                      WHERE decision.candidate_prompt_version = candidate.version
                  )
                ORDER BY created_at DESC, version DESC LIMIT 1
                """,
                [active],
            ).fetchone()
            cohort = connection.execute(
                """
                SELECT cohort_key, active_prompt_version, candidate_prompt_version, matched_outcomes,
                       scorecard, walk_forward_scorecard, created_at
                FROM analysis.continuous_advisor_evaluation_cohort
                ORDER BY created_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
            promotion = connection.execute(
                """
                SELECT candidate_prompt_version, previous_active_prompt_version, decision, reason, scorecard, created_at
                FROM analysis.continuous_advisor_promotion_decision
                ORDER BY created_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
            totals = connection.execute(
                """
                SELECT count(*) AS runs,
                       count(*) FILTER (WHERE response.status = 'succeeded') AS succeeded,
                       count(DISTINCT response.symbol) FILTER (WHERE response.status = 'succeeded') AS covered,
                       coalesce(sum(response.cost_usd), 0) AS cost
                FROM analysis.continuous_advisor_response response
                """
            ).fetchone()
        return _jsonable({
            "active_prompt_version": active,
            "challenger": dict(challenger) if challenger else None,
            "matched_cohort_scorecard": dict(cohort) if cohort else None,
            "promotion": dict(promotion) if promotion else None,
            "coverage": {
                "runs": int(totals["runs"] or 0),
                "succeeded": int(totals["succeeded"] or 0),
                "symbols": int(totals["covered"] or 0),
                "cost_usd": float(totals["cost"] or 0),
            },
            "scheduler": dict(scheduler or {}),
            "advisory_only": True,
            "promotion_can_change": "advisory_prompt_only",
            "execution_controls_unchanged": True,
        })


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value) if isinstance(value, UUID) else float(value)
    return value


__all__ = ["ContinuousAdvisorRepository"]
