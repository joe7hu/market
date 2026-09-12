"""Bounded read models for the Research trading and learning workbench."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
from typing import Any
from uuid import UUID

from investment_panel.infrastructure.postgres.continuous_advisor import (
    ContinuousAdvisorRepository,
)
from investment_panel.infrastructure.postgres.runtime import API_PROFILE, DatabaseRuntime


MAX_ROWS = 200
CALCULATION_VERSION = "research-workbench.v1"


class ResearchWorkbenchRepository:
    """Compose read-only research records without creating a second authority."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def strategy_revisions(
        self,
        *,
        limit: int = MAX_ROWS,
        cursor: tuple[int, datetime, int] | None = None,
    ) -> dict[str, Any]:
        safe_limit = _limit(limit)
        filters = ["revision.created_at <= now()"]
        params: list[Any] = []
        if cursor:
            filters.append(
                "((revision.status = 'active')::integer, revision.created_at, revision.id) < (%s, %s, %s::bigint)"
            )
            params.extend(cursor)
        with self.runtime.snapshot(API_PROFILE) as connection:
            rows = connection.execute(
                f"""
                SELECT revision.id AS strategy_revision_id, revision.strategy_key,
                       revision.revision, revision.name, revision.status,
                       revision.authority_group, revision.strategy_family,
                       revision.promotability, revision.actionability,
                       revision.parameters, revision.created_at, revision.promoted_at,
                       revision.supersedes_id, revision.hypothesis_id::text,
                       revision.experiment_family_id::text, revision.artifact_id,
                       revision.artifact_hash, revision.research_required,
                       revision.mechanism_class, revision.economic_mechanism,
                       revision.falsification_rule, revision.source_definition_version,
                       COALESCE(evaluation_stats.evaluation_count, 0) AS evaluation_count,
                       evaluation_stats.last_evaluation_at,
                       COALESCE(paper_stats.paper_order_count, 0) AS paper_order_count,
                       hypothesis.statement AS hypothesis,
                       hypothesis.falsification AS hypothesis_falsification,
                       family.family_key, family.name AS experiment_name,
                       family.status AS experiment_status
                FROM analysis.strategy_revision revision
                LEFT JOIN analysis.hypothesis hypothesis ON hypothesis.id = revision.hypothesis_id
                LEFT JOIN analysis.experiment_family family ON family.id = revision.experiment_family_id
                LEFT JOIN LATERAL (
                    SELECT count(*) AS evaluation_count, max(evaluation.evaluated_at) AS last_evaluation_at
                    FROM analysis.strategy_evaluation evaluation
                    WHERE evaluation.strategy_revision_id = revision.id
                      AND evaluation.evaluated_at <= now()
                      AND evaluation.available_at <= now()
                ) evaluation_stats ON TRUE
                LEFT JOIN LATERAL (
                    SELECT count(DISTINCT paper.id) AS paper_order_count
                    FROM analysis.decision decision
                    JOIN app.paper_order paper ON paper.decision_id = decision.id
                    WHERE decision.strategy_revision_id = revision.id
                      AND paper.paper_only IS TRUE
                ) paper_stats ON TRUE
                WHERE {' AND '.join(filters)}
                ORDER BY (revision.status = 'active') DESC, revision.created_at DESC,
                         revision.id DESC
                LIMIT %s
                """,
                [*params, safe_limit],
            ).fetchall()
            total = connection.execute(
                "SELECT count(*) AS count FROM analysis.strategy_revision WHERE created_at <= now()"
            ).fetchone()["count"]
            watermark = connection.execute(
                """
                SELECT max(value) AS source_watermark
                FROM (VALUES
                    ((SELECT max(created_at) FROM analysis.strategy_revision)),
                    ((SELECT max(evaluated_at) FROM analysis.strategy_evaluation)),
                    ((SELECT max(observed_at) FROM analysis.strategy_comparison))
                ) values(value)
                """
            ).fetchone()["source_watermark"]
        payload = _page(
            rows,
            total=int(total or 0),
            watermark=watermark,
            scope={"limit": safe_limit, "cursor": cursor[1].isoformat() if cursor else None},
        )
        if len(rows) < int(total or 0):
            payload["quality_status"] = "partial"
            payload["missing_evidence_reasons"] = ["strategy_history_page_bounded"]
        return payload

    def strategy_revision(self, revision_id: int) -> dict[str, Any] | None:
        with self.runtime.snapshot(API_PROFILE) as connection:
            revision = connection.execute(
                """
                SELECT revision.*, hypothesis.hypothesis_key, hypothesis.statement AS hypothesis,
                       hypothesis.mechanism_class AS hypothesis_mechanism,
                       hypothesis.falsification AS hypothesis_falsification,
                       family.family_key, family.name AS experiment_name,
                       family.design AS experiment_design, family.controls AS experiment_controls,
                       family.status AS experiment_status,
                       manifest.source_definition_version AS manifest_source_definition_version,
                       manifest.source_manifest, manifest.data_manifest, manifest.cost_manifest,
                       manifest.capacity_manifest, manifest.failure_manifest, manifest.manifest_hash,
                       dossier.id::text AS validation_dossier_id, dossier.status AS dossier_status,
                       dossier.compiled_policy, dossier.artifact_id AS dossier_artifact_id,
                       dossier.artifact_hash AS dossier_artifact_hash
                FROM analysis.strategy_revision revision
                LEFT JOIN analysis.hypothesis hypothesis ON hypothesis.id = revision.hypothesis_id
                LEFT JOIN analysis.experiment_family family ON family.id = revision.experiment_family_id
                LEFT JOIN analysis.strategy_manifest manifest ON manifest.strategy_revision_id = revision.id
                LEFT JOIN analysis.validation_dossier dossier ON dossier.strategy_revision_id = revision.id
                WHERE revision.id = %s AND revision.created_at <= now()
                """,
                [revision_id],
            ).fetchone()
            if revision is None:
                return None
            evaluations = connection.execute(
                """
                SELECT id::text AS evaluation_id, evaluation_type, evaluated_at,
                       available_at, period_start, period_end, verdict, metrics, evidence,
                       hypothesis_id::text, experiment_family_id::text, research_trial_id::text,
                       validation_dossier_id::text, artifact_id, artifact_hash, input_hash,
                       lineage
                FROM analysis.strategy_evaluation
                WHERE strategy_revision_id = %s
                  AND evaluated_at <= now() AND available_at <= now()
                ORDER BY evaluated_at DESC, id DESC
                LIMIT %s
                """,
                [revision_id, MAX_ROWS],
            ).fetchall()
            gates = connection.execute(
                """
                SELECT gate.id::text AS gate_id, gate.gate_code, gate.verdict,
                       gate.metrics, gate.evidence, gate.evaluated_at, gate.available_at
                FROM analysis.validation_gate_result gate
                JOIN analysis.validation_dossier dossier ON dossier.id = gate.dossier_id
                WHERE dossier.strategy_revision_id = %s
                  AND gate.evaluated_at <= now() AND gate.available_at <= now()
                ORDER BY gate.gate_code
                """,
                [revision_id],
            ).fetchall()
            trials = connection.execute(
                """
                SELECT trial.id::text AS research_trial_id, trial.trial_key,
                       trial.input_cutoff, trial.code_version, trial.input_hash,
                       trial.parameters, trial.status, trial.failure_reason,
                       trial.started_at, trial.finished_at, trial.available_at, trial.outcome,
                       family.family_key,
                       manifest.expected_member_count,
                       count(observation.id) AS observed_member_count,
                       count(observation.id) FILTER (WHERE observation.eligible) AS eligible_member_count,
                       count(observation.id) FILTER (WHERE NOT observation.eligible) AS excluded_member_count
                FROM analysis.research_trial trial
                JOIN analysis.experiment_family family ON family.id = trial.experiment_family_id
                LEFT JOIN analysis.trial_universe_manifest manifest
                  ON manifest.research_trial_id = trial.id
                LEFT JOIN analysis.universe_observation observation
                  ON observation.research_trial_id = trial.id
                 AND observation.available_at <= now() AND observation.observed_at <= now()
                WHERE trial.experiment_family_id = %s
                  AND trial.available_at <= now() AND trial.input_cutoff <= now()
                GROUP BY trial.id, family.family_key, manifest.expected_member_count
                ORDER BY trial.input_cutoff DESC, trial.id DESC
                LIMIT %s
                """,
                [revision["experiment_family_id"], MAX_ROWS],
            ).fetchall() if revision["experiment_family_id"] else []
            comparisons = connection.execute(
                """
                SELECT comparison.id::text AS comparison_id,
                       comparison.champion_revision_id, comparison.challenger_revision_id,
                       comparison.input_cutoff, comparison.observed_at, comparison.available_at,
                       comparison.input_hash, comparison.distinctness, comparison.explanation,
                       comparison.metrics
                FROM analysis.strategy_comparison comparison
                WHERE (comparison.champion_revision_id = %s OR comparison.challenger_revision_id = %s)
                  AND comparison.available_at <= now() AND comparison.observed_at <= now()
                ORDER BY comparison.input_cutoff DESC, comparison.id DESC
                LIMIT %s
                """,
                [revision_id, revision_id, MAX_ROWS],
            ).fetchall()
        payload = _jsonable(dict(revision))
        payload["strategy_revision_id"] = int(payload["id"])
        payload["evaluations"] = _jsonable([dict(row) for row in evaluations])
        payload["gates"] = _jsonable([dict(row) for row in gates])
        payload["trials"] = _jsonable([dict(row) for row in trials])
        payload["comparisons"] = _jsonable([dict(row) for row in comparisons])
        payload["scope"] = {"strategy_revision_id": revision_id}
        payload["calculation_version"] = CALCULATION_VERSION
        payload["evidence_status"] = _revision_evidence_status(payload)
        payload["as_of"] = datetime.now(UTC)
        return payload

    def forecast_claims(
        self,
        *,
        symbol: str | None = None,
        prompt_version: str | None = None,
        limit: int = MAX_ROWS,
        cursor: tuple[datetime, str] | None = None,
    ) -> dict[str, Any]:
        safe_limit = _limit(limit)
        filters = ["response.status = 'succeeded'"]
        params: list[Any] = []
        if symbol:
            filters.append("claim.symbol = %s")
            params.append(symbol.strip().upper())
        if prompt_version:
            filters.append("response.prompt_version = %s")
            params.append(prompt_version)
        scope_filters = list(filters)
        scope_params = list(params)
        if cursor:
            filters.append("(claim.created_at, claim.id) < (%s, %s::uuid)")
            params.extend(cursor)
        where = " AND ".join(filters)
        with self.runtime.snapshot(API_PROFILE) as connection:
            count = connection.execute(
                f"""
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE outcome.status = 'resolved' AND outcome.evidence_valid) AS eligible,
                       count(*) FILTER (WHERE outcome.id IS NULL) AS pending,
                       count(*) FILTER (WHERE outcome.status IN ('unresolvable', 'quarantined')) AS excluded,
                       max(greatest(claim.created_at, COALESCE(outcome.created_at, claim.created_at))) AS source_watermark
                FROM analysis.continuous_advisor_forecast_claim claim
                JOIN analysis.continuous_advisor_response response ON response.id = claim.response_id
                LEFT JOIN analysis.continuous_advisor_forecast_outcome outcome ON outcome.claim_id = claim.id
                WHERE {' AND '.join(scope_filters)}
                """,
                scope_params,
            ).fetchone()
            rows = connection.execute(
                f"""
                SELECT claim.id::text AS claim_id, claim.claim_key, claim.claim_kind,
                       claim.symbol, claim.statement, claim.direction, claim.probability,
                       claim.target, claim.horizon, claim.evidence_refs, claim.claim,
                       claim.created_at AS issued_at, packet.cutoff AS information_cutoff,
                       packet.id::text AS packet_id, packet.fingerprint AS packet_fingerprint,
                       response.prompt_version, response.provider, response.model,
                       response.reasoning_effort, response.id::text AS response_id,
                       outcome.status AS outcome_status, outcome.resolved_at,
                       outcome.measured_through, outcome.actual_return, outcome.excess_return,
                       outcome.actual_direction, outcome.correct, outcome.calibration_error,
                       outcome.invalidation_actual, outcome.invalidation_correct,
                       outcome.evidence_valid, outcome.metadata AS outcome_metadata,
                       attempt.status AS last_attempt_status, attempt.reason AS last_attempt_reason,
                       attempt.created_at AS last_attempt_at
                FROM analysis.continuous_advisor_forecast_claim claim
                JOIN analysis.continuous_advisor_response response ON response.id = claim.response_id
                JOIN analysis.continuous_advisor_packet packet ON packet.id = claim.packet_id
                LEFT JOIN analysis.continuous_advisor_forecast_outcome outcome ON outcome.claim_id = claim.id
                LEFT JOIN LATERAL (
                    SELECT status, reason, created_at
                    FROM analysis.continuous_advisor_resolution_attempt
                    WHERE claim_id = claim.id
                    ORDER BY created_at DESC, id DESC LIMIT 1
                ) attempt ON TRUE
                WHERE {where}
                ORDER BY claim.created_at DESC, claim.id DESC
                LIMIT %s
                """,
                [*params, safe_limit],
            ).fetchall()
        claim_rows: list[dict[str, Any]] = []
        for row in rows:
            claim = dict(row)
            claim["maturity_state"] = _claim_state(claim)
            claim["scoring_version"] = _scoring_version(claim)
            claim_rows.append(claim)
        return _page(
            claim_rows,
            total=int(count["total"] or 0),
            eligible=int(count["eligible"] or 0),
            pending=int(count["pending"] or 0),
            excluded=int(count["excluded"] or 0),
            watermark=count["source_watermark"],
            scope={"symbol": symbol.strip().upper() if symbol else None, "prompt_version": prompt_version, "limit": safe_limit},
        )

    def forecast_claim(self, claim_id: str) -> dict[str, Any] | None:
        try:
            normalized = str(UUID(claim_id))
        except (TypeError, ValueError, AttributeError):
            return None
        with self.runtime.snapshot(API_PROFILE) as connection:
            row = connection.execute(
                """
                SELECT claim.id::text AS claim_id, claim.claim_key, claim.claim_kind,
                       claim.symbol, claim.statement, claim.direction, claim.probability,
                       claim.target, claim.horizon, claim.evidence_refs, claim.claim,
                       claim.created_at AS issued_at, packet.cutoff AS information_cutoff,
                       packet.id::text AS packet_id, packet.fingerprint AS packet_fingerprint,
                       packet.packet, packet.source_refs, packet.blockers,
                       response.prompt_version, response.provider, response.model,
                       response.reasoning_effort, response.id::text AS response_id,
                       response.response, response.validation,
                       outcome.id::text AS outcome_id, outcome.status AS outcome_status,
                       outcome.resolved_at, outcome.measured_through, outcome.actual_return,
                       outcome.excess_return, outcome.actual_direction, outcome.correct,
                       outcome.calibration_error, outcome.invalidation_actual,
                       outcome.invalidation_correct, outcome.evidence_valid,
                       outcome.metadata AS outcome_metadata
                FROM analysis.continuous_advisor_forecast_claim claim
                JOIN analysis.continuous_advisor_response response ON response.id = claim.response_id
                JOIN analysis.continuous_advisor_packet packet ON packet.id = claim.packet_id
                LEFT JOIN analysis.continuous_advisor_forecast_outcome outcome ON outcome.claim_id = claim.id
                WHERE claim.id = %s::uuid
                """,
                [normalized],
            ).fetchone()
            if row is None:
                return None
            attempts = connection.execute(
                """
                SELECT id::text AS attempt_id, status, reason, measured_through, metadata, created_at
                FROM analysis.continuous_advisor_resolution_attempt
                WHERE claim_id = %s::uuid
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                [normalized, MAX_ROWS],
            ).fetchall()
        payload = _jsonable(dict(row))
        payload["resolution_attempts"] = _jsonable([dict(item) for item in attempts])
        if attempts:
            payload["last_attempt_status"] = attempts[0]["status"]
            payload["last_attempt_reason"] = attempts[0]["reason"]
            payload["last_attempt_at"] = attempts[0]["created_at"]
        payload["maturity_state"] = _claim_state(payload)
        payload["scoring_version"] = _scoring_version(payload)
        payload["as_of"] = datetime.now(UTC)
        payload["scope"] = {"claim_id": normalized}
        return payload

    def prompt_versions(self, *, default_version: str | None = None) -> dict[str, Any]:
        with self.runtime.snapshot(API_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT prompt.version, prompt.parent_version, prompt.template,
                       prompt.approved_change_set, prompt.mutation_rationale,
                       prompt.created_at,
                       count(DISTINCT response.id) AS response_count,
                       count(DISTINCT claim.id) AS claim_count,
                       max(response.finished_at) AS last_response_at,
                       max(promotion.created_at) AS last_promotion_at,
                       max(promotion.decision) FILTER (WHERE promotion.created_at = (
                           SELECT max(inner_promotion.created_at)
                           FROM analysis.continuous_advisor_promotion_decision inner_promotion
                           WHERE inner_promotion.candidate_prompt_version = prompt.version
                       )) AS last_promotion_decision
                FROM analysis.continuous_advisor_prompt_version prompt
                LEFT JOIN analysis.continuous_advisor_response response
                  ON response.prompt_version = prompt.version
                LEFT JOIN analysis.continuous_advisor_forecast_claim claim
                  ON claim.response_id = response.id
                LEFT JOIN analysis.continuous_advisor_promotion_decision promotion
                  ON promotion.candidate_prompt_version = prompt.version
                GROUP BY prompt.version
                ORDER BY prompt.created_at DESC, prompt.version DESC
                LIMIT %s
                """,
                [MAX_ROWS],
            ).fetchall()
            prompt_history = _prompt_lineage(
                connection, [str(row["version"]) for row in rows]
            )
            promotions = connection.execute(
                """
                SELECT id::text AS promotion_id, candidate_prompt_version,
                       previous_active_prompt_version, decision, reason, scorecard, created_at
                FROM analysis.continuous_advisor_promotion_decision
                ORDER BY created_at DESC, id DESC LIMIT %s
                """,
                [MAX_ROWS],
            ).fetchall()
        promotion_by_version: dict[str, dict[str, Any]] = {}
        for item in promotions:
            promotion_by_version.setdefault(str(item["candidate_prompt_version"]), dict(item))
        active_version = (
            ContinuousAdvisorRepository(self.runtime).active_prompt_version(default_version)
            if default_version
            else None
        )
        prompt_sources = {str(item["version"]): dict(item) for item in prompt_history}
        result = []
        for raw in rows:
            row = _jsonable(dict(raw))
            effective = _effective_prompt(row["version"], prompt_sources)
            row["effective_template"] = effective
            row["semantic_effective_hash"] = _hash(effective)
            row["last_promotion"] = _jsonable(promotion_by_version.get(str(row["version"])))
            row["active"] = str(row["version"]) == str(active_version) if active_version else False
            row["advisory_only"] = True
            result.append(row)
        return {
            "rows": result,
            "count": len(result),
            "as_of": datetime.now(UTC),
            "source_watermark": max((row.get("created_at") for row in rows), default=None),
            "calculation_version": CALCULATION_VERSION,
            "scope": {"default_version": default_version},
        }

    def prompt_version(self, version: str, *, default_version: str | None = None) -> dict[str, Any] | None:
        with self.runtime.snapshot(API_PROFILE) as connection:
            row = connection.execute(
                """
                SELECT prompt.version, prompt.parent_version, prompt.template,
                       prompt.approved_change_set, prompt.mutation_rationale,
                       prompt.created_at,
                       count(DISTINCT response.id) AS response_count,
                       count(DISTINCT claim.id) AS claim_count,
                       max(response.finished_at) AS last_response_at,
                       max(promotion.created_at) AS last_promotion_at,
                       max(promotion.decision) FILTER (WHERE promotion.created_at = (
                           SELECT max(inner_promotion.created_at)
                           FROM analysis.continuous_advisor_promotion_decision inner_promotion
                           WHERE inner_promotion.candidate_prompt_version = prompt.version
                       )) AS last_promotion_decision
                FROM analysis.continuous_advisor_prompt_version prompt
                LEFT JOIN analysis.continuous_advisor_response response
                  ON response.prompt_version = prompt.version
                LEFT JOIN analysis.continuous_advisor_forecast_claim claim
                  ON claim.response_id = response.id
                LEFT JOIN analysis.continuous_advisor_promotion_decision promotion
                  ON promotion.candidate_prompt_version = prompt.version
                WHERE prompt.version = %s
                GROUP BY prompt.version
                """,
                [version],
            ).fetchone()
            if row is None:
                return None
            prompt_history = _prompt_lineage(connection, [version])
        prompt_sources = {str(item["version"]): dict(item) for item in prompt_history}
        payload = _jsonable(dict(row))
        payload["effective_template"] = _effective_prompt(version, prompt_sources)
        payload["semantic_effective_hash"] = _hash(payload["effective_template"])
        parent = prompt_sources.get(str(payload.get("parent_version")))
        parent_effective = (
            _effective_prompt(str(parent["version"]), prompt_sources) if parent else {}
        )
        payload["effective_diff"] = _diff(parent_effective, payload["effective_template"])
        promotions = self._promotions(version=version)
        payload["last_promotion"] = next(
            (item for item in promotions if item.get("candidate_prompt_version") == version),
            None,
        )
        payload["source_failure_cases"] = _failure_cases(payload)
        payload["promotion_history"] = promotions
        active_version = (
            ContinuousAdvisorRepository(self.runtime).active_prompt_version(default_version)
            if default_version
            else None
        )
        payload["active"] = str(version) == str(active_version) if active_version else False
        payload["advisory_only"] = True
        payload["as_of"] = datetime.now(UTC)
        payload["calculation_version"] = CALCULATION_VERSION
        payload["scope"] = {"version": version, "default_version": default_version}
        return payload

    def experiments(
        self,
        *,
        limit: int = MAX_ROWS,
        cursor: tuple[datetime, str] | None = None,
    ) -> dict[str, Any]:
        safe_limit = _limit(limit)
        strategy_filters = [
            "comparison.available_at <= now()",
            "comparison.observed_at <= now()",
        ]
        strategy_params: list[Any] = []
        prompt_filters = ["cohort.created_at <= now()"]
        prompt_params: list[Any] = []
        if cursor:
            strategy_filters.append(
                "(comparison.observed_at, 'strategy-' || comparison.id::text) < (%s, %s)"
            )
            strategy_params.extend(cursor)
            prompt_filters.append(
                "(cohort.created_at, 'prompt-' || cohort.id::text) < (%s, %s)"
            )
            prompt_params.extend(cursor)
        with self.runtime.snapshot(API_PROFILE) as connection:
            strategy = connection.execute(
                f"""
                SELECT 'strategy-' || comparison.id::text AS experiment_id,
                       'strategy_comparison' AS experiment_kind,
                       comparison.id::text AS comparison_id,
                       comparison.champion_revision_id, champion.strategy_key AS champion_strategy_key,
                       champion.revision AS champion_revision,
                       comparison.challenger_revision_id, challenger.strategy_key AS challenger_strategy_key,
                       challenger.revision AS challenger_revision,
                       comparison.input_cutoff, comparison.observed_at, comparison.available_at,
                       comparison.input_hash, comparison.distinctness, comparison.explanation,
                       comparison.metrics, NULL::text AS active_prompt_version,
                       NULL::text AS candidate_prompt_version,
                       NULL::integer AS matched_outcomes,
                       NULL::integer AS matched_frozen_packets
                FROM analysis.strategy_comparison comparison
                JOIN analysis.strategy_revision champion ON champion.id = comparison.champion_revision_id
                JOIN analysis.strategy_revision challenger ON challenger.id = comparison.challenger_revision_id
                WHERE {' AND '.join(strategy_filters)}
                ORDER BY comparison.observed_at DESC, comparison.id DESC LIMIT %s
                """,
                [*strategy_params, safe_limit],
            ).fetchall()
            prompts = connection.execute(
                f"""
                SELECT 'prompt-' || cohort.id::text AS experiment_id,
                       'prompt_cohort' AS experiment_kind,
                       cohort.id::text AS comparison_id,
                       NULL::bigint AS champion_revision_id, NULL::text AS champion_strategy_key,
                       NULL::integer AS champion_revision,
                       NULL::bigint AS challenger_revision_id, NULL::text AS challenger_strategy_key,
                       NULL::integer AS challenger_revision,
                       cohort.cutoff_start AS input_cutoff, cohort.created_at AS observed_at,
                       cohort.created_at AS available_at, NULL::text AS input_hash,
                       CASE WHEN COALESCE(NULLIF(cohort.scorecard #>> '{{matched_cohort,common_frozen_packets}}', '')::integer, 0) > 0
                            THEN 'matched' ELSE 'pending' END AS distinctness,
                       'Matched frozen-packet prompt comparison; advisory only.' AS explanation,
                       cohort.scorecard AS metrics, cohort.active_prompt_version,
                       cohort.candidate_prompt_version,
                       COALESCE(NULLIF(cohort.scorecard #>> '{{matched_cohort,candidate,full,matched_outcomes}}', '')::integer, 0) AS matched_outcomes,
                       COALESCE(NULLIF(cohort.scorecard #>> '{{matched_cohort,common_frozen_packets}}', '')::integer, 0) AS matched_frozen_packets
                FROM analysis.continuous_advisor_evaluation_cohort cohort
                WHERE {' AND '.join(prompt_filters)}
                ORDER BY cohort.created_at DESC, cohort.id DESC LIMIT %s
                """,
                [*prompt_params, safe_limit],
            ).fetchall()
            total = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM analysis.strategy_comparison
                     WHERE available_at <= now() AND observed_at <= now())
                    +
                    (SELECT count(*) FROM analysis.continuous_advisor_evaluation_cohort
                     WHERE created_at <= now()) AS count
                """
            ).fetchone()["count"]
        rows = sorted([*strategy, *prompts], key=lambda row: row["observed_at"] or datetime.min.replace(tzinfo=UTC), reverse=True)[:safe_limit]
        payload = _page(
            rows,
            total=int(total or 0),
            scope={"limit": safe_limit, "cursor": cursor[0].isoformat() if cursor else None},
        )
        if len(rows) < int(total or 0):
            payload["quality_status"] = "partial"
            payload["missing_evidence_reasons"] = ["experiment_history_page_bounded"]
        return payload

    def experiment(self, experiment_id: str) -> dict[str, Any] | None:
        prefix, raw_id = (experiment_id.split("-", 1) + [""])[:2] if "-" in experiment_id else ("", experiment_id)
        try:
            normalized_id = str(UUID(raw_id))
        except (TypeError, ValueError, AttributeError):
            return None
        with self.runtime.snapshot(API_PROFILE) as connection:
            if prefix == "strategy":
                row = connection.execute(
                    """
                    SELECT 'strategy-' || comparison.id::text AS experiment_id,
                           'strategy_comparison' AS experiment_kind,
                           comparison.id::text AS comparison_id,
                           comparison.champion_revision_id,
                           champion.strategy_key AS champion_strategy_key,
                           champion.revision AS champion_revision,
                           comparison.challenger_revision_id,
                           challenger.strategy_key AS challenger_strategy_key,
                           challenger.revision AS challenger_revision,
                           comparison.input_cutoff, comparison.observed_at,
                           comparison.available_at, comparison.input_hash,
                           comparison.distinctness, comparison.explanation,
                           comparison.metrics,
                           NULL::text AS active_prompt_version,
                           NULL::text AS candidate_prompt_version,
                           NULL::integer AS matched_outcomes,
                           NULL::integer AS matched_frozen_packets
                    FROM analysis.strategy_comparison comparison
                    JOIN analysis.strategy_revision champion
                      ON champion.id = comparison.champion_revision_id
                    JOIN analysis.strategy_revision challenger
                      ON challenger.id = comparison.challenger_revision_id
                    WHERE comparison.id = %s::uuid
                      AND comparison.available_at <= now()
                      AND comparison.observed_at <= now()
                    """,
                    [normalized_id],
                ).fetchone()
                return _jsonable(dict(row)) if row else None
            if prefix == "prompt":
                row = connection.execute(
                    """
                    SELECT 'prompt-' || cohort.id::text AS experiment_id,
                           'prompt_cohort' AS experiment_kind,
                           cohort.id::text AS comparison_id,
                           NULL::bigint AS champion_revision_id,
                           NULL::text AS champion_strategy_key,
                           NULL::integer AS champion_revision,
                           NULL::bigint AS challenger_revision_id,
                           NULL::text AS challenger_strategy_key,
                           NULL::integer AS challenger_revision,
                           cohort.cutoff_start AS input_cutoff,
                           cohort.created_at AS observed_at,
                           cohort.created_at AS available_at,
                           NULL::text AS input_hash,
                           CASE WHEN COALESCE(NULLIF(cohort.scorecard #>> '{matched_cohort,common_frozen_packets}', '')::integer, 0) > 0
                                THEN 'matched' ELSE 'pending' END AS distinctness,
                           'Matched frozen-packet prompt comparison; advisory only.' AS explanation,
                           cohort.scorecard AS metrics,
                           cohort.active_prompt_version,
                           cohort.candidate_prompt_version,
                           COALESCE(NULLIF(cohort.scorecard #>> '{matched_cohort,candidate,full,matched_outcomes}', '')::integer, 0) AS matched_outcomes,
                           COALESCE(NULLIF(cohort.scorecard #>> '{matched_cohort,common_frozen_packets}', '')::integer, 0) AS matched_frozen_packets,
                           cohort.scorecard,
                           cohort.walk_forward_scorecard
                    FROM analysis.continuous_advisor_evaluation_cohort cohort
                    WHERE cohort.id = %s::uuid AND cohort.created_at <= now()
                    """,
                    [normalized_id],
                ).fetchone()
                return _jsonable(dict(row)) if row else None
        return None

    def events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = _limit(limit)
        with self.runtime.read(API_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT event_type, event_at, original_version, new_version, reason, evidence_id
                FROM (
                    SELECT 'strategy_revision' AS event_type, revision.created_at AS event_at,
                           revision.supersedes_id::text AS original_version,
                           revision.id::text AS new_version,
                           revision.status AS reason, revision.artifact_id AS evidence_id
                    FROM analysis.strategy_revision revision
                    UNION ALL
                    SELECT 'strategy_evaluation', evaluation.evaluated_at,
                           evaluation.strategy_revision_id::text, evaluation.id::text,
                           evaluation.verdict, evaluation.artifact_id
                    FROM analysis.strategy_evaluation evaluation
                    UNION ALL
                    SELECT 'prompt_promotion', promotion.created_at,
                           promotion.previous_active_prompt_version,
                           promotion.candidate_prompt_version, promotion.decision,
                           promotion.id::text
                    FROM analysis.continuous_advisor_promotion_decision promotion
                ) events
                WHERE event_at <= now()
                ORDER BY event_at DESC NULLS LAST
                LIMIT %s
                """,
                [safe_limit],
            ).fetchall()
        return _jsonable([dict(row) for row in rows])

    def artifact(self, artifact_id: str) -> dict[str, Any] | None:
        normalized = artifact_id.strip()
        if not normalized or len(normalized) > 200 or any(ord(char) < 32 for char in normalized):
            return None
        with self.runtime.snapshot(API_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT artifact_type, artifact_id, artifact_hash, created_at, available_at,
                       source_relation, source_id, preview
                FROM (
                    SELECT 'strategy_revision' AS artifact_type, revision.artifact_id,
                           trim(revision.artifact_hash) AS artifact_hash, revision.created_at,
                           revision.promoted_at AS available_at,
                           'analysis.strategy_revision' AS source_relation,
                           revision.id::text AS source_id,
                           jsonb_build_object(
                               'strategy_key', revision.strategy_key,
                               'revision', revision.revision,
                               'status', revision.status,
                               'strategy_family', revision.strategy_family
                           ) AS preview
                    FROM analysis.strategy_revision revision
                    WHERE revision.artifact_id = %s
                    UNION ALL
                    SELECT 'strategy_evaluation', evaluation.artifact_id,
                           trim(evaluation.artifact_hash), evaluation.evaluated_at,
                           evaluation.available_at, 'analysis.strategy_evaluation',
                           evaluation.id::text,
                           jsonb_build_object(
                               'strategy_revision_id', evaluation.strategy_revision_id,
                               'evaluation_type', evaluation.evaluation_type,
                               'verdict', evaluation.verdict,
                               'input_hash', evaluation.input_hash
                           )
                    FROM analysis.strategy_evaluation evaluation
                    WHERE evaluation.artifact_id = %s
                    UNION ALL
                    SELECT 'validation_dossier', dossier.artifact_id,
                           trim(dossier.artifact_hash), dossier.created_at,
                           dossier.sealed_at, 'analysis.validation_dossier',
                           dossier.id::text,
                           jsonb_build_object(
                               'strategy_revision_id', dossier.strategy_revision_id,
                               'status', dossier.status
                           )
                    FROM analysis.validation_dossier dossier
                    WHERE dossier.artifact_id = %s
                ) known
                WHERE artifact_id IS NOT NULL
                ORDER BY created_at DESC, source_id DESC
                LIMIT %s
                """,
                [normalized, normalized, normalized, MAX_ROWS],
            ).fetchall()
        if not rows:
            return None
        payload_rows = _jsonable([dict(row) for row in rows])
        return {
            "artifact_id": normalized,
            "records": payload_rows,
            "content_available": False,
            "retention_state": "database_metadata_only",
            "quality_status": "metadata_only",
            "as_of": datetime.now(UTC),
            "source_watermark": max(
                (row.get("created_at") for row in payload_rows), default=None
            ),
            "calculation_version": CALCULATION_VERSION,
            "scope": {"artifact_id": normalized},
        }

    def _promotions(self, *, version: str | None = None) -> list[dict[str, Any]]:
        with self.runtime.read(API_PROFILE) as connection:
            rows = connection.execute(
                f"""
                SELECT id::text AS promotion_id, candidate_prompt_version,
                       previous_active_prompt_version, decision, reason, scorecard, created_at
                FROM analysis.continuous_advisor_promotion_decision
                {"WHERE candidate_prompt_version = %s OR previous_active_prompt_version = %s" if version else ""}
                ORDER BY created_at DESC, id DESC LIMIT %s
                """,
                [version, version, MAX_ROWS] if version else [MAX_ROWS],
            ).fetchall()
        return _jsonable([dict(row) for row in rows])


def _prompt_lineage(connection: Any, roots: list[str]) -> list[dict[str, Any]]:
    if not roots:
        return []
    rows = connection.execute(
        """
        WITH RECURSIVE prompt_tree(version, parent_version, template) AS (
            SELECT prompt.version, prompt.parent_version, prompt.template
            FROM analysis.continuous_advisor_prompt_version prompt
            WHERE prompt.version = ANY(%s::text[])
            UNION
            SELECT parent.version, parent.parent_version, parent.template
            FROM analysis.continuous_advisor_prompt_version parent
            JOIN prompt_tree child ON child.parent_version = parent.version
        )
        SELECT version, parent_version, template FROM prompt_tree
        """,
        [roots],
    ).fetchall()
    return [dict(row) for row in rows]


def _page(rows: list[Any], *, total: int, eligible: int = 0, pending: int = 0, excluded: int = 0,
          watermark: Any = None, scope: dict[str, Any]) -> dict[str, Any]:
    return {
        "rows": _jsonable([dict(row) for row in rows]),
        "count": {"total": total, "eligible": eligible, "pending": pending, "excluded": excluded},
        "as_of": datetime.now(UTC),
        "source_watermark": watermark,
        "calculation_version": CALCULATION_VERSION,
        "scope": scope,
        "quality_status": "complete" if not excluded else "partial",
    }


def _limit(value: int) -> int:
    return max(1, min(MAX_ROWS, int(value)))


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    return value


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _effective_prompt(version: str, rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    seen: set[str] = set()
    current = rows.get(version)
    while current and current["version"] not in seen:
        seen.add(current["version"])
        template = current.get("template")
        if isinstance(template, dict):
            merged = {**template, **merged}
        current = rows.get(str(current.get("parent_version")))
    return merged


def _diff(parent: dict[str, Any], current: dict[str, Any]) -> dict[str, dict[str, Any]]:
    keys = sorted(set(parent) | set(current))
    return {key: {"before": parent.get(key), "after": current.get(key)} for key in keys if parent.get(key) != current.get(key)}


def _claim_state(row: dict[str, Any]) -> str:
    outcome = row.get("outcome_status")
    if outcome == "resolved":
        return "resolved"
    if outcome == "quarantined":
        return "unsupported"
    if outcome == "unresolvable":
        return "blocked"
    if row.get("last_attempt_status") == "blocked":
        return "blocked"
    return "pending"


def _scoring_version(row: dict[str, Any]) -> str:
    metadata = row.get("outcome_metadata")
    if isinstance(metadata, dict) and metadata.get("scoring_version"):
        return str(metadata["scoring_version"])
    return "continuous-advisor-score.v2"


def _failure_cases(row: dict[str, Any]) -> list[Any]:
    metadata = row.get("last_promotion")
    if isinstance(metadata, dict):
        scorecard = metadata.get("scorecard")
        if isinstance(scorecard, dict):
            gate = scorecard.get("gate")
            comparison = scorecard.get("comparison")
            return list(
                scorecard.get("blockers")
                or (gate.get("blockers") if isinstance(gate, dict) else [])
                or (comparison.get("blockers") if isinstance(comparison, dict) else [])
                or []
            )
    return []


def _revision_evidence_status(row: dict[str, Any]) -> str:
    if row.get("status") == "active":
        return "deployed"
    if row.get("evaluations"):
        return "evaluated"
    if row.get("experiment_family_id"):
        return "collecting"
    return "unavailable"


__all__ = ["ResearchWorkbenchRepository"]
