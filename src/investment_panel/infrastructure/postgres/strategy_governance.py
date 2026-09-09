"""Automatic strategy promotion and rollback behind deterministic evidence gates."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.domain.decision import promotion_readiness
from investment_panel.infrastructure.postgres.strategy_parameters import merge_strategy_parameters, mutation_capability
from investment_panel.infrastructure.postgres.options_paper_ledger import PAPER_FILL_MULTIPLIERS_SQL


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
                              dossier.sealed_at, trial.input_cutoff,
                              count(gate.id) AS gate_count,
                              count(gate.id) FILTER (WHERE gate.verdict = 'pass') AS passing_gates,
                              count(gate.id) FILTER (WHERE gate.verdict = 'pass' AND gate.evidence ? 'trial_result_id') AS evidence_gates,
                              EXISTS (SELECT 1 FROM analysis.trial_result result
                                      WHERE result.research_trial_id = trial.id AND result.result_kind = 'validation'
                                        AND (%s::timestamptz IS NULL OR result.available_at <= %s::timestamptz)
                                        AND result.outcome->'checks'->'multiple_testing' ?& ARRAY['psr','dsr','pbo','data_snooping_probability','fdr_q_value']
                                        AND result.outcome->'checks'->'cost_capacity'->'multiples' ?& ARRAY['1x','2x','3x']) AS mandatory_metrics,
                              EXISTS (SELECT 1 FROM analysis.strategy_forecast forecast
                                      WHERE forecast.strategy_revision_id = %s
                                        AND forecast.artifact_hash = revision_artifact.artifact_hash
                                        AND forecast.forecast_distribution IS NOT NULL
                                        AND (%s::timestamptz IS NULL OR (forecast.available_at <= %s::timestamptz AND forecast.generated_at <= %s::timestamptz))) AS forecast_lineage
                       FROM analysis.validation_dossier dossier
                       LEFT JOIN analysis.research_trial trial ON trial.id = dossier.research_trial_id
                       LEFT JOIN analysis.validation_gate_result gate ON gate.dossier_id = dossier.id
                       JOIN analysis.strategy_revision revision_artifact ON revision_artifact.id = dossier.strategy_revision_id
                       WHERE dossier.strategy_revision_id = %s AND dossier.status = 'sealed'
                       GROUP BY dossier.id, trial.id, revision_artifact.artifact_hash""",
                    [cutoff, cutoff, strategy_revision_id, cutoff, cutoff, cutoff, strategy_revision_id],
                ).fetchone()
                checks = {
                    "validation_dossier_incomplete": dossier is None,
                    "research_trial_incomplete": dossier is None or dossier["trial_status"] != "succeeded",
                    "universe_manifest_incomplete": dossier is None or not dossier["universe_complete"],
                    "trial_manifest_incomplete": dossier is None or not dossier["family_complete"],
                    "five_gates_incomplete": dossier is None or dossier["gate_count"] != 5 or dossier["passing_gates"] != 5 or dossier["evidence_gates"] != 5,
                    "artifact_lineage_mismatch": dossier is None or dossier["artifact_id"] != linked["artifact_id"] or dossier["artifact_hash"] != linked["artifact_hash"],
                    "paper_only_required": dossier is None or dict(dossier["compiled_policy"] or {}).get("paper_only") is not True,
                    "validation_metrics_incomplete": dossier is None or not dossier["mandatory_metrics"],
                    "forecast_lineage_incomplete": dossier is None or not dossier["forecast_lineage"],
                    "seal_not_available_at_cutoff": dossier is None or cutoff is not None and dossier["sealed_at"] > cutoff,
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
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["strategy:options-radar-core"],
            )
            proposals = connection.execute(
                """
                SELECT task.id, task.result, candidate.id AS candidate_id,
                       candidate.supersedes_id, candidate.parameters,
                       candidate.authority_group, parent.parameters AS parent_parameters
                FROM analysis.agent_task task
                JOIN analysis.strategy_revision candidate
                  ON candidate.id = (task.result->>'candidate_revision_id')::bigint
                JOIN analysis.strategy_revision parent ON parent.id = candidate.supersedes_id
                WHERE task.task_kind = 'strategy_mutation_proposal'
                  AND task.status = 'completed'
                  AND candidate.status IN ('candidate', 'testing', 'approved')
                  AND candidate.authority_group = 'options-radar-core'
                  AND COALESCE(task.validation->>'status', '') <> 'promoted'
                ORDER BY task.created_at
                FOR UPDATE OF candidate, task
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
                try:
                    expected_parameters = merge_strategy_parameters(proposal["parent_parameters"], changes)
                    capability = mutation_capability(proposal["parent_parameters"], changes)
                except (TypeError, ValueError, OverflowError):
                    continue
                if capability["blocking_verdict"] or expected_parameters != proposal["parameters"]:
                    continue
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
                    "UPDATE app.publication SET status = 'superseded', superseded_at = COALESCE(superseded_at, now()) "
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
        """Restore the parent after 20 independent negative trailing outcomes."""
        from investment_panel.infrastructure.postgres.strategy_learning import (
            OBSERVATION_QUERY, OUTCOME_QUERY, PAPER_EPISODE_ORDERS_SQL,
            has_multiple_paper_orders, observation_lineage_matches, paper_realized_return,
        )

        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["strategy:options-radar-core"],
            )
            active = connection.execute(
                """
                SELECT id, supersedes_id, promoted_at FROM analysis.strategy_revision
                WHERE authority_group = 'options-radar-core' AND status = 'active'
                FOR UPDATE
                """
            ).fetchone()
            if active is None or active["supersedes_id"] is None or active["promoted_at"] is None:
                return 0
            parent = connection.execute(
                """SELECT id FROM analysis.strategy_revision
                   WHERE id = %s AND authority_group = 'options-radar-core'
                     AND status = 'superseded' FOR UPDATE""",
                [active["supersedes_id"]],
            ).fetchone()
            if parent is None:
                return 0
            trailing = connection.execute(
                f"""
                WITH independent_outcomes AS (
                    SELECT DISTINCT ON (decision.lane, decision.episode_key)
                           outcome.current_return, outcome.observed_through,
                           decision.as_of, decision.id::text AS decision_id,
                           decision.lane, decision.episode_key,
                           paper_episode.paper_order_count, paper_episode.first_paper_at, paper_episode.last_paper_at
                    FROM analysis.option_outcome outcome
                    JOIN analysis.decision decision ON decision.id = outcome.decision_id
                    LEFT JOIN LATERAL ({PAPER_EPISODE_ORDERS_SQL}) paper_episode ON TRUE
                    WHERE decision.strategy_revision_id = %s
                      AND decision.as_of >= %s
                      AND outcome.current_return > '-Infinity'::double precision
                      AND outcome.current_return < 'Infinity'::double precision
                      AND outcome.promotion_eligible IS TRUE
                      AND outcome.outcome_classification = 'captured'
                      AND outcome.maturity_state IN ('mature', 'expired')
                      AND outcome.observed_through <= clock_timestamp()
                      AND outcome.observed_through >= decision.as_of
                      AND outcome.updated_at <= clock_timestamp()
                      AND decision.sample_eligible IS TRUE
                      AND outcome.sample_eligible IS TRUE
                      AND decision.quarantine_reason IS NULL
                      AND outcome.quarantine_reason IS NULL
                      AND decision.lane = outcome.lane
                      AND NULLIF(decision.episode_key, '') = outcome.episode_key
                      AND decision.calibration_cohort LIKE 'option-scorecard-truth-v1:%%'
                      AND outcome.calibration_cohort = decision.calibration_cohort
                    ORDER BY decision.lane, decision.episode_key, decision.as_of, decision.id
                )
                SELECT * FROM independent_outcomes
                ORDER BY observed_through DESC, as_of DESC, decision_id DESC
                """,
                [active["id"], active["promoted_at"]],
            ).fetchall()
            observations = connection.execute(
                OBSERVATION_QUERY, [[active["id"]], active["promoted_at"]],
            ).fetchall()
            closed = {
                row["decision_id"] for row in observations
                if row["shadow_status"] == "closed"
                and observation_lineage_matches(row, revision_id=active["id"])
            }
            measured = connection.execute(
                f"SELECT * FROM ({OUTCOME_QUERY}) measured WHERE as_of >= %s",
                [active["id"], active["id"], active["promoted_at"]],
            ).fetchall()
            trailing = [dict(row) for row in trailing]
            verified_paper = []
            for row in measured:
                realized = paper_realized_return(row)
                if realized is not None:
                    verified_paper.append({**row, "current_return": realized,
                                           "observed_through": row["exit_at"], "paper_verified": True})
            verified_keys = {(row["lane"], row["episode_key"]) for row in verified_paper}
            ambiguous = {}
            for row in [*trailing, *observations, *measured]:
                key = (row["lane"], row["episode_key"])
                if (has_multiple_paper_orders(row)
                    or (row.get("paper_order_count") != 0 and key not in verified_keys)):
                    # A later attempt can share an already-closed shadow's
                    # episode. Keep its actual clock in the trailing window.
                    through = max(value for value in (
                        row.get("observed_through"), row.get("exit_at"), row.get("first_paper_at"),
                        row.get("last_paper_at"), row["as_of"],
                    ) if value is not None)
                    if key not in ambiguous or through > ambiguous[key]["observed_through"]:
                        ambiguous[key] = {**row, "current_return": None, "observed_through": through}
            # Keep unresolved exposure in the trailing window. Do not replace
            # it with a selected paper winner, a shadow, or an older episode.
            trailing = [row for row in trailing if (row["lane"], row["episode_key"]) not in ambiguous]
            trailing.extend(row for row in verified_paper if (row["lane"], row["episode_key"]) not in ambiguous)
            for row in measured:
                if (row.get("paper_order_count") == 0 and row["decision_id"] in closed
                    and row["current_return"] is not None
                    and (row["lane"], row["episode_key"]) not in ambiguous):
                    trailing.append(dict(row))
            trailing.extend(ambiguous.values())
            # One economic episode has one return. Prefer its verified journal
            # over shadow marks, then the earliest decision, irrespective of P&L.
            independent = {}
            for row in sorted(trailing, key=lambda item: (
                item.get("paper_verified") is not True, item["as_of"], item["decision_id"],
            )):
                independent.setdefault((row["lane"], row["episode_key"]), row)
            trailing = sorted(independent.values(), key=lambda item: (
                item["observed_through"], item["as_of"], item["decision_id"],
            ), reverse=True)[:20]
            if (len(trailing) < 20 or any(row["current_return"] is None for row in trailing)
                or sum(float(row["current_return"]) for row in trailing) / len(trailing) >= 0):
                return 0
            connection.execute(
                "UPDATE analysis.strategy_revision SET status = 'rolled_back' WHERE id = %s",
                [active["id"]],
            )
            connection.execute(
                "UPDATE analysis.strategy_revision SET status = 'active', promoted_at = now() WHERE id = %s",
                [active["supersedes_id"]],
            )
            connection.execute(
                "UPDATE app.publication SET status = 'superseded', superseded_at = COALESCE(superseded_at, now()) "
                "WHERE scope = 'options-radar' AND status = 'published'"
            )
            connection.execute("DELETE FROM app.current_publication_item WHERE scope = 'options-radar'")
            connection.execute(
                """
                INSERT INTO app.alert
                    (alert_type, severity, title, detail)
                VALUES ('strategy_rollback', 'high', 'Options strategy rolled back',
                        'Negative trailing expectancy restored the prior policy. Fresh analysis is required before new paper entries.')
                """
            )
            return 1


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
        clocks = [row.get("evaluated_at"), row.get("available_at")]
        if any(not isinstance(value, datetime) or value.utcoffset() is None for value in clocks):
            row["evidence"] = {}
            continue
        if not paper_provenance_is_database_backed(connection, strategy_revision_id, paper, cutoff=min(clocks)):
            row["evidence"] = {}


def paper_provenance_is_database_backed(
    connection: Any, strategy_revision_id: int, paper: Any, *, cutoff: datetime | None = None,
) -> bool:
    from investment_panel.infrastructure.postgres.strategy_learning import PAPER_EPISODE_ORDERS_SQL, paper_execution_complete

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
        or type(sample_size) is not int
        or sample_size <= 0
        or len(paper_ids) != sample_size
        or len(decision_ids) != sample_size
        or len(set(paper_ids)) != sample_size
        or len(set(decision_ids)) != sample_size
    ):
        return False
    episode_orders = PAPER_EPISODE_ORDERS_SQL.replace("now()", "COALESCE(%s::timestamptz, clock_timestamp())")
    try:
        matched = connection.execute(
            f"""
            SELECT paper.id::text AS paper_order_id, paper.paper_only,
                   paper.status AS paper_status, paper.filled_at, paper.exit_at,
                   paper.filled_quantity, paper.exited_quantity, paper.fees,
                   paper.entry_slippage, paper.exit_slippage, paper.contract_multiplier,
                   decision.id::text AS decision_id, decision.as_of, decision.lane, decision.episode_key,
                   fills.entry_price AS actual_fill_price, fills.exit_price,
                   fills.entry_quantity, fills.exit_quantity, fills.fill_multipliers_verified, paper_episode.paper_order_count
            FROM app.paper_order paper
            JOIN analysis.decision decision ON decision.id = paper.decision_id
            LEFT JOIN LATERAL ({episode_orders}) paper_episode ON TRUE
            LEFT JOIN LATERAL (
                SELECT sum(quantity * price) FILTER (WHERE action = 'paper_entry')
                           / nullif(sum(quantity) FILTER (WHERE action = 'paper_entry'), 0) AS entry_price,
                       sum(quantity * price) FILTER (WHERE action = 'paper_exit' OR action LIKE 'paper_exit:%%')
                           / nullif(sum(quantity) FILTER (WHERE action = 'paper_exit' OR action LIKE 'paper_exit:%%'), 0) AS exit_price,
                       sum(quantity) FILTER (WHERE action = 'paper_entry') AS entry_quantity,
                       sum(quantity) FILTER (WHERE action = 'paper_exit' OR action LIKE 'paper_exit:%%') AS exit_quantity,
                       {PAPER_FILL_MULTIPLIERS_SQL} AS fill_multipliers_verified
                FROM app.trade_journal
                WHERE details->>'paper_order_id' = paper.id::text
                  AND decision_id = decision.id AND quantity > 0 AND price IS NOT NULL
                  AND rationale = 'deterministic_options_paper_execution'
                  AND created_at <= COALESCE(%s::timestamptz, clock_timestamp())
            ) fills ON TRUE
            WHERE decision.strategy_revision_id = %s
              AND decision.id = ANY(%s::uuid[])
              AND paper.id = ANY(%s::uuid[])
              AND paper.paper_only IS TRUE
              AND paper.status IN ('exited', 'closed')
              AND paper.exit_at <= COALESCE(%s::timestamptz, clock_timestamp())
              AND decision.lane = paper.lane
              AND nullif(btrim(decision.episode_key), '') IS NOT NULL
            """,
            [cutoff, cutoff, strategy_revision_id, decision_ids, paper_ids, cutoff],
        ).fetchall()
    except Exception:
        return False
    return (
        len(matched) == sample_size
        and len({row["decision_id"] for row in matched}) == sample_size
        and len({(row["lane"], row["episode_key"]) for row in matched}) == sample_size
        and all(paper_execution_complete(row) for row in matched)
    )
