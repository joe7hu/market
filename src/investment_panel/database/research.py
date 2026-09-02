"""PostgreSQL writes for the immutable Phase 1 research authority."""

from __future__ import annotations

import hashlib
import json
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
            ).fetchone()["id"]

    def create_experiment_family(self, *, hypothesis_id: UUID, key: str, name: str, input_hash: str, design: Mapping[str, Any] | None = None, controls: Mapping[str, Any] | None = None) -> UUID:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            return connection.execute(
                """INSERT INTO analysis.experiment_family
                   (hypothesis_id, family_key, name, input_hash, design, controls)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                [hypothesis_id, key, name, input_hash, Jsonb(dict(design or {})), Jsonb(dict(controls or {}))],
            ).fetchone()["id"]

    def start_trial(self, *, family_id: UUID, key: str, input_cutoff: Any, code_version: str, input_hash: str, parameters: Mapping[str, Any] | None = None, available_at: Any | None = None) -> UUID:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            return connection.execute(
                """INSERT INTO analysis.research_trial
                   (experiment_family_id, trial_key, input_cutoff, code_version, input_hash, parameters, available_at)
                   VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, now())) RETURNING id""",
                [family_id, key, input_cutoff, code_version, input_hash, Jsonb(dict(parameters or {})), available_at],
            ).fetchone()["id"]

    def create_experiment_manifest(self, *, family_id: UUID, trial_keys: Sequence[str], available_at: Any | None = None) -> str:
        keys = sorted({str(key) for key in trial_keys})
        if not keys or len(keys) > 10_000:
            raise ValueError("experiment manifest must contain 1..10000 trial keys")
        manifest_hash = hashlib.sha256(json.dumps(keys, separators=(",", ":")).encode()).hexdigest()
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """INSERT INTO analysis.experiment_manifest
                   (experiment_family_id, expected_trial_count, expected_trial_keys, manifest_hash, available_at)
                   VALUES (%s, %s, %s, %s, COALESCE(%s, now()))""",
                [family_id, len(keys), Jsonb(keys), manifest_hash, available_at],
            )
        return manifest_hash

    def create_universe_manifest(self, *, trial_id: UUID, cutoff: Any, instrument_ids: Sequence[int], available_at: Any | None = None) -> str:
        members = sorted({str(int(member)) for member in instrument_ids})
        if len(members) > 10_000:
            raise ValueError("universe manifest must contain at most 10000 instruments")
        manifest_hash = hashlib.sha256(json.dumps(members, separators=(",", ":")).encode()).hexdigest()
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """INSERT INTO analysis.trial_universe_manifest
                   (research_trial_id, cutoff, expected_member_count, expected_members, manifest_hash, available_at)
                   VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()))""",
                [trial_id, cutoff, len(members), Jsonb(members), manifest_hash, available_at],
            )
        return manifest_hash

    def finish_trial(self, trial_id: UUID, *, status: str, failure_reason: str | None = None, outcome: Mapping[str, Any] | None = None) -> None:
        if status not in {"succeeded", "failed", "rejected"}:
            raise ValueError("terminal trial status is required")
        with self.runtime.transaction(JOB_PROFILE) as connection:
            state = connection.execute(
                """SELECT status, input_cutoff,
                          EXISTS (SELECT 1 FROM analysis.trial_result WHERE research_trial_id = %s) AS has_result,
                          analysis.research_trial_universe_complete(%s) AS universe_complete
                   FROM analysis.research_trial WHERE id = %s FOR UPDATE""",
                [trial_id, trial_id, trial_id],
            ).fetchone()
            if state is None:
                raise ValueError("research trial does not exist")
            if state["status"] != "running":
                raise ValueError("research trial is already terminal")
            if not state["has_result"] or not state["universe_complete"]:
                raise ValueError("terminal research trial requires a result and complete universe manifest")
            connection.execute(
                """UPDATE analysis.research_trial
                   SET status = %s, failure_reason = %s, outcome = %s,
                       finished_at = now()
                   WHERE id = %s AND status = 'running'""",
                [status, failure_reason, Jsonb(dict(outcome or {})), trial_id],
            )

    def record_result(self, *, trial_id: UUID, kind: str, input_hash: str, metrics: Mapping[str, Any] | None = None, outcome: Mapping[str, Any] | None = None, observed_at: Any, available_at: Any) -> UUID:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            trial = connection.execute("SELECT input_cutoff FROM analysis.research_trial WHERE id = %s", [trial_id]).fetchone()
            if trial is None:
                raise ValueError("research trial does not exist")
            return connection.execute(
                """INSERT INTO analysis.trial_result
                   (research_trial_id, result_kind, input_hash, metrics, outcome, observed_at, available_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [trial_id, kind, input_hash, Jsonb(dict(metrics or {})), Jsonb(dict(outcome or {})), observed_at, available_at],
            ).fetchone()["id"]

    def record_universe_observations(self, rows: Sequence[Mapping[str, Any]]) -> int:
        if len(rows) > 10_000:
            raise ValueError("universe observation batch exceeds bound")
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for row in rows:
                manifest = connection.execute(
                    "SELECT expected_members, cutoff FROM analysis.trial_universe_manifest WHERE research_trial_id = %s",
                    [row["research_trial_id"]],
                ).fetchone()
                if manifest is None or str(row["instrument_id"]) not in {str(item) for item in manifest["expected_members"]}:
                    raise ValueError("universe observation is not in the immutable full-denominator manifest")
                connection.execute(
                    """INSERT INTO analysis.universe_observation
                       (research_trial_id, instrument_id, cutoff, eligible, rank, candidate_score,
                        exclusion_reason, observed_at, available_at, input_hash, outcome)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [row["research_trial_id"], row["instrument_id"], row["cutoff"], row["eligible"], row.get("rank"), row.get("candidate_score"), row.get("exclusion_reason"), row["observed_at"], row["available_at"], row["input_hash"], Jsonb(dict(row.get("outcome") or {}))],
                )
        return len(rows)

    def record_validation(self, *, trial_id: UUID, dossier_id: UUID, report: Mapping[str, Any], result_kind: str = "validation", result_version: int = 1, input_hash: str, observed_at: Any, available_at: Any) -> UUID:
        """Persist one validation result and exactly the five gate outcomes."""
        if result_version < 1:
            raise ValueError("validation result_version must be positive")
        gates = dict(report.get("gates") or {})
        missing = [code for code in ("pit_integrity", "denominator_completeness", "oos_predictive_validity", "falsification_and_robustness", "economic_promotability") if code not in gates]
        if missing:
            raise ValueError(f"validation report is missing gates: {', '.join(missing)}")
        with self.runtime.transaction(JOB_PROFILE) as connection:
            result = connection.execute(
                """INSERT INTO analysis.trial_result
                   (research_trial_id, result_kind, result_version, input_hash, metrics, outcome, observed_at, available_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [trial_id, result_kind, result_version, input_hash, Jsonb(dict(report.get("checks") or {})), Jsonb(dict(report)), observed_at, available_at],
            ).fetchone()["id"]
            for code in ("pit_integrity", "denominator_completeness", "oos_predictive_validity", "falsification_and_robustness", "economic_promotability"):
                gate = gates[code]
                passed = bool(gate.get("passed")) if isinstance(gate, Mapping) else bool(gate)
                connection.execute(
                    """INSERT INTO analysis.validation_gate_result
                       (dossier_id, gate_code, verdict, metrics, evidence, evaluated_at, available_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    [dossier_id, code, "pass" if passed else "fail", Jsonb(dict(gate) if isinstance(gate, Mapping) else {"passed": passed}), Jsonb({"trial_result_id": str(result)}), observed_at, available_at],
                )
        return result

    def create_dossier(self, *, strategy_revision_id: int, trial_id: UUID | None = None, sections: Mapping[str, Any] | None = None, policy: Mapping[str, Any] | None = None, artifact_id: str | None = None, artifact_hash: str | None = None) -> UUID:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            return connection.execute(
                """INSERT INTO analysis.validation_dossier
                   (strategy_revision_id, research_trial_id, sections, compiled_policy, artifact_id, artifact_hash)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                [strategy_revision_id, trial_id, Jsonb(dict(sections or {})), Jsonb(dict(policy or {})), artifact_id, artifact_hash],
            ).fetchone()["id"]

    def record_gate(self, *, dossier_id: UUID, code: str, verdict: str, metrics: Mapping[str, Any] | None = None, evidence: Mapping[str, Any] | None = None, evaluated_at: Any, available_at: Any) -> UUID:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            return connection.execute(
                """INSERT INTO analysis.validation_gate_result
                   (dossier_id, gate_code, verdict, metrics, evidence, evaluated_at, available_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                [dossier_id, code, verdict, Jsonb(dict(metrics or {})), Jsonb(dict(evidence or {})), evaluated_at, available_at],
            ).fetchone()["id"]

    def seal_dossier(self, dossier_id: UUID) -> None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute("UPDATE analysis.validation_dossier SET status = 'sealed' WHERE id = %s", [dossier_id])
