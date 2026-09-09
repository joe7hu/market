"""Deterministic PostgreSQL evaluation of agent-proposed strategy changes."""

from __future__ import annotations

import hashlib
import json
import math
from statistics import mean, pstdev
from typing import Any, Mapping
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.infrastructure.postgres.options_paper_ledger import PAPER_FILL_MULTIPLIERS_SQL
from investment_panel.domain.decision import TRACKED_METRICS, MARKET_TZ
from investment_panel.infrastructure.postgres.strategy_parameters import (
    merge_strategy_parameters,
    mutation_capability as _evaluation_capability,
    normalize_gates,
)

OPTIONS_COMPARISON_VERSION = "options-independent-comparison-v1"


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
            "promotion_policy": "deterministic_walk_forward_shadow_execution_grade_paper",
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
            for row in connection.execute(OUTCOME_QUERY, [candidate["supersedes_id"]] * 2).fetchall()
        ]
        candidate_rows = [
            dict(row)
            for row in connection.execute(OUTCOME_QUERY, [candidate_id] * 2).fetchall()
        ]
        rows = measured_rows(rows)
        candidate_rows = measured_rows(candidate_rows)
        historical = _independent_rows([row for row in rows if row["as_of"] < proposal["created_at"]])
        proposed_rows = [row for row in historical if _passes(row, dict(candidate["parameters"] or {}))]
        capability = _evaluation_capability(
            dict(candidate["base_parameters"] or {}),
            dict(result.get("proposed_parameter_changes") or {}),
        )
        backtest = evaluate_comparison(historical, proposed_rows, minimum=100, require_span_days=120)
        if capability["blocking_verdict"]:
            backtest = {
                **backtest,
                "verdict": capability["blocking_verdict"],
                "blocked_parameters": capability["blocked_parameters"],
            }
        observations = [dict(row) for row in connection.execute(
            OBSERVATION_QUERY,
            [[candidate["supersedes_id"], candidate_id], proposal["created_at"]],
        ).fetchall()]
        forward_source, forward_rows, forward_context = forward_cohort(
            observations, rows, candidate_rows, candidate_id=candidate_id,
            parent_id=candidate["supersedes_id"], proposal_id=str(proposal_id), span_days=30,
        )
        forward = evaluate_comparison(forward_source, forward_rows, minimum=30, require_span_days=30, paired=True, **forward_context)
        execution_source, execution_rows, execution_cohort = forward_cohort(
            observations, rows, candidate_rows, candidate_id=candidate_id,
            parent_id=candidate["supersedes_id"], proposal_id=str(proposal_id), span_days=20,
            execution_grade=True,
        )
        execution_grade = evaluate_comparison(execution_source, execution_rows, minimum=20, require_span_days=20, paired=True, **execution_cohort)
        self._store_evaluation(connection, candidate_id, "walk_forward", backtest, proposed_rows)
        self._store_evaluation(connection, candidate_id, "shadow", forward, forward_rows)
        if execution_rows:
            self._store_evaluation(
                connection, candidate_id, "execution_grade_paper", execution_grade,
                execution_rows, execution_grade=True,
            )
        status = _proposal_status(
            str(backtest["verdict"]), str(forward["verdict"]), str(execution_grade["verdict"]),
        )
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
        *,
        execution_grade: bool = False,
    ) -> None:
        metrics = _phase7_metrics(evaluation, source_rows)
        evidence = _phase7_evidence(
            evaluation, source_rows, execution_grade=execution_grade,
            candidate_revision_id=candidate_id,
        )
        period_start, period_end = _evaluation_period(evaluation.get("cohort"), source_rows)
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
                period_start,
                period_end,
                evaluation["verdict"],
                Jsonb(metrics),
                Jsonb(evidence),
            ],
        )


PAPER_EPISODE_ORDERS_SQL = """
    SELECT count(*) AS paper_order_count, min(paper.created_at) AS first_paper_at,
           max(paper.created_at) AS last_paper_at
    FROM app.paper_order paper JOIN analysis.decision original ON original.id = paper.decision_id
    WHERE original.strategy_revision_id = decision.strategy_revision_id
      AND original.episode_key = decision.episode_key AND original.lane = decision.lane
      AND paper.paper_only IS TRUE AND paper.created_at <= now()
"""


OUTCOME_QUERY = f"""
    WITH qualified_outcomes AS (
        SELECT outcome.*
        FROM analysis.option_outcome outcome
        JOIN analysis.decision decision ON decision.id = outcome.decision_id
        LEFT JOIN analysis.shadow_trade shadow ON shadow.id = outcome.shadow_trade_id
             AND shadow.decision_id = decision.id
        WHERE decision.strategy_revision_id = %s
          AND outcome.outcome_classification = 'captured'
          AND outcome.maturity_state IN ('mature', 'expired')
          AND outcome.current_return IS NOT NULL AND decision.as_of <= now()
          AND outcome.current_return > '-Infinity'::double precision
          AND outcome.current_return < 'Infinity'::double precision
          AND outcome.updated_at <= now()
          AND outcome.observed_through >= decision.as_of AND outcome.observed_through <= now()
          AND decision.sample_eligible IS TRUE AND outcome.sample_eligible IS TRUE
          AND decision.quarantine_reason IS NULL AND outcome.quarantine_reason IS NULL
          AND outcome.lane = decision.lane AND outcome.episode_key = decision.episode_key
          AND outcome.calibration_cohort = decision.calibration_cohort
          AND decision.calibration_cohort LIKE 'option-scorecard-truth-v1:%%'
          AND outcome.calibration_cohort LIKE 'option-scorecard-truth-v1:%%'
          AND (outcome.promotion_eligible IS TRUE OR (
              shadow.source_kind = 'options_paper_experiment' AND shadow.status = 'closed'
              AND decision.state IN ('WATCH', 'SETUP', 'READY')
              AND outcome.objective_version = 'option-paper-experiment-v1'
              AND decision.as_of < outcome.entry_fill_at AND outcome.entry_fill_at < outcome.exit_fill_at
              AND outcome.entry_fill_at = shadow.entry_at AND outcome.exit_fill_at = shadow.exit_at
              AND outcome.entry_fill_price = shadow.entry_price AND outcome.exit_fill_price = shadow.exit_price
              AND outcome.entry_fill_price > 0 AND outcome.entry_fill_price < 'Infinity'::numeric
              AND outcome.exit_fill_price >= 0 AND outcome.exit_fill_price < 'Infinity'::numeric
              AND outcome.exit_fill_at <= now() AND outcome.observed_through <= now()
              AND outcome.fee_total >= 0 AND outcome.slippage_total >= 0
              AND outcome.fee_total < 'Infinity'::numeric AND outcome.slippage_total < 'Infinity'::numeric
          ))
    ), eligible AS (
        SELECT decision_id FROM qualified_outcomes
        UNION
        SELECT paper.decision_id FROM app.paper_order paper
        JOIN analysis.decision decision ON decision.id = paper.decision_id
        WHERE decision.strategy_revision_id = %s AND paper.paper_only IS TRUE
          AND paper.status IN ('exited', 'closed') AND paper.exit_at <= now()
    )
    SELECT decision.as_of, decision.lane, decision.episode_key, decision.score, feature.modeled_delta, feature.dte, feature.spread_pct,
           feature.iv_percentile, feature.required_move_pct, quote.open_interest, quote.volume,
           outcome.peak_return, outcome.current_return, outcome.max_drawdown,
           outcome.entry_fill_at, outcome.exit_fill_at, outcome.observed_through,
           decision.id::text AS decision_id, decision.strategy_revision_id,
           option_decision.probability_profit, option_decision.structure, option_decision.market_regime,
           instrument.symbol AS ticker,
           paper.id::text AS paper_order_id, paper.paper_only, paper.status AS paper_status,
           paper.filled_at, paper.exit_at, fills.entry_price AS actual_fill_price,
           fills.exit_price, paper.filled_quantity, paper.exited_quantity,
           paper.entry_slippage, paper.exit_slippage, paper.fees, paper.contract_multiplier,
           paper.execution_quote->'observed_liquidation_v1' AS paper_marks,
           paper.reserved_collateral, fills.entry_quantity, fills.exit_quantity, fills.fill_multipliers_verified,
           paper_episode.paper_order_count, paper_episode.first_paper_at, paper_episode.last_paper_at
    FROM eligible
    JOIN analysis.decision decision ON decision.id = eligible.decision_id
    JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
    JOIN analysis.option_feature feature
      ON feature.snapshot_id = option_decision.snapshot_id AND feature.contract_id = option_decision.contract_id
     AND feature.quote_observed_at = option_decision.quote_observed_at AND feature.run_id = decision.run_id
    JOIN raw.option_quote quote
      ON quote.snapshot_id = option_decision.snapshot_id AND quote.contract_id = option_decision.contract_id
     AND quote.observed_at = option_decision.quote_observed_at
    JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
    LEFT JOIN qualified_outcomes outcome ON outcome.decision_id = decision.id
    LEFT JOIN LATERAL ({PAPER_EPISODE_ORDERS_SQL}) paper_episode ON TRUE
    LEFT JOIN LATERAL (
        SELECT paper.* FROM app.paper_order paper
        WHERE paper.decision_id = decision.id AND paper.paper_only IS TRUE
          AND paper.created_at <= now()
        ORDER BY paper.created_at, paper.id LIMIT 1
    ) paper ON TRUE
    LEFT JOIN LATERAL (
        SELECT sum(quantity * price) FILTER (WHERE action = 'paper_entry')
                   / nullif(sum(quantity) FILTER (WHERE action = 'paper_entry'), 0) AS entry_price,
               sum(quantity * price) FILTER (WHERE action = 'paper_exit' OR action LIKE 'paper_exit:%%')
                   / nullif(sum(quantity) FILTER (WHERE action = 'paper_exit' OR action LIKE 'paper_exit:%%'), 0) AS exit_price,
               sum(quantity) FILTER (WHERE action = 'paper_entry') AS entry_quantity,
               sum(quantity) FILTER (WHERE action = 'paper_exit' OR action LIKE 'paper_exit:%%') AS exit_quantity,
               {PAPER_FILL_MULTIPLIERS_SQL} AS fill_multipliers_verified
        FROM app.trade_journal
        WHERE details->>'paper_order_id' = paper.id::text AND decision_id = decision.id
          AND quantity > 0 AND price IS NOT NULL
          AND rationale = 'deterministic_options_paper_execution'
          AND created_at <= now()
    ) fills ON TRUE
    WHERE nullif(btrim(decision.episode_key), '') IS NOT NULL
      AND decision.quarantine_reason IS NULL AND decision.as_of <= now()
    ORDER BY decision.as_of, decision.id
"""


OBSERVATION_QUERY = f"""
    SELECT decision.id::text AS decision_id, decision.strategy_revision_id,
           decision.episode_key, decision.lane, decision.as_of, decision.state, shadow.status AS shadow_status,
           shadow.pending_entry_reason, shadow.entry_at, shadow.entry_price,
           shadow.exit_at, shadow.exit_price, shadow.metrics,
           run.inputs, run.run_type, run.id::text AS run_id,
           publication.scope, item.payload, paper.paper_order_count, paper.first_paper_at, paper.last_paper_at
    FROM analysis.shadow_trade shadow
    JOIN analysis.decision decision ON decision.id = shadow.decision_id
    JOIN analysis.run run ON run.id = decision.run_id AND run.strategy_revision_id = decision.strategy_revision_id
    LEFT JOIN app.publication publication ON publication.id::text = shadow.metrics->>'publication_id'
         AND publication.analysis_run_id = run.id AND publication.published_at <= now()
    LEFT JOIN app.publication_content_item item ON item.publication_id = publication.id
         AND item.model_name = 'option_paper_experiment' AND item.payload->>'decision_id' = decision.id::text
    LEFT JOIN LATERAL ({PAPER_EPISODE_ORDERS_SQL}) paper ON true
    WHERE shadow.source_kind = 'options_paper_experiment'
      AND decision.strategy_revision_id = ANY(%s::bigint[])
      AND decision.as_of >= %s AND decision.as_of <= now() AND shadow.created_at <= now()
      AND run.status = 'succeeded' AND run.input_cutoff <= now()
    ORDER BY decision.as_of, decision.id
"""


def _independent_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    independent: dict[Any, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda item: (item["as_of"], str(item.get("decision_id") or ""))):
        independent.setdefault(_comparison_key(row), row)
    return list(independent.values())


def observation_lineage_matches(
    row: Mapping[str, Any], *, revision_id: int, parent_id: int | None = None,
    proposal_id: str | None = None,
) -> bool:
    """Bind prospective evidence to its exact run, publication and policy."""
    from investment_panel.infrastructure.postgres.options_experiments import EXPERIMENT_VERSION

    payload = dict(row.get("payload") or {})
    expected = {"version": EXPERIMENT_VERSION, "paper_only": True, "run_id": row["run_id"]}
    if parent_id is not None:
        expected.update(candidate_revision_id=revision_id, parent_revision_id=parent_id, proposal_id=proposal_id)
        kind, scope, input_key = "options-paper-experiment", f"options-paper-experiment:{revision_id}", "experiment"
    else:
        expected["incumbent_revision_id"] = revision_id
        kind, scope, input_key = "options-radar", f"options-paper-incumbent:{revision_id}", "observation"
    return (
        row["strategy_revision_id"] == revision_id
        and row["run_type"] == kind and row.get("scope") == scope
        and payload.get("experiment") == expected
        and dict(payload.get("ticket") or {}).get("experiment") == expected
        and dict(row.get("metrics") or {}).get("experiment") == expected
        and dict(row.get("inputs") or {}).get(input_key) == {name: value for name, value in expected.items() if name != "run_id"}
    )


def forward_cohort(
    observations: list[dict[str, Any]], baseline: list[dict[str, Any]], proposed: list[dict[str, Any]], *,
    candidate_id: int, parent_id: int, proposal_id: str, span_days: int, execution_grade: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Score a fixed observation window, including confirmed rejections and gaps."""
    candidate = [row for row in observations if row["strategy_revision_id"] == candidate_id]
    starts = [row.get("first_paper_at") if execution_grade else row["as_of"] for row in candidate]
    dates = sorted({value.astimezone(MARKET_TZ).date() for value in starts if value is not None})
    # Exclude the initial partial session by a rule fixed before outcomes.
    # Its incumbent decision can predate the candidate's creation.
    start = dates[0] + timedelta(days=1) if dates else None
    # The first observation date after the required span closes this batch.
    # Returns never choose its boundary; new open batches cannot censor losses.
    ends = sorted({row["as_of"].astimezone(MARKET_TZ).date() for row in candidate
                   if start is not None and row["as_of"].astimezone(MARKET_TZ).date() >= start + timedelta(days=span_days)})
    end = ends[0] if ends else None
    scoped = [row for row in observations if start is not None
              and start <= row["as_of"].astimezone(MARKET_TZ).date()
              and (end is None or row["as_of"].astimezone(MARKET_TZ).date() <= end)]
    universe = {_comparison_key(row) for row in scoped}
    cash: dict[int, set[Any]] = {parent_id: set(), candidate_id: set()}
    measured: dict[int, list[dict[str, Any]]] = {parent_id: [], candidate_id: []}
    states: dict[str, int] = {}
    terminal_unknown: set[tuple[int, Any]] = set()
    multiple_paper_episodes: set[tuple[int, Any]] = set()
    for revision, source in ((parent_id, baseline), (candidate_id, proposed)):
        own = [row for row in scoped if row["strategy_revision_id"] == revision]
        by_id = {row["decision_id"]: row for row in source}
        paper = {_comparison_key(row): row for row in _independent_rows(
            [row for row in source if paper_execution_complete(row)]
        )} if execution_grade else {}
        for row in own:
            key = _comparison_key(row)
            valid = observation_lineage_matches(
                row, revision_id=revision, parent_id=parent_id if revision == candidate_id else None,
                proposal_id=proposal_id if revision == candidate_id else None,
            )
            state = str(row["shadow_status"]) if valid else "invalid_lineage"
            states[f"{revision}:{state}"] = states.get(f"{revision}:{state}", 0) + 1
            if execution_grade and has_multiple_paper_orders(row):
                # One order cannot represent the capital or unresolved exposure
                # of several executions. Keep this episode in the denominator.
                multiple_paper_episodes.add((revision, key))
                continue
            observed = paper.get(key) if execution_grade else by_id.get(row["decision_id"])
            if (valid and observed is not None
                and (execution_grade or (row["state"] in {"WATCH", "SETUP", "READY"} and state == "closed"))):
                if not execution_grade and "shadow_current_return" in observed:
                    observed = {**observed, "current_return": observed["shadow_current_return"],
                                "peak_return": observed["shadow_peak_return"], "max_drawdown": observed["shadow_max_drawdown"]}
                if all(observed.get(name) is not None and math.isfinite(float(observed[name])) for name in ("current_return", "peak_return")):
                    measured[revision].append(observed)
            elif (valid and state == "rejected" and row["state"] == "REJECTED"
                  and row["pending_entry_reason"] == "candidate_gate_rejected"
                  and (not execution_grade or (row.get("paper_order_count") == 0 and row.get("first_paper_at") is None))
                  and all(row.get(name) is None for name in ("entry_at", "entry_price", "exit_at", "exit_price"))):
                cash[revision].add(key)
            elif valid and not execution_grade and state in {"unfilled", "unmeasurable"}:
                terminal_unknown.add((revision, key))
    # A terminal collection gap cannot be cured by observing a later winner.
    # Keep this trial and its denominator; a new prospective trial is separate.
    terminal_unknown -= {(revision, key) for revision, keys in cash.items() for key in keys}
    terminal_unknown -= {(revision, _comparison_key(row)) for revision, rows in measured.items() for row in rows}
    return _independent_rows(measured[parent_id]), _independent_rows(measured[candidate_id]), {
        "comparison_keys": universe, "baseline_cash_keys": cash[parent_id], "proposed_cash_keys": cash[candidate_id],
        "window_complete": end is not None and end < datetime.now(UTC).astimezone(MARKET_TZ).date(),
        "cohort_metadata": {"period_start": start.isoformat() if start else None,
                            "period_end": end.isoformat() if end else None, "observation_states": states,
                            "multiple_paper_order_episodes": len(multiple_paper_episodes),
                            "multiple_paper_order_episode_keys": [
                                {"strategy_revision_id": revision, "episode_key": key}
                                for revision, key in sorted(multiple_paper_episodes, key=str)
                            ],
                            "terminal_unmatched_episodes": len(terminal_unknown)},
    }


def has_multiple_paper_orders(row: Mapping[str, Any]) -> bool:
    count = row.get("paper_order_count")
    return type(count) is int and count > 1


def paper_execution_complete(row: Mapping[str, Any]) -> bool:
    """Require one fully journaled order for an independent paper sample."""
    if (row.get("paper_only") is not True or row.get("paper_status") not in {"exited", "closed"}
        or type(row.get("paper_order_count")) is not int or row["paper_order_count"] != 1
        or row.get("fill_multipliers_verified") is not True):
        return False
    required = (
        row.get("paper_order_id"), row.get("filled_at"), row.get("exit_at"),
        row.get("actual_fill_price"), row.get("exit_price"), row.get("filled_quantity"),
        row.get("exited_quantity"), row.get("fees"), row.get("entry_slippage"), row.get("exit_slippage"),
    )
    if not row.get("paper_order_id") or any(value is None for value in required[1:]):
        return False
    try:
        entry = float(row["actual_fill_price"])
        exit_price = float(row["exit_price"])
        filled = float(row["filled_quantity"])
        exited = float(row["exited_quantity"])
        fees = float(row.get("fees") or 0)
        entry_slippage = float(row.get("entry_slippage") or 0)
        exit_slippage = float(row.get("exit_slippage") or 0)
        entry_quantity = float(row["entry_quantity"])
        exit_quantity = float(row["exit_quantity"])
        multiplier = float(row["contract_multiplier"])
        ordered_times = row["as_of"] < row["filled_at"] < row["exit_at"] <= datetime.now(UTC)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return (
        math.isfinite(entry) and entry > 0
        and math.isfinite(exit_price) and exit_price >= 0
        and math.isfinite(filled) and filled > 0
        and math.isfinite(exited) and exited == filled
        and entry_quantity == filled and exit_quantity == exited
        and math.isfinite(multiplier) and multiplier > 0 and ordered_times
        and math.isfinite(fees) and fees >= 0
        and math.isfinite(entry_slippage) and entry_slippage >= 0
        and math.isfinite(exit_slippage) and exit_slippage >= 0
    )


def paper_realized_return(row: Mapping[str, Any]) -> float | None:
    if not paper_execution_complete(row):
        return None
    entry = float(row["actual_fill_price"])
    exit_price = float(row["exit_price"])
    quantity = float(row["filled_quantity"])
    fees = float(row.get("fees") or 0)
    multiplier = float(row["contract_multiplier"])
    credit = str(row.get("structure")) in {"cash_secured_put", "put_credit_spread", "call_credit_spread"}
    capital = float(row.get("reserved_collateral") or 0) if credit else entry * quantity * multiplier
    if not math.isfinite(capital) or capital <= 0:
        return None
    value = ((entry - exit_price if credit else exit_price - entry) * quantity * multiplier - fees) / capital
    return value if math.isfinite(value) else None

def measured_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    measured = []
    for row in rows:
        realized = paper_realized_return(row)
        if realized is not None:
            marks = dict(row.get("paper_marks") or {})
            drawdown = marks.get("max_drawdown")
            try:
                mark_available = datetime.fromisoformat(str(marks.get("measured_at"))) <= datetime.now(UTC)
            except (TypeError, ValueError):
                mark_available = False
            observed_drawdown = (marks.get("status") == "observed" and mark_available
                                 and type(marks.get("mark_count")) is int and marks["mark_count"] >= 2
                                 and isinstance(drawdown, (float, int)) and not isinstance(drawdown, bool)
                                 and math.isfinite(drawdown) and drawdown <= 0)
            row = {**row, "shadow_current_return": row.get("current_return"),
                   "shadow_peak_return": row.get("peak_return"), "shadow_max_drawdown": row.get("max_drawdown"),
                   "current_return": realized, "peak_return": realized,
                   "max_drawdown": drawdown if observed_drawdown else None}
        if all(row.get(key) is not None and math.isfinite(float(row[key])) for key in ("current_return", "peak_return")):
            measured.append(row)
    return measured


_DEFAULT_PARAMETERS = {
    "feature_version": "option-core-v1",
    "gates": {"max_spread_pct": 0.25, "min_open_interest": 50, "min_dte": 14, "max_dte": 900},
}



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


def _comparison_key(row: Mapping[str, Any]) -> Any:
    return row.get("episode_key") or (str(row.get("ticker") or ""), str(row.get("structure") or ""), row["as_of"].astimezone(MARKET_TZ).date())



def _evaluation_period(
    cohort: Mapping[str, Any] | None, rows: list[dict[str, Any]],
) -> tuple[datetime | None, datetime | None]:
    if cohort:
        try:
            start, end = (datetime.strptime(cohort[name], "%Y-%m-%d").replace(tzinfo=MARKET_TZ)
                          for name in ("period_start", "period_end"))
        except (KeyError, TypeError, ValueError):
            return None, None
        return (start, end) if start <= end else (None, None)
    return min((row["as_of"] for row in rows), default=None), max((row["as_of"] for row in rows), default=None)


def evaluate_comparison(
    baseline: list[dict[str, Any]], proposed: list[dict[str, Any]], *,
    minimum: int, require_span_days: int = 0, paired: bool = False,
    comparison_keys: set[Any] | None = None, baseline_cash_keys: set[Any] | None = None,
    proposed_cash_keys: set[Any] | None = None, window_complete: bool = True,
    cohort_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    baseline_by_key = {_comparison_key(row): row for row in baseline}
    proposed_by_key = {_comparison_key(row): row for row in proposed}
    keys = comparison_keys if comparison_keys is not None else set(baseline_by_key) | set(proposed_by_key)
    missing_pairs = (sum(key not in baseline_by_key and key not in (baseline_cash_keys or set()) for key in keys)
                     + sum(key not in proposed_by_key and key not in (proposed_cash_keys or set()) for key in keys)) if paired else 0
    comparison_baseline = baseline
    baseline_metrics = _metrics(comparison_baseline)
    proposed_metrics = _metrics(proposed)
    period_start, period_end = _evaluation_period(cohort_metadata if paired else None, proposed)
    span_days = 0
    if period_start and period_end:
        span_days = ((period_end.date() - period_start.date()).days if paired and cohort_metadata
                     else (period_end - period_start).days)
    # A tighter filter leaves CASH on excluded opportunities. Compare returns
    # on the same original opportunity denominator, not two selected averages.
    denominator = len(keys) if paired else len(comparison_baseline)
    known = bool(denominator and not missing_pairs and window_complete)
    candidate_net = sum(float(row["current_return"]) for row in proposed) / denominator if known else None
    baseline_net = sum(float(row["current_return"]) for row in baseline) / denominator if known else None
    comparison_returns = ([float(proposed_by_key[key]["current_return"]) if key in proposed_by_key else 0.0
                           for key in keys] if known else [])
    comparison_lower = (candidate_net - 1.96 * pstdev(comparison_returns) / math.sqrt(denominator)
                        if len(comparison_returns) > 1 else None)
    if window_complete and missing_pairs and (cohort_metadata or {}).get("terminal_unmatched_episodes", 0):
        verdict = "blocked_terminal_evidence"
    elif len(proposed) < minimum or span_days < require_span_days or not known:
        verdict = "collecting_data" if require_span_days else "insufficient_data"
    elif (
        candidate_net > 0 and candidate_net >= baseline_net
        and comparison_lower is not None and comparison_lower > 0
        and proposed_metrics["false_positive_rate"] <= baseline_metrics["false_positive_rate"] + 0.02
    ):
        verdict = "pass"
    else:
        verdict = "fail"
    selected_keys = {_comparison_key(row) for row in proposed}
    winners = [row for row in comparison_baseline if float(row["current_return"]) > 0]
    return {
        "verdict": verdict, "baseline": baseline_metrics, "proposed": proposed_metrics,
        "minimum_sample": minimum, "observation_span_days": span_days,
        "scope": "paired_independent_opportunities" if paired else "retained_actionable_decisions_only",
        "comparison_denominator": denominator, "unmatched_episodes": missing_pairs,
        "candidate_net_on_comparison_universe": candidate_net,
        "baseline_net_on_comparison_universe": baseline_net,
        "comparison_lower_95": comparison_lower,
        "comparison_window_complete": window_complete,
        "cohort": cohort_metadata or {},
        "confirmed_cash": {"baseline": len(baseline_cash_keys or set()), "proposed": len(proposed_cash_keys or set())},
        "missed_winners": (
            sum(_comparison_key(row) not in selected_keys for row in winners) / len(winners)
            if winners and known else None
        ),
    }


def _phase7_metrics(evaluation: dict[str, Any], source_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Project measured learning metrics into the strict Phase 7 shape."""

    proposed = dict(evaluation.get("proposed") or {})
    returns = [
        float(row.get("current_return") if row.get("current_return") is not None else row["peak_return"])
        for row in source_rows
        if row.get("peak_return") is not None
    ]
    probabilities = [
        (float(row["probability_profit"]), float((row.get("current_return") or 0) > 0))
        for row in source_rows
        if row.get("probability_profit") is not None and row.get("current_return") is not None
    ]
    brier = mean((probability - observed) ** 2 for probability, observed in probabilities) if probabilities else None
    log_loss = (
        mean(
            -(observed * math.log(max(min(probability, 1.0 - 1e-12), 1e-12))
              + (1.0 - observed) * math.log(max(min(1.0 - probability, 1.0 - 1e-12), 1e-12)))
            for probability, observed in probabilities
        )
        if probabilities else None
    )
    values = {
        "calibration": proposed.get("calibration_error"),
        "brier": brier,
        "log_loss": log_loss,
        "precision_at_top_k": proposed.get("precision_at_5"),
        "net_pnl_after_modeled_costs": evaluation.get("candidate_net_on_comparison_universe"),
        "net_pnl_after_realized_costs": None,
        "drawdown": proposed.get("max_drawdown"),
        "tail_loss": min(returns) if returns else None,
        # No transaction, capacity, or execution tape is present in this
        # evaluator. Explicitly retain these dimensions as unavailable.
        "turnover": None,
        "slippage": None,
        "capacity": None,
        "regime_performance": {
            str(regime): mean(float(row["current_return"]) for row in source_rows if row.get("market_regime") == regime)
            for regime in {row.get("market_regime") for row in source_rows if row.get("market_regime") not in (None, "", "unknown")}
        } or None,
        "false_positives": proposed.get("false_positive_rate"),
        "missed_winners": evaluation.get("missed_winners"),
    }
    if source_rows and all(paper_execution_complete(row) for row in source_rows):
        realized_returns = [paper_realized_return(row) for row in source_rows]
        realized_returns = [value for value in realized_returns if value is not None]
        if len(realized_returns) == len(source_rows):
            values.update({
                "net_pnl_after_realized_costs": mean(realized_returns),
                "turnover": mean(
                    (float(row["actual_fill_price"]) + float(row["exit_price"]))
                    * float(row["filled_quantity"]) * float(row["contract_multiplier"])
                    for row in source_rows
                ),
                "slippage": mean(
                    abs(float(row.get("entry_slippage") or 0))
                    + abs(float(row.get("exit_slippage") or 0))
                    for row in source_rows
                ),
                "capacity": min(float(row["filled_quantity"]) for row in source_rows),
            })
    return {
        **{name: values.get(name) for name in TRACKED_METRICS},
        # Preserve the measured comparison details for non-authoritative
        # historical readers while the Phase 7 fields above remain canonical.
        "baseline": evaluation.get("baseline") or {},
        "proposed": proposed,
        "observation_span_days": evaluation.get("observation_span_days", 0),
        "comparison_denominator": evaluation.get("comparison_denominator"),
        "candidate_net_on_comparison_universe": evaluation.get("candidate_net_on_comparison_universe"),
        "baseline_net_on_comparison_universe": evaluation.get("baseline_net_on_comparison_universe"),
        "comparison_lower_95": evaluation.get("comparison_lower_95"),
        "unmatched_episodes": evaluation.get("unmatched_episodes"),
        "comparison_window_complete": evaluation.get("comparison_window_complete"),
        "cohort": evaluation.get("cohort") or {},
        "confirmed_cash": evaluation.get("confirmed_cash") or {},
    }


def _phase7_evidence(
    evaluation: dict[str, Any], source_rows: list[dict[str, Any]], *, execution_grade: bool = False,
    candidate_revision_id: int | None = None,
) -> dict[str, Any]:
    proposed = dict(evaluation.get("proposed") or {})
    sample_size = proposed.get("sample_size")
    lower = evaluation.get("comparison_lower_95")
    uncertainty: dict[str, Any] = {"lower_95_expectancy": lower}
    if lower is not None:
        uncertainty["upper_95_expectancy"] = 2 * evaluation["candidate_net_on_comparison_universe"] - lower
    evidence = {
        "source": "analysis.option_outcome",
        "method": "retained_actionable_decisions_forward_evaluation",
        "version": "phase7-governance-evidence-v1",
        "options_comparison_version": OPTIONS_COMPARISON_VERSION,
        "sample_size": sample_size,
        "sample_definition": evaluation.get("scope", "retained_actionable_decisions_only"),
        "uncertainty": uncertainty,
        "actionable_only": True,
    }
    if execution_grade and source_rows and all(paper_execution_complete(row) for row in source_rows):
        evidence["paper_execution"] = {
            "source": "app.paper_order",
            "paper_only": True,
            "sample_size": len(source_rows),
            "completed_orders": len(source_rows),
            "strategy_revision_id": candidate_revision_id,
            "paper_order_ids": [row["paper_order_id"] for row in source_rows],
            "decision_ids": [row["decision_id"] for row in source_rows],
            "database_verified": True,
        }
    return evidence


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    peaks = [float(row["peak_return"]) for row in rows]
    count = len(peaks)
    returns = [float(row.get("current_return") if row.get("current_return") is not None else row["peak_return"]) for row in rows]
    net_expectancy = mean(returns) if returns else 0.0
    lower_bound = net_expectancy - 1.96 * pstdev(returns) / math.sqrt(len(returns)) if len(returns) > 1 else 0.0
    calibration_rows = [row for row in rows if row.get("probability_profit") is not None]
    calibration_error = mean(
        abs(float(row["probability_profit"]) - float((row.get("current_return") or 0) > 0))
        for row in calibration_rows
    ) if calibration_rows else None
    ranked_days: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("score") is not None and math.isfinite(float(row["score"])):
            ranked_days.setdefault(row["as_of"].astimezone(MARKET_TZ).date(), []).append(row)
    top_k = [row for day in ranked_days.values() for row in sorted(
        day, key=lambda item: (-float(item["score"]), str(item.get("decision_id") or "")),
    )[:5]]
    ticker_counts: dict[str, int] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "")
        ticker_counts[ticker] = ticker_counts.get(ticker, 0) + 1
    return {
        "sample_size": count,
        "hit_rate_2x": sum(value >= 1 for value in peaks) / count if count else 0.0,
        "hit_rate_5x": sum(value >= 4 for value in peaks) / count if count else 0.0,
        "hit_rate_10x": sum(value >= 9 for value in peaks) / count if count else 0.0,
        "false_positive_rate": sum(value <= 0 for value in returns) / count if count else 0.0,
        "precision_at_5": mean(float(row["current_return"]) > 0 for row in top_k) if top_k else None,
        "net_expectancy": net_expectancy,
        "lower_95_expectancy": lower_bound,
        "max_drawdown": min(float(row["max_drawdown"]) for row in rows) if rows and all(row.get("max_drawdown") is not None for row in rows) else None,
        "calibration_error": calibration_error,
        "max_ticker_contribution": max(ticker_counts.values(), default=0) / count if count else 1.0,
    }


def _proposal_status(backtest: str, forward: str, execution_grade: str = "unavailable") -> str:
    if backtest == "fail":
        return "backtest_failed"
    if backtest != "pass":
        return "backtest_required"
    if forward == "blocked_terminal_evidence" or execution_grade == "blocked_terminal_evidence":
        return "blocked_terminal_evidence"
    if forward == "fail":
        return "forward_test_failed"
    if forward != "pass":
        return "forward_test_required"
    if execution_grade != "pass":
        return "execution_grade_paper_required"
    return "ready"
