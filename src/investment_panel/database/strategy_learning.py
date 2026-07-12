"""Deterministic PostgreSQL evaluation of agent-proposed strategy changes."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.database.strategy_parameters import (
    EVALUABLE_GATES,
    MAXIMUM_GATES,
    MINIMUM_GATES,
    canonical_gate_name,
    merge_strategy_parameters,
    normalize_gates,
)


class StrategyLearningRepository:
    """Materialize advisory postmortems behind deterministic promotion gates."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def materialize_postmortem(self, postmortem_task_id: str, payload: dict[str, Any]) -> dict[str, int]:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            return self.materialize_postmortem_in_transaction(connection, postmortem_task_id, payload)

    def materialize_postmortem_in_transaction(
        self, connection: Any, postmortem_task_id: str, payload: dict[str, Any]
    ) -> dict[str, int]:
        raw_changes = payload.get("proposed_parameter_changes")
        if not isinstance(raw_changes, dict):
            return {"strategy_proposals": 0, "strategy_backtests": 0, "strategy_forward_tests": 0}
        changes = {str(key): value for key, value in raw_changes.items() if value not in (None, "")}
        if not changes:
            return {"strategy_proposals": 0, "strategy_backtests": 0, "strategy_forward_tests": 0}
        source = connection.execute(
            """
            SELECT task.id, decision.strategy_revision_id
            FROM analysis.agent_task task
            LEFT JOIN analysis.decision decision ON decision.id = task.decision_id
            WHERE task.id = %s AND task.task_kind = 'option_postmortem'
            FOR UPDATE OF task
            """,
            [postmortem_task_id],
        ).fetchone()
        if source is None:
            raise ValueError(f"postmortem task not found: {postmortem_task_id}")
        existing = connection.execute(
            "SELECT id, validation FROM analysis.agent_task WHERE task_kind = 'strategy_mutation_proposal' "
            "AND request->>'postmortem_task_id' = %s LIMIT 1",
            [postmortem_task_id],
        ).fetchone()
        if existing:
            if dict(existing["validation"] or {}).get("status") == "promoted":
                return {
                    "strategy_proposals": 0,
                    "strategy_backtests": 0,
                    "strategy_forward_tests": 0,
                }
            return {"strategy_proposals": 0, **self._evaluate(connection, existing["id"])}
        base = self._resolve_base(connection, source["strategy_revision_id"])
        digest = hashlib.sha256(
            f"{postmortem_task_id}:{json.dumps(changes, sort_keys=True)}".encode()
        ).hexdigest()[:10]
        proposed_key = f"{base['strategy_key']}__agent_{digest}"
        parameters = merge_strategy_parameters(dict(base["parameters"] or {}), changes)
        candidate = connection.execute(
            "SELECT id, status, parameters, supersedes_id, authority_group "
            "FROM analysis.strategy_revision "
            "WHERE strategy_key = %s AND revision = 1 FOR UPDATE",
            [proposed_key],
        ).fetchone()
        if candidate is None:
            candidate = connection.execute(
                """
                INSERT INTO analysis.strategy_revision
                    (strategy_key, revision, name, status, parameters, supersedes_id, authority_group)
                VALUES (%s, 1, %s, 'candidate', %s, %s, %s)
                RETURNING id, status, parameters, supersedes_id, authority_group
                """,
                [
                    proposed_key, proposed_key, Jsonb(parameters), base["id"],
                    base["authority_group"],
                ],
            ).fetchone()
        elif (
            candidate["status"] != "candidate"
            or candidate["supersedes_id"] != base["id"]
            or candidate["authority_group"] != base["authority_group"]
            or dict(candidate["parameters"] or {}) != parameters
        ):
            raise ValueError("proposed strategy key collides with an existing revision")
        result = {
            "status": "backtest_required",
            "source_postmortem_id": postmortem_task_id,
            "strategy_version": str(base["strategy_key"]),
            "proposed_strategy_version": proposed_key,
            "proposed_parameter_changes": changes,
            "expected_effect": payload.get("expected_effect"),
            "risk": payload.get("risk"),
            "candidate_revision_id": candidate["id"],
            "promotion_policy": "deterministic_backtest_forward_test_and_human_approval",
        }
        proposal = connection.execute(
            """
            INSERT INTO analysis.agent_task (task_kind, status, request, result, validation)
            VALUES ('strategy_mutation_proposal', 'completed', %s, %s, %s)
            RETURNING id
            """,
            [
                Jsonb({"postmortem_task_id": postmortem_task_id}),
                Jsonb(result),
                Jsonb({"status": "deterministic_evaluation_required"}),
            ],
        ).fetchone()
        counts = self._evaluate(connection, proposal["id"])
        return {"strategy_proposals": 1, **counts}

    @staticmethod
    def _resolve_base(connection: Any, source_strategy_id: int | None) -> Any:
        connection.execute(
            """
            INSERT INTO analysis.strategy_revision
                (strategy_key, revision, name, status, parameters, authority_group, promoted_at)
            SELECT 'options-radar-core', 1, 'options-radar-core', 'active', %s,
                   'options-radar-core', now()
            WHERE NOT EXISTS (
                SELECT 1 FROM analysis.strategy_revision
                WHERE authority_group = 'options-radar-core' AND status = 'active'
            )
            ON CONFLICT (strategy_key, revision) DO NOTHING
            """,
            [Jsonb(_DEFAULT_PARAMETERS)],
        )
        if source_strategy_id is not None:
            base = connection.execute(
                """
                WITH RECURSIVE ancestry AS (
                    SELECT id, strategy_key, revision, parameters, supersedes_id, authority_group
                    FROM analysis.strategy_revision WHERE id = %s
                    UNION ALL
                    SELECT parent.id, parent.strategy_key, parent.revision,
                           parent.parameters, parent.supersedes_id, parent.authority_group
                    FROM analysis.strategy_revision parent
                    JOIN ancestry child ON child.supersedes_id = parent.id
                )
                SELECT source.id, source.strategy_key, source.revision, source.parameters,
                       source.authority_group,
                       EXISTS (SELECT 1 FROM ancestry WHERE strategy_key = 'options-radar-core') AS in_core_lineage
                FROM analysis.strategy_revision source WHERE source.id = %s
                """,
                [source_strategy_id, source_strategy_id],
            ).fetchone()
            if (
                base is None
                or not base["in_core_lineage"]
                or base["authority_group"] != "options-radar-core"
            ):
                raise ValueError("source decision strategy is outside the options-radar-core lineage")
            return base
        return connection.execute(
            """
            SELECT id, strategy_key, revision, parameters, authority_group
            FROM analysis.strategy_revision
            WHERE authority_group = 'options-radar-core' AND status = 'active'
            """
        ).fetchone()

    def refresh_evaluations(self) -> dict[str, int]:
        totals = {"strategy_backtests": 0, "strategy_forward_tests": 0}
        with self.runtime.transaction(JOB_PROFILE) as connection:
            proposals = connection.execute(
                "SELECT id FROM analysis.agent_task WHERE task_kind = 'strategy_mutation_proposal' "
                "AND status = 'completed' AND COALESCE(validation->>'status', '') <> 'promoted' "
                "ORDER BY created_at"
            ).fetchall()
            for proposal in proposals:
                counts = self._evaluate(connection, proposal["id"])
                for key in totals:
                    totals[key] += counts[key]
        return totals

    def _evaluate(self, connection: Any, proposal_id: Any) -> dict[str, int]:
        proposal = connection.execute(
            "SELECT id, created_at, result FROM analysis.agent_task WHERE id = %s FOR UPDATE",
            [proposal_id],
        ).fetchone()
        if proposal is None:
            raise ValueError(f"strategy proposal not found: {proposal_id}")
        result = dict(proposal["result"] or {})
        candidate_id = result.get("candidate_revision_id")
        if candidate_id is None:
            return {"strategy_backtests": 0, "strategy_forward_tests": 0}
        candidate = connection.execute(
            """
            SELECT candidate.parameters, candidate.supersedes_id,
                   base.parameters AS base_parameters
            FROM analysis.strategy_revision candidate
            LEFT JOIN analysis.strategy_revision base ON base.id = candidate.supersedes_id
            WHERE candidate.id = %s
            """,
            [candidate_id],
        ).fetchone()
        rows = [
            dict(row)
            for row in connection.execute(_OUTCOME_QUERY, [candidate["supersedes_id"]]).fetchall()
        ]
        proposed_rows = [row for row in rows if _passes(row, dict(candidate["parameters"] or {}))]
        capability = _evaluation_capability(
            dict(candidate["base_parameters"] or {}),
            dict(result.get("proposed_parameter_changes") or {}),
        )
        backtest = _evaluation(rows, proposed_rows, minimum=20)
        if capability["blocking_verdict"]:
            backtest = {
                **backtest,
                "verdict": capability["blocking_verdict"],
                "blocked_parameters": capability["blocked_parameters"],
            }
        forward_source = [row for row in rows if row["as_of"] >= proposal["created_at"]]
        forward_rows = [row for row in forward_source if _passes(row, dict(candidate["parameters"] or {}))]
        forward = _evaluation(forward_source, forward_rows, minimum=20, require_span_days=30)
        self._store_evaluation(connection, candidate_id, "backtest", backtest, rows)
        self._store_evaluation(connection, candidate_id, "forward_shadow_test", forward, forward_source)
        status = _proposal_status(str(backtest["verdict"]), str(forward["verdict"]))
        result["status"] = status
        connection.execute(
            "UPDATE analysis.agent_task SET result = %s, validation = %s, updated_at = now() WHERE id = %s",
            [Jsonb(result), Jsonb({"status": status, "authority": "deterministic"}), proposal_id],
        )
        return {"strategy_backtests": 1, "strategy_forward_tests": 1}

    @staticmethod
    def _store_evaluation(
        connection: Any,
        candidate_id: int,
        evaluation_type: str,
        evaluation: dict[str, Any],
        source_rows: list[dict[str, Any]],
    ) -> None:
        connection.execute(
            "DELETE FROM analysis.strategy_evaluation WHERE strategy_revision_id = %s AND evaluation_type = %s",
            [candidate_id, evaluation_type],
        )
        connection.execute(
            """
            INSERT INTO analysis.strategy_evaluation
                (strategy_revision_id, evaluation_type, evaluated_at, period_start,
                 period_end, verdict, metrics, evidence)
            VALUES (%s, %s, now(), %s, %s, %s, %s, %s)
            """,
            [
                candidate_id,
                evaluation_type,
                min((row["as_of"] for row in source_rows), default=None),
                max((row["as_of"] for row in source_rows), default=None),
                evaluation["verdict"],
                Jsonb(evaluation),
                Jsonb([{"source": "analysis.option_outcome", "actionable_only": True}]),
            ],
        )


_OUTCOME_QUERY = """
    SELECT decision.as_of, feature.modeled_delta, feature.dte, feature.spread_pct,
           feature.iv_percentile, feature.required_move_pct,
           quote.open_interest, quote.volume, outcome.peak_return
    FROM analysis.option_outcome outcome
    JOIN analysis.decision decision ON decision.id = outcome.decision_id
    JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
    JOIN analysis.option_feature feature
      ON feature.snapshot_id = option_decision.snapshot_id
     AND feature.contract_id = option_decision.contract_id
     AND feature.quote_observed_at = option_decision.quote_observed_at
     AND feature.run_id = decision.run_id
    JOIN raw.option_quote quote
      ON quote.snapshot_id = option_decision.snapshot_id
     AND quote.contract_id = option_decision.contract_id
     AND quote.observed_at = option_decision.quote_observed_at
    WHERE outcome.peak_return IS NOT NULL
      AND outcome.maturity_state IN ('mature', 'expired')
      AND decision.strategy_revision_id = %s
"""

_DEFAULT_PARAMETERS = {
    "feature_version": "option-core-v1",
    "gates": {"max_spread_pct": 0.25, "min_open_interest": 50, "min_dte": 14, "max_dte": 900},
}


_METADATA_CHANGES = {"candidate_note", "filter_reason"}


def _evaluation_capability(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    base_gates = normalize_gates(base)
    unsupported: list[str] = []
    loosened: list[str] = []
    evaluated = 0
    for key, value in changes.items():
        if value is None or key in _METADATA_CHANGES:
            continue
        canonical = canonical_gate_name(key)
        if canonical not in EVALUABLE_GATES:
            unsupported.append(key)
            continue
        evaluated += 1
        baseline = base_gates.get(canonical)
        if baseline is None:
            loosened.append(key)
            continue
        if canonical in MINIMUM_GATES and float(value) < float(baseline):
            loosened.append(key)
        if canonical in MAXIMUM_GATES and float(value) > float(baseline):
            loosened.append(key)
    if unsupported or evaluated == 0:
        return {
            "blocking_verdict": "unsupported_parameters",
            "blocked_parameters": unsupported or sorted(changes),
        }
    if loosened:
        return {
            "blocking_verdict": "requires_rejected_or_shadow_outcomes",
            "blocked_parameters": loosened,
        }
    return {"blocking_verdict": None, "blocked_parameters": []}


def _passes(row: dict[str, Any], parameters: dict[str, Any]) -> bool:
    gates = normalize_gates(parameters)
    checks = (
        ("max_spread_pct", row.get("spread_pct"), lambda actual, limit: actual <= limit),
        ("min_open_interest", row.get("open_interest"), lambda actual, limit: actual >= limit),
        ("min_volume", row.get("volume"), lambda actual, limit: actual >= limit),
        ("min_dte", row.get("dte"), lambda actual, limit: actual >= limit),
        ("max_dte", row.get("dte"), lambda actual, limit: actual <= limit),
        ("delta_min", abs(row["modeled_delta"]) if row.get("modeled_delta") is not None else None, lambda actual, limit: actual >= limit),
        ("delta_max", abs(row["modeled_delta"]) if row.get("modeled_delta") is not None else None, lambda actual, limit: actual <= limit),
        ("max_required_move_pct", row.get("required_move_pct"), lambda actual, limit: actual <= limit),
        ("max_iv_percentile", row.get("iv_percentile"), lambda actual, limit: actual <= limit),
    )
    return all(key not in gates or actual is not None and compare(float(actual), float(gates[key])) for key, actual, compare in checks)


def _evaluation(baseline: list[dict[str, Any]], proposed: list[dict[str, Any]], *, minimum: int, require_span_days: int = 0) -> dict[str, Any]:
    baseline_metrics = _metrics(baseline)
    proposed_metrics = _metrics(proposed)
    span_days = (
        (max(row["as_of"] for row in baseline) - min(row["as_of"] for row in baseline)).days
        if baseline else 0
    )
    if len(proposed) < minimum or span_days < require_span_days:
        verdict = "collecting_data" if require_span_days else "insufficient_data"
    elif (
        proposed_metrics["hit_rate_2x"] >= baseline_metrics["hit_rate_2x"]
        and proposed_metrics["false_positive_rate"] <= baseline_metrics["false_positive_rate"] + 0.02
    ):
        verdict = "pass"
    else:
        verdict = "fail"
    return {
        "verdict": verdict,
        "baseline": baseline_metrics,
        "proposed": proposed_metrics,
        "minimum_sample": minimum,
        "observation_span_days": span_days,
        "scope": "retained_actionable_decisions_only",
    }


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    peaks = [float(row["peak_return"]) for row in rows]
    count = len(peaks)
    return {
        "sample_size": count,
        "hit_rate_2x": sum(value >= 1 for value in peaks) / count if count else 0.0,
        "hit_rate_5x": sum(value >= 4 for value in peaks) / count if count else 0.0,
        "hit_rate_10x": sum(value >= 9 for value in peaks) / count if count else 0.0,
        "false_positive_rate": sum(value <= 0 for value in peaks) / count if count else 0.0,
    }


def _proposal_status(backtest: str, forward: str) -> str:
    if backtest == "fail":
        return "backtest_failed"
    if backtest != "pass":
        return "backtest_required"
    if forward == "fail":
        return "forward_test_failed"
    if forward != "pass":
        return "forward_test_required"
    return "ready"
