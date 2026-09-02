"""PostgreSQL writes for the immutable Phase 1 research authority."""

from __future__ import annotations

from typing import Any, Mapping, Sequence
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


class ResearchRepository:
    """Record research lifecycle rows without introducing a second API owner."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def create_hypothesis(self, *, key: str, statement: str, mechanism_class: str, falsification: str, input_hash: str, metadata: Mapping[str, Any] | None = None) -> UUID:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            return connection.execute(
                """INSERT INTO analysis.hypothesis
                   (hypothesis_key, statement, mechanism_class, falsification, input_hash, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                [key, statement, mechanism_class, falsification, input_hash, Jsonb(dict(metadata or {}))],
            ).fetchone()[0]

    def create_experiment_family(self, *, hypothesis_id: UUID, key: str, name: str, input_hash: str, design: Mapping[str, Any] | None = None, controls: Mapping[str, Any] | None = None) -> UUID:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            return connection.execute(
                """INSERT INTO analysis.experiment_family
                   (hypothesis_id, family_key, name, input_hash, design, controls)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                [hypothesis_id, key, name, input_hash, Jsonb(dict(design or {})), Jsonb(dict(controls or {}))],
            ).fetchone()[0]

    def start_trial(self, *, family_id: UUID, key: str, input_cutoff: Any, code_version: str, input_hash: str, parameters: Mapping[str, Any] | None = None, available_at: Any | None = None) -> UUID:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            return connection.execute(
                """INSERT INTO analysis.research_trial
                   (experiment_family_id, trial_key, input_cutoff, code_version, input_hash, parameters, available_at)
                   VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, now())) RETURNING id""",
                [family_id, key, input_cutoff, code_version, input_hash, Jsonb(dict(parameters or {})), available_at],
            ).fetchone()[0]

    def finish_trial(self, trial_id: UUID, *, status: str, failure_reason: str | None = None, outcome: Mapping[str, Any] | None = None) -> None:
        if status not in {"succeeded", "failed", "rejected"}:
            raise ValueError("terminal trial status is required")
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """UPDATE analysis.research_trial
                   SET status = %s, failure_reason = %s, outcome = %s,
                       finished_at = now()
                   WHERE id = %s AND status = 'running'""",
                [status, failure_reason, Jsonb(dict(outcome or {})), trial_id],
            )

    def record_result(self, *, trial_id: UUID, kind: str, input_hash: str, metrics: Mapping[str, Any] | None = None, outcome: Mapping[str, Any] | None = None, observed_at: Any, available_at: Any) -> UUID:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            return connection.execute(
                """INSERT INTO analysis.trial_result
                   (research_trial_id, result_kind, input_hash, metrics, outcome, observed_at, available_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [trial_id, kind, input_hash, Jsonb(dict(metrics or {})), Jsonb(dict(outcome or {})), observed_at, available_at],
            ).fetchone()[0]

    def record_universe_observations(self, rows: Sequence[Mapping[str, Any]]) -> int:
        if len(rows) > 10_000:
            raise ValueError("universe observation batch exceeds bound")
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for row in rows:
                connection.execute(
                    """INSERT INTO analysis.universe_observation
                       (research_trial_id, instrument_id, cutoff, eligible, rank, candidate_score,
                        exclusion_reason, observed_at, available_at, input_hash, outcome)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [row["research_trial_id"], row["instrument_id"], row["cutoff"], row["eligible"], row.get("rank"), row.get("candidate_score"), row.get("exclusion_reason"), row["observed_at"], row["available_at"], row["input_hash"], Jsonb(dict(row.get("outcome") or {}))],
                )
        return len(rows)

    def create_dossier(self, *, strategy_revision_id: int, trial_id: UUID | None = None, sections: Mapping[str, Any] | None = None, policy: Mapping[str, Any] | None = None, artifact_id: str | None = None, artifact_hash: str | None = None) -> UUID:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            return connection.execute(
                """INSERT INTO analysis.validation_dossier
                   (strategy_revision_id, research_trial_id, sections, compiled_policy, artifact_id, artifact_hash)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                [strategy_revision_id, trial_id, Jsonb(dict(sections or {})), Jsonb(dict(policy or {})), artifact_id, artifact_hash],
            ).fetchone()[0]

    def record_gate(self, *, dossier_id: UUID, code: str, verdict: str, metrics: Mapping[str, Any] | None = None, evidence: Mapping[str, Any] | None = None, evaluated_at: Any, available_at: Any) -> UUID:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            return connection.execute(
                """INSERT INTO analysis.validation_gate_result
                   (dossier_id, gate_code, verdict, metrics, evidence, evaluated_at, available_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [dossier_id, code, verdict, Jsonb(dict(metrics or {})), Jsonb(dict(evidence or {})), evaluated_at, available_at],
            ).fetchone()[0]

    def seal_dossier(self, dossier_id: UUID) -> None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute("UPDATE analysis.validation_dossier SET status = 'sealed' WHERE id = %s", [dossier_id])
