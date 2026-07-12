"""Automatic strategy promotion and rollback behind deterministic evidence gates."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


class StrategyGovernanceRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def automatic_promote_eligible(self) -> int:
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
                  AND COALESCE(task.validation->>'status', '') <> 'promoted'
                ORDER BY task.created_at
                """
            ).fetchall()
            for proposal in proposals:
                evaluations = connection.execute(
                    """
                    SELECT evaluation_type, verdict, metrics
                    FROM analysis.strategy_evaluation
                    WHERE strategy_revision_id = %s
                    ORDER BY evaluated_at DESC
                    """,
                    [proposal["candidate_id"]],
                ).fetchall()
                latest = {str(row["evaluation_type"]): row for row in evaluations}
                if not _promotion_evidence_passes(latest):
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
                    "WHERE authority_group = %s AND status = 'active' FOR UPDATE",
                    [proposal["authority_group"]],
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
                  AND outcome.maturity_state IN ('mature', 'expired')
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


def _promotion_evidence_passes(evaluations: dict[str, Any]) -> bool:
    requirements = {
        "backtest": (100, 120),
        "forward_shadow_test": (30, 30),
        "canary": (20, 20),
    }
    for evaluation_type, (sample, span) in requirements.items():
        row = evaluations.get(evaluation_type)
        if row is None or str(row["verdict"]) != "pass":
            return False
        metrics = dict(row["metrics"] or {})
        proposed = dict(metrics.get("proposed") or metrics)
        baseline = dict(metrics.get("baseline") or {})
        if int(proposed.get("sample_size") or 0) < sample:
            return False
        if int(metrics.get("observation_span_days") or 0) < span:
            return False
        if float(proposed.get("lower_95_expectancy") or 0) <= 0:
            return False
        baseline_expectancy = float(baseline.get("net_expectancy") or 0)
        proposed_expectancy = float(proposed.get("net_expectancy") or 0)
        if proposed_expectancy < baseline_expectancy * 1.10:
            return False
        if float(proposed.get("precision_at_5") or 0) < float(baseline.get("precision_at_5") or 0) - 0.02:
            return False
        if float(proposed.get("max_drawdown") or 0) < float(baseline.get("max_drawdown") or 0) * 1.10:
            return False
        if float(proposed.get("calibration_error") or 0) > float(baseline.get("calibration_error") or 0) + 0.02:
            return False
        if float(proposed.get("max_ticker_contribution") or 1) > 0.20:
            return False
    return True
