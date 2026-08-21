"""Advisory-only, paired DeepSeek/Luna experiment ownership."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from statistics import mean
from typing import Any, Mapping
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from investment_panel.core.decision import is_us_market_day
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


EXPERIMENT_KEY = "deepseek-v4-flash-vs-luna-20d"
MAX_PAIRS_PER_TRADING_DAY = 12
EXPERIMENT_ROLES = frozenset({"thesis_survival", "red_team", "postmortem", "mutation_draft"})
MARKET_TZ = ZoneInfo("America/New_York")


class AgentExperimentRepository:
    """Persist frozen-evidence paired tasks without changing any trade control."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def ensure_current(self) -> dict[str, Any]:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            row = connection.execute(
                "SELECT * FROM analysis.agent_experiment WHERE experiment_key = %s FOR UPDATE",
                [EXPERIMENT_KEY],
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    INSERT INTO analysis.agent_experiment
                        (experiment_key, champion_provider, champion_model,
                         challenger_provider, challenger_model, max_pairs_per_trading_day,
                         advisory_only, parameters)
                    VALUES (%s, 'codex', 'gpt-5.6-luna', 'deepseek', 'deepseek-v4-flash', %s, true, %s)
                    RETURNING *
                    """,
                    [
                        EXPERIMENT_KEY,
                        MAX_PAIRS_PER_TRADING_DAY,
                        Jsonb({
                            "trading_days": 20,
                            "reasoning_effort": "high",
                            "roles": sorted(EXPERIMENT_ROLES),
                            "routing_changes_allowed": False,
                            "execution_authority": "advisory_only",
                            "provider_rate_source": "typed_provider_registry",
                        }),
                    ],
                ).fetchone()
        return dict(row)

    def queue_pair(
        self,
        *,
        role: str,
        evidence_packet: Mapping[str, Any],
        prompt_version: str,
        schema_version: str,
        baseline_version: str,
        decision_id: UUID | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_role = str(role).strip().lower()
        if normalized_role not in EXPERIMENT_ROLES:
            raise ValueError("experiment role is invalid")
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("experiment time must be timezone-aware")
        if not is_us_market_day(reference.astimezone(MARKET_TZ).date()):
            raise ValueError("paired experiments run only on US trading days")
        experiment = self.ensure_current()
        if str(experiment["status"]) != "active":
            raise ValueError("agent experiment is not active")
        frozen_packet = _frozen_packet(evidence_packet)
        fingerprint = _fingerprint(frozen_packet)
        task_kind = "option_postmortem" if normalized_role in {"postmortem", "mutation_draft"} else "option_thesis"
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"agent-experiment:{experiment['id']}:{reference.astimezone(UTC).date().isoformat()}"],
            )
            prior = connection.execute(
                """
                SELECT champion.id AS champion_task_id, challenger.id AS challenger_task_id
                FROM analysis.agent_task champion
                JOIN analysis.agent_task challenger ON challenger.id = champion.paired_task_id
                WHERE champion.experiment_id = %s AND champion.arm = 'champion'
                  AND champion.evidence_fingerprint = %s
                  AND (champion.created_at AT TIME ZONE 'America/New_York')::date
                      = (%s AT TIME ZONE 'America/New_York')::date
                ORDER BY champion.created_at DESC
                LIMIT 1
                """,
                [experiment["id"], fingerprint, reference],
            ).fetchone()
            if prior is not None:
                return {
                    "experiment_id": str(experiment["id"]),
                    "champion_task_id": str(prior["champion_task_id"]),
                    "challenger_task_id": str(prior["challenger_task_id"]),
                    "evidence_fingerprint": fingerprint,
                    "authority": "advisory_only",
                    "idempotent_replay": True,
                }
            used = connection.execute(
                """
                SELECT count(*) AS count
                FROM analysis.agent_task
                WHERE experiment_id = %s
                  AND arm = 'challenger'
                  AND (created_at AT TIME ZONE 'America/New_York')::date
                      = (%s AT TIME ZONE 'America/New_York')::date
                """,
                [experiment["id"], reference],
            ).fetchone()
            if int(used["count"] or 0) >= int(experiment["max_pairs_per_trading_day"]):
                raise ValueError("daily paired-task cap reached")
            request = {
                "experiment_role": normalized_role,
                "evidence_packet": frozen_packet,
                "evidence_fingerprint": fingerprint,
                "authority": "advisory_only",
                "trade_control_mutation_allowed": False,
                "reasoning_effort": "high",
            }
            champion = connection.execute(
                """
                INSERT INTO analysis.agent_task
                    (decision_id, task_kind, status, request, experiment_id, arm, provider, model,
                     evidence_fingerprint, prompt_version, schema_version, baseline_version, created_at, updated_at)
                VALUES (%s, %s, 'queued', %s, %s, 'champion', %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                [
                    decision_id, task_kind, Jsonb(request), experiment["id"],
                    experiment["champion_provider"], experiment["champion_model"], fingerprint,
                    prompt_version, schema_version, baseline_version, reference, reference,
                ],
            ).fetchone()
            challenger = connection.execute(
                """
                INSERT INTO analysis.agent_task
                    (decision_id, task_kind, status, request, experiment_id, arm, provider, model,
                     evidence_fingerprint, prompt_version, schema_version, baseline_version, created_at, updated_at)
                VALUES (%s, %s, 'queued', %s, %s, 'challenger', %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                [
                    decision_id, task_kind, Jsonb(request), experiment["id"],
                    experiment["challenger_provider"], experiment["challenger_model"], fingerprint,
                    prompt_version, schema_version, baseline_version, reference, reference,
                ],
            ).fetchone()
            connection.execute(
                "UPDATE analysis.agent_task SET paired_task_id = %s WHERE id = %s",
                [challenger["id"], champion["id"]],
            )
            connection.execute(
                "UPDATE analysis.agent_task SET paired_task_id = %s WHERE id = %s",
                [champion["id"], challenger["id"]],
            )
        return {
            "experiment_id": str(experiment["id"]),
            "champion_task_id": str(champion["id"]),
            "challenger_task_id": str(challenger["id"]),
            "evidence_fingerprint": fingerprint,
            "authority": "advisory_only",
        }

    def claim_pair(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        """Atomically claim one frozen champion/challenger pair for execution.

        Normal option-agent workers deliberately exclude these tasks.  A pair
        has a dedicated run per arm, so provider telemetry cannot be mistaken
        for the configured production agent.
        """

        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("experiment time must be timezone-aware")
        experiment = self.ensure_current()
        if str(experiment["status"]) != "active":
            return None
        with self.runtime.transaction(JOB_PROFILE) as connection:
            champion = connection.execute(
                """
                SELECT *
                FROM analysis.agent_task
                WHERE experiment_id = %s AND arm = 'champion' AND status = 'queued'
                ORDER BY created_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                [experiment["id"]],
            ).fetchone()
            if champion is None:
                return None
            challenger = connection.execute(
                """
                SELECT * FROM analysis.agent_task
                WHERE id = %s AND experiment_id = %s AND arm = 'challenger'
                  AND status = 'queued'
                FOR UPDATE
                """,
                [champion["paired_task_id"], experiment["id"]],
            ).fetchone()
            if challenger is None:
                return None
            tasks: list[dict[str, Any]] = []
            for task in (dict(champion), dict(challenger)):
                run = connection.execute(
                    """
                    INSERT INTO analysis.agent_run
                        (provider, model, trigger, started_at, status, summary,
                         experiment_id, arm, evidence_fingerprint, prompt_version,
                         schema_version, baseline_version)
                    VALUES (%s, %s, 'experiment', %s, 'running', %s,
                            %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    [
                        task["provider"], task["model"], reference,
                        Jsonb({"workflow": "paired_agent_experiment", "advisory_only": True}),
                        experiment["id"], task["arm"], task["evidence_fingerprint"],
                        task["prompt_version"], task["schema_version"], task["baseline_version"],
                    ],
                ).fetchone()
                connection.execute(
                    """
                    UPDATE analysis.agent_task
                    SET agent_run_id = %s, status = 'running', updated_at = %s
                    WHERE id = %s
                    """,
                    [run["id"], reference, task["id"]],
                )
                tasks.append({**task, "agent_run_id": str(run["id"])})
        return {"experiment_id": str(experiment["id"]), "tasks": tasks}

    def record_result(
        self,
        *,
        task_id: UUID,
        status: str,
        validation_status: str,
        validation_detail: Mapping[str, Any],
        latency_ms: int | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_usd: float | None,
        result: Mapping[str, Any] | None = None,
        provider_telemetry: Mapping[str, Any] | None = None,
    ) -> None:
        if status not in {"completed", "failed"}:
            raise ValueError("experiment task status is invalid")
        with self.runtime.transaction(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                UPDATE analysis.agent_task
                SET status = %s, result = %s, validation = %s,
                    validation_status = %s, validation_detail = %s,
                    latency_ms = %s, input_tokens = %s, output_tokens = %s, cost_usd = %s,
                    provider = COALESCE(%s, provider), model = COALESCE(%s, model),
                    updated_at = now()
                WHERE id = %s AND experiment_id IS NOT NULL
                RETURNING agent_run_id
                """,
                [
                    status, Jsonb(dict(result)) if result is not None else None,
                    Jsonb({"status": validation_status, **dict(validation_detail)}),
                    validation_status, Jsonb(dict(validation_detail)), latency_ms,
                    input_tokens, output_tokens, cost_usd,
                    (provider_telemetry or {}).get("provider"),
                    (provider_telemetry or {}).get("model"), task_id,
                ],
            ).fetchone()
            if row is None:
                raise ValueError("experiment task not found")
            if row["agent_run_id"] is not None:
                connection.execute(
                    """
                    UPDATE analysis.agent_run
                    SET status = %s, finished_at = now(), input_tokens = %s,
                        output_tokens = %s, cost_usd = %s, validation_status = %s,
                        validation_detail = %s,
                        latency_ms = %s,
                        provider = COALESCE(%s, provider), model = COALESCE(%s, model),
                        summary = summary || %s
                    WHERE id = %s
                    """,
                    [
                        "succeeded" if status == "completed" else "failed",
                        input_tokens, output_tokens, cost_usd, validation_status,
                        Jsonb(dict(validation_detail)), latency_ms,
                        (provider_telemetry or {}).get("provider"),
                        (provider_telemetry or {}).get("model"),
                        Jsonb({
                            "completed": status == "completed", "advisory_only": True,
                            "provider_telemetry": dict(provider_telemetry or {}),
                        }),
                        row["agent_run_id"],
                    ],
                )

    def current(self) -> dict[str, Any] | None:
        with self.runtime.read(JOB_PROFILE) as connection:
            experiment = connection.execute(
                """
                SELECT * FROM analysis.agent_experiment
                WHERE experiment_key = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                [EXPERIMENT_KEY],
            ).fetchone()
            if experiment is None:
                return None
            tasks = [dict(row) for row in connection.execute(
                """
                SELECT id::text AS id, paired_task_id::text AS paired_task_id,
                       arm, status, validation_status, validation_detail, latency_ms,
                       cost_usd, provider, model, input_tokens, output_tokens, created_at
                FROM analysis.agent_task
                WHERE experiment_id = %s
                ORDER BY created_at
                """,
                [experiment["id"]],
            ).fetchall()]
        return _experiment_summary(dict(experiment), tasks)

    def seal_report(self) -> dict[str, Any]:
        summary = self.current()
        if summary is None:
            raise ValueError("agent experiment has not started")
        if not bool(summary.get("deepseek_default_eligible")):
            failed_gates = [name for name, passed in dict(summary.get("gates") or {}).items() if not passed]
            raise ValueError(
                "agent experiment cannot be sealed or used for provider routing until every gate passes: "
                + ", ".join(failed_gates)
            )
        with self.runtime.transaction(JOB_PROFILE) as connection:
            row = connection.execute(
                "SELECT status, immutable_report FROM analysis.agent_experiment WHERE id = %s FOR UPDATE",
                [summary["experiment_id"]],
            ).fetchone()
            if row is None:
                raise ValueError("agent experiment not found")
            if row["immutable_report"] is not None:
                return dict(row["immutable_report"])
            connection.execute(
                """
                UPDATE analysis.agent_experiment
                SET status = 'completed', completed_at = now(), immutable_report = %s,
                    report_sealed_at = now()
                WHERE id = %s
                """,
                [Jsonb(summary), summary["experiment_id"]],
            )
        return summary


def _frozen_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Round-trip JSON to detach the task from a mutable caller object."""

    encoded = json.dumps(dict(packet), sort_keys=True, separators=(",", ":"), default=str)
    value = json.loads(encoded)
    if not isinstance(value, dict):  # defensive; Mapping always encodes to an object
        raise ValueError("evidence packet must be an object")
    return value


def _fingerprint(packet: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(packet), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _experiment_summary(experiment: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    arms = {arm: [row for row in tasks if row.get("arm") == arm] for arm in ("champion", "challenger")}
    arm_summary = {arm: _arm_summary(rows) for arm, rows in arms.items()}
    status_by_task_id = {str(row["id"]): str(row["status"]) for row in tasks}
    completed_pair_days = {
        row["created_at"].astimezone(MARKET_TZ).date()
        for row in arms["challenger"]
        if str(row["status"]) == "completed"
        and status_by_task_id.get(str(row.get("paired_task_id") or "")) == "completed"
        and is_us_market_day(row["created_at"].astimezone(MARKET_TZ).date())
    }
    daily_pairs = len(completed_pair_days)
    deepseek = arm_summary["challenger"]
    luna = arm_summary["champion"]
    gates = {
        "twenty_trading_days": daily_pairs >= 20,
        "all_scheduled_tasks_resolved": deepseek["all_scheduled_tasks_resolved"] and luna["all_scheduled_tasks_resolved"],
        "deepseek_failure_under_5pct": deepseek["failure_rate"] is not None and deepseek["failure_rate"] < 0.05,
        "deepseek_schema_validation_100pct": deepseek["schema_validation_rate"] == 1.0,
        "deepseek_evidence_validation_95pct": deepseek["evidence_validation_rate"] is not None and deepseek["evidence_validation_rate"] >= 0.95,
        "useful_advice_within_5pp_of_luna": (
            deepseek["useful_advice_rate"] is not None
            and luna["useful_advice_rate"] is not None
            and deepseek["useful_advice_rate"] >= luna["useful_advice_rate"] - 0.05
        ),
        "deepseek_p95_under_90_seconds": deepseek["p95_latency_ms"] is not None and deepseek["p95_latency_ms"] < 90_000,
        "deepseek_lower_cost": (
            deepseek["mean_cost_usd"] is not None
            and luna["mean_cost_usd"] is not None
            and deepseek["mean_cost_usd"] < luna["mean_cost_usd"]
        ),
        "provider_rate_costs_recorded": deepseek["provider_rate_costs_recorded"] and luna["provider_rate_costs_recorded"],
        "observed_token_usage_recorded": deepseek["observed_token_usage_recorded"] and luna["observed_token_usage_recorded"],
        "confidence_intervals_available": deepseek["confidence_intervals_available"] and luna["confidence_intervals_available"],
    }
    return {
        "experiment_id": str(experiment["id"]),
        "experiment_key": experiment["experiment_key"],
        "status": experiment["status"],
        "started_at": experiment["started_at"].isoformat(),
        "completed_at": experiment["completed_at"].isoformat() if experiment.get("completed_at") else None,
        "advisory_only": True,
        "routing_changed": False,
        "progress": {
            "paired_trading_days": daily_pairs,
            "required_trading_days": 20,
            "paired_tasks": len(arms["challenger"]),
            "completed_pairs": sum(
                1
                for row in arms["challenger"]
                if str(row["status"]) == "completed"
                and status_by_task_id.get(str(row.get("paired_task_id") or "")) == "completed"
            ),
        },
        "arms": {
            "luna": {"provider": experiment["champion_provider"], "model": experiment["champion_model"], **luna},
            "deepseek": {"provider": experiment["challenger_provider"], "model": experiment["challenger_model"], **deepseek},
        },
        "gates": gates,
        "deepseek_default_eligible": all(gates.values()),
    }


def _arm_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in rows if row.get("status") == "completed"]
    terminal = [row for row in rows if row.get("status") in {"completed", "failed"}]
    scheduled = len(rows)
    schema_valid = [row for row in rows if row.get("validation_status") == "passed"]
    evidence_valid = [
        row for row in rows
        if bool((row.get("validation_detail") or {}).get("evidence_valid"))
    ]
    useful = [
        row for row in rows
        if bool((row.get("validation_detail") or {}).get("useful_advice"))
    ]
    latencies = sorted(int(row["latency_ms"]) for row in completed if row.get("latency_ms") is not None)
    costs = [float(row["cost_usd"]) for row in completed if row.get("cost_usd") is not None]
    provider_rate_rows = [
        row for row in rows
        if ((row.get("validation_detail") or {}).get("provider_telemetry") or {}).get("pricing", {}).get("pricing_status") == "provider_rate"
        and row.get("cost_usd") is not None
    ]
    observed_usage_rows = [
        row for row in provider_rate_rows
        if bool((((row.get("validation_detail") or {}).get("provider_telemetry") or {}).get("pricing") or {}).get("token_usage_observed"))
    ]
    failure_count = sum(row.get("status") == "failed" for row in rows)
    return {
        "tasks": scheduled,
        "completed": len(completed),
        "failed": failure_count,
        "unresolved": scheduled - len(terminal),
        "all_scheduled_tasks_resolved": scheduled > 0 and len(terminal) == scheduled,
        "failure_rate": (
            failure_count / scheduled if scheduled else None
        ),
        "schema_validation_rate": len(schema_valid) / scheduled if scheduled else None,
        "evidence_validation_rate": len(evidence_valid) / scheduled if scheduled else None,
        "useful_advice_rate": len(useful) / scheduled if scheduled else None,
        "p95_latency_ms": _percentile(latencies, 0.95),
        "mean_cost_usd": mean(costs) if costs else None,
        "provider_rate_costs_recorded": scheduled > 0 and len(provider_rate_rows) == scheduled,
        "observed_token_usage_recorded": scheduled > 0 and len(observed_usage_rows) == scheduled,
        "confidence_intervals": {
            "failure_rate": _wilson_interval(failure_count, scheduled),
            "schema_validation_rate": _wilson_interval(len(schema_valid), scheduled),
            "evidence_validation_rate": _wilson_interval(len(evidence_valid), scheduled),
            "useful_advice_rate": _wilson_interval(len(useful), scheduled),
        },
        "confidence_intervals_available": scheduled > 0,
    }


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    index = max(0, min(len(values) - 1, int((len(values) - 1) * percentile)))
    return values[index]


def _wilson_interval(successes: int, total: int, *, z: float = 1.96) -> dict[str, float] | None:
    """Two-sided 95% Wilson interval; no rate has meaning without its denominator."""

    if total <= 0:
        return None
    proportion = max(0.0, min(1.0, successes / total))
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * ((proportion * (1 - proportion) / total + z * z / (4 * total * total)) ** 0.5) / denominator
    return {"lower": round(max(0.0, center - margin), 6), "upper": round(min(1.0, center + margin), 6), "confidence": 0.95}


arm_summary = _arm_summary
