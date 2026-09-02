"""Automatic strategy promotion and rollback behind deterministic evidence gates."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.core.decision import promotion_readiness


class StrategyGovernanceRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def promotion_readiness(
        self, strategy_revision_id: int, *, cutoff: Any | None = None,
    ) -> dict[str, Any]:
        """Read one strategy's point-in-time Phase 7 governance evidence."""

        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT evaluation_type, verdict, metrics, evidence,
                       evaluated_at, available_at, period_start, period_end
                FROM analysis.strategy_evaluation
                WHERE strategy_revision_id = %s
                  AND (%s::timestamptz IS NULL OR evaluated_at <= %s::timestamptz)
                  AND (%s::timestamptz IS NULL OR available_at <= %s::timestamptz)
                ORDER BY evaluated_at DESC, id DESC
                """,
                [strategy_revision_id, cutoff, cutoff, cutoff, cutoff],
            ).fetchall()
            evaluations = [dict(row) for row in rows]
            _quarantine_unverified_paper_evaluations(connection, strategy_revision_id, evaluations)
            result = promotion_readiness(evaluations, now=cutoff)
            linked = connection.execute(
                """SELECT hypothesis_id, experiment_family_id, artifact_id, artifact_hash,
                          research_required
                   FROM analysis.strategy_revision WHERE id = %s""",
                [strategy_revision_id],
            ).fetchone()
            if linked and (linked["research_required"] or linked["hypothesis_id"] is not None or linked["experiment_family_id"] is not None):
                blockers = result.setdefault("blockers", [])
                dossier = connection.execute(
                    """SELECT dossier.id, dossier.status, dossier.artifact_id, dossier.artifact_hash,
                              dossier.compiled_policy, trial.id AS trial_id, trial.status AS trial_status,
                              analysis.research_trial_universe_complete(trial.id) AS universe_complete,
                              analysis.research_family_complete(trial.experiment_family_id) AS family_complete,
                              count(gate.id) AS gate_count,
                              count(gate.id) FILTER (WHERE gate.verdict = 'pass') AS passing_gates
                       FROM analysis.validation_dossier dossier
                       LEFT JOIN analysis.research_trial trial ON trial.id = dossier.research_trial_id
                       LEFT JOIN analysis.validation_gate_result gate ON gate.dossier_id = dossier.id
                       WHERE dossier.strategy_revision_id = %s AND dossier.status = 'sealed'
                       GROUP BY dossier.id, trial.id""",
                    [strategy_revision_id],
                ).fetchone()
                checks = {
                    "validation_dossier_incomplete": dossier is None,
                    "research_trial_incomplete": dossier is None or dossier["trial_status"] != "succeeded",
                    "universe_manifest_incomplete": dossier is None or not dossier["universe_complete"],
                    "trial_manifest_incomplete": dossier is None or not dossier["family_complete"],
                    "five_gates_incomplete": dossier is None or dossier["gate_count"] != 5 or dossier["passing_gates"] != 5,
                    "artifact_lineage_mismatch": dossier is None or dossier["artifact_id"] != linked["artifact_id"] or dossier["artifact_hash"] != linked["artifact_hash"],
                    "paper_only_required": dossier is None or dict(dossier["compiled_policy"] or {}).get("paper_only") is not True,
                }
                for blocker, failed in checks.items():
                    if failed and blocker not in blockers:
                        blockers.append(blocker)
                if blockers:
                    result["status"] = "unavailable"
                    result["promotion_eligible"] = False
            return result

    def automatic_promote_eligible(self, *, enabled: bool = True) -> int:
        if not enabled:
            return 0
        promoted = 0
        with self.runtime.transaction(JOB_PROFILE) as connection:
            proposals = connection.execute(
                """
                SELECT task.id, task.result, candidate.id AS candidate_id,
                       candidate.supersedes_id, candidate.parameters,
                       candidate.authority_group
                FROM analysis.agent_task task
                JOIN analysis.strategy_revision candidate
                  ON candidate.id = (task.result->>'candidate_revision_id')::bigint
                WHERE task.task_kind = 'strategy_mutation_proposal'
                  AND task.status = 'completed'
                  AND candidate.status IN ('candidate', 'testing', 'approved')
                  AND candidate.authority_group = 'options-radar-core'
                  AND COALESCE(task.validation->>'status', '') <> 'promoted'
                ORDER BY task.created_at
                """
            ).fetchall()
            for proposal in proposals:
                if proposal["authority_group"] != "options-radar-core":
                    continue
                evaluations = [dict(row) for row in connection.execute(
                    """
                    SELECT evaluation_type, verdict, metrics, evidence,
                           evaluated_at, available_at
                    FROM analysis.strategy_evaluation
                    WHERE strategy_revision_id = %s
                    ORDER BY evaluated_at DESC, id DESC
                    """,
                    [proposal["candidate_id"]],
                ).fetchall()]
                _quarantine_unverified_paper_evaluations(connection, proposal["candidate_id"], evaluations)
                latest: dict[str, Any] = {}
                for row in evaluations:
                    latest.setdefault(str(row["evaluation_type"]), row)
                if not _promotion_evidence_passes(list(latest.values())):
                    continue
                result = dict(proposal["result"] or {})
                changes = dict(result.get("proposed_parameter_changes") or {})
                if not changes or any(key not in _AUTOMATIC_PARAMETER_ALLOWLIST for key in changes):
                    continue
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ["strategy:options-radar-core"],
                )
                active = connection.execute(
                    "SELECT id FROM analysis.strategy_revision "
                    "WHERE authority_group = 'options-radar-core' AND status = 'active' FOR UPDATE",
                ).fetchall()
                if len(active) != 1 or active[0]["id"] != proposal["supersedes_id"]:
                    continue
                connection.execute(
                    "UPDATE analysis.strategy_revision SET status = 'superseded' WHERE id = %s",
                    [proposal["supersedes_id"]],
                )
                connection.execute(
                    "UPDATE analysis.strategy_revision SET status = 'active', promoted_at = now() WHERE id = %s",
                    [proposal["candidate_id"]],
                )
                connection.execute(
                    "UPDATE app.publication SET status = 'superseded' "
                    "WHERE scope = 'options-radar' AND status = 'published'"
                )
                connection.execute("DELETE FROM app.current_publication_item WHERE scope = 'options-radar'")
                connection.execute(
                    "UPDATE analysis.agent_task SET validation = %s, updated_at = now() WHERE id = %s",
                    [
                        Jsonb({
                            "status": "promoted",
                            "authority": "automatic_deterministic_governance",
                            "evidence_types": ["backtest", "forward_shadow_test", "canary"],
                        }),
                        proposal["id"],
                    ],
                )
                promoted += 1
        return promoted

    def rollback_regressing_active(self) -> int:
        """Restore the parent after 20 resolved negative trailing outcomes."""

        with self.runtime.transaction(JOB_PROFILE) as connection:
            active = connection.execute(
                """
                SELECT id, supersedes_id FROM analysis.strategy_revision
                WHERE authority_group = 'options-radar-core' AND status = 'active'
                FOR UPDATE
                """
            ).fetchone()
            if active is None or active["supersedes_id"] is None:
                return 0
            trailing = connection.execute(
                """
                SELECT outcome.current_return
                FROM analysis.option_outcome outcome
                JOIN analysis.decision decision ON decision.id = outcome.decision_id
                WHERE decision.strategy_revision_id = %s
                  AND outcome.current_return IS NOT NULL
                  AND outcome.promotion_eligible IS TRUE
                  AND outcome.outcome_classification = 'captured'
                  AND outcome.maturity_state IN ('mature', 'expired')
                  AND decision.sample_eligible IS TRUE
                  AND outcome.sample_eligible IS TRUE
                  AND decision.calibration_cohort LIKE 'option-scorecard-truth-v1:%%'
                  AND outcome.calibration_cohort LIKE 'option-scorecard-truth-v1:%%'
                ORDER BY outcome.updated_at DESC LIMIT 20
                """,
                [active["id"]],
            ).fetchall()
            if len(trailing) < 20 or sum(float(row["current_return"]) for row in trailing) / len(trailing) >= 0:
                return 0
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["strategy:options-radar-core"],
            )
            connection.execute(
                "UPDATE analysis.strategy_revision SET status = 'rolled_back' WHERE id = %s",
                [active["id"]],
            )
            connection.execute(
                "UPDATE analysis.strategy_revision SET status = 'active', promoted_at = now() WHERE id = %s",
                [active["supersedes_id"]],
            )
            connection.execute(
                "UPDATE app.publication SET status = 'superseded' "
                "WHERE scope = 'options-radar' AND status = 'published'"
            )
            connection.execute("DELETE FROM app.current_publication_item WHERE scope = 'options-radar'")
            restored = connection.execute(
                """
                SELECT publication.id
                FROM app.publication publication
                JOIN analysis.run run ON run.id = publication.analysis_run_id
                WHERE publication.scope = 'options-radar'
                  AND run.strategy_revision_id = %s
                ORDER BY publication.published_at DESC NULLS LAST LIMIT 1
                """,
                [active["supersedes_id"]],
            ).fetchone()
            if restored:
                connection.execute(
                    "UPDATE app.publication SET status = 'published', published_at = now(), "
                    "validation = validation || %s WHERE id = %s",
                    [Jsonb({"rollback_reason": "negative_trailing_expectancy"}), restored["id"]],
                )
                connection.execute(
                    """
                    INSERT INTO app.current_publication_item
                        (scope, publication_id, model_name, stable_key, rank, instrument_id, content_hash)
                    SELECT publication.scope, publication.id, item.model_name, item.stable_key,
                           item.rank, item.instrument_id, item.content_hash
                    FROM app.publication publication
                    JOIN app.publication_bundle_item item ON item.bundle_id = publication.bundle_id
                    WHERE publication.id = %s
                    """,
                    [restored["id"]],
                )
            connection.execute(
                """
                INSERT INTO app.alert
                    (alert_type, severity, title, detail)
                VALUES ('strategy_rollback', 'high', 'Options strategy rolled back',
                        'Negative trailing expectancy restored the prior champion revision.')
                """
            )
            return 1


_AUTOMATIC_PARAMETER_ALLOWLIST = {
    "min_open_interest", "min_volume", "min_dte", "max_dte",
    "max_spread_pct", "delta_min", "delta_max",
    "max_required_move_pct", "max_iv_percentile",
}


def _promotion_evidence_passes(evaluations: list[dict[str, Any]]) -> bool:
    return promotion_readiness(evaluations)["promotion_eligible"]


def _quarantine_unverified_paper_evaluations(
    connection: Any, strategy_revision_id: int, evaluations: list[dict[str, Any]],
) -> None:
    """Remove execution claims whose IDs do not resolve to immutable DB records."""
    for row in evaluations:
        if row.get("evaluation_type") != "execution_grade_paper":
            continue
        evidence = row.get("evidence")
        paper = evidence.get("paper_execution") if isinstance(evidence, dict) else None
        if not paper_provenance_is_database_backed(connection, strategy_revision_id, paper):
            row["evidence"] = {}


def paper_provenance_is_database_backed(
    connection: Any, strategy_revision_id: int, paper: Any,
) -> bool:
    if not isinstance(paper, dict):
        return False
    try:
        paper_ids = [UUID(value) for value in paper["paper_order_ids"]]
        decision_ids = [UUID(value) for value in paper["decision_ids"]]
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    sample_size = paper.get("sample_size")
    if (
        paper.get("strategy_revision_id") != strategy_revision_id
        or paper.get("database_verified") is not True
        or not isinstance(sample_size, int)
        or sample_size <= 0
        or len(paper_ids) != sample_size
        or len(decision_ids) != sample_size
        or len(set(paper_ids)) != sample_size
        or len(set(decision_ids)) != sample_size
    ):
        return False
    try:
        matched = connection.execute(
            """
            SELECT count(DISTINCT paper.id) AS paper_count,
                   count(DISTINCT decision.id) AS decision_count
            FROM app.paper_order paper
            JOIN analysis.decision decision ON decision.id = paper.decision_id
            WHERE decision.strategy_revision_id = %s
              AND decision.id = ANY(%s::uuid[])
              AND paper.id = ANY(%s::uuid[])
              AND paper.paper_only IS TRUE
              AND paper.status IN ('exited', 'closed')
            """,
            [strategy_revision_id, decision_ids, paper_ids],
        ).fetchone()
    except Exception:
        return False
    return matched is not None and int(matched["paper_count"]) == sample_size and int(matched["decision_count"]) == sample_size
