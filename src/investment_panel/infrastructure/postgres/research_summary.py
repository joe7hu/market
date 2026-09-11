"""Small, read-only research results drawn from stored strategy evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from typing import Any

from investment_panel.domain.research.stock_alpha import TARGET_VERSION
from investment_panel.settings import AppConfig
from investment_panel.domain.decision import next_action_for
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.infrastructure.postgres.user_state import portfolio_rows

OPTIONS_COMPARISON_VERSION = "options-independent-comparison-v1"
OPTIONS_EVIDENCE_BASIS = {
    "walk_forward": "independent_options_replay",
    "shadow": "independent_options_shadow",
    "execution_grade_paper": "independent_options_paper",
}


def research_summary(runtime: DatabaseRuntime, config: AppConfig) -> dict[str, Any]:
    with runtime.snapshot() as connection:
        strategies = [dict(row) for row in connection.execute("""
            WITH latest_research AS (
                SELECT DISTINCT ON (authority_group) id
                FROM analysis.strategy_revision
                WHERE status <> 'active' AND created_at <= now()
                ORDER BY authority_group, created_at DESC, id DESC
            ), selected AS (
                SELECT revision.id, revision.strategy_key, revision.revision,
                       revision.name, revision.status, revision.authority_group,
                       revision.experiment_family_id, revision.promoted_at,
                       coalesce(hypothesis.statement, revision.economic_mechanism) AS hypothesis,
                       count(*) OVER () AS total_count,
                       (SELECT max(changed.promoted_at) FROM analysis.strategy_revision changed
                        WHERE changed.authority_group = revision.authority_group
                          AND changed.promoted_at <= now()) AS last_policy_change
                FROM analysis.strategy_revision revision
                LEFT JOIN analysis.hypothesis hypothesis ON hypothesis.id = revision.hypothesis_id
                    AND hypothesis.available_at <= now()
                WHERE revision.created_at <= now()
                  AND (revision.status = 'active' OR revision.id IN (SELECT id FROM latest_research))
            ) SELECT * FROM selected ORDER BY (status = 'active') DESC, authority_group, id DESC LIMIT 12
        """).fetchall()]
        ids = [row["id"] for row in strategies]
        evaluations = [dict(row) for row in connection.execute("""
            SELECT latest.* FROM unnest(%s::bigint[]) revision(id)
            CROSS JOIN LATERAL (
                SELECT ranked.*
                  FROM (
                    SELECT evaluation.id AS evaluation_id, evaluation.strategy_revision_id,
                           evaluation.evaluation_type, evaluation.verdict, evaluation.evaluated_at,
                           evaluation.available_at, evaluation.period_start, evaluation.period_end,
                           evaluation.metrics, evaluation.evidence, evaluation.input_hash,
                           evaluation.output_hash,
                           evaluation.run_id, evaluation.scope, evaluation.mode, evaluation.lineage,
                           row_number() OVER (
                               PARTITION BY evaluation.evaluation_type
                               ORDER BY evaluation.evaluated_at DESC, evaluation.id DESC
                           ) AS stage_rank
                      FROM analysis.strategy_evaluation evaluation
                      LEFT JOIN analysis.run run ON run.id = evaluation.run_id
                     WHERE evaluation.strategy_revision_id = revision.id
                       AND evaluation.evaluated_at <= now() AND evaluation.available_at <= now()
                       AND evaluation.mode IS DISTINCT FROM 'replay'
                       AND evaluation.lineage->>'mode' IS DISTINCT FROM 'replay'
                       AND (
                           evaluation.evaluation_type <> 'strategy_signal'
                           OR (evaluation.run_id IS NOT NULL AND run.status = 'succeeded')
                       )
                       AND (evaluation.run_id IS NULL OR run.status = 'succeeded')
                  ) ranked
                 WHERE ranked.stage_rank <= 8
                 ORDER BY ranked.evaluated_at DESC, ranked.evaluation_id DESC
            ) latest
        """, [ids]).fetchall()]
        trials = [dict(row) for row in connection.execute("""
            SELECT revision.id AS strategy_revision_id, trial.status, trial.failure_reason,
                   trial.finished_at, result.outcome->'gates' AS gates,
                   manifest.expected_member_count,
                   CASE WHEN manifest.research_trial_id IS NOT NULL THEN universe.included END AS included,
                   CASE WHEN manifest.research_trial_id IS NOT NULL THEN universe.excluded END AS excluded
            FROM analysis.strategy_revision revision
            LEFT JOIN LATERAL (
                SELECT * FROM analysis.research_trial
                WHERE experiment_family_id = revision.experiment_family_id
                  AND input_cutoff <= now() AND available_at <= now()
                ORDER BY input_cutoff DESC, id DESC LIMIT 1
            ) trial ON true
            LEFT JOIN LATERAL (
                SELECT outcome FROM analysis.trial_result
                WHERE research_trial_id = trial.id AND result_kind = 'validation'
                  AND observed_at <= now() AND available_at <= now()
                ORDER BY result_version DESC, observed_at DESC LIMIT 1
            ) result ON true
            LEFT JOIN analysis.trial_universe_manifest manifest ON manifest.research_trial_id = trial.id
                AND manifest.available_at <= now() AND manifest.cutoff <= now()
            LEFT JOIN LATERAL (
                SELECT count(*) FILTER (WHERE eligible) AS included,
                       count(*) FILTER (WHERE NOT eligible) AS excluded
                FROM analysis.universe_observation
                WHERE research_trial_id = trial.id AND available_at <= now() AND observed_at <= now()
            ) universe ON true
            WHERE revision.id = ANY(%s::bigint[])
        """, [ids]).fetchall()]
        reviews = dict(connection.execute("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE user_state = 'acknowledged') AS acknowledged,
                   count(*) FILTER (WHERE reviewed_at IS NOT NULL) AS completed,
                   count(*) FILTER (WHERE useful IS NOT NULL) AS rated,
                   count(*) FILTER (WHERE useful IS TRUE) AS helpful,
                   count(*) FILTER (WHERE useful IS FALSE) AS not_helpful
            FROM app.decision_inbox_item
            WHERE created_at >= now() - interval '30 days' AND created_at <= now()
        """).fetchone())
        ideas = [dict(row) for row in connection.execute("""
            WITH latest AS MATERIALIZED (
                SELECT DISTINCT ON (decision.instrument_id) decision.id,
                       instrument.symbol AS ticker, position.instrument_id IS NOT NULL AS owned,
                       decision.input_manifest->'opportunity_rank'->>'research_rank' AS research_rank
                FROM analysis.ticker_decision decision
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                LEFT JOIN app.portfolio_position position ON position.instrument_id = instrument.id
                    AND position.quantity > 0
                WHERE decision.status = 'published' AND decision.as_of <= now() AND decision.published_at <= now()
                  AND (position.instrument_id IS NOT NULL OR EXISTS (
                    SELECT 1 FROM app.watchlist_item watchlist WHERE watchlist.instrument_id = instrument.id
                    AND watchlist.watch_state <> 'excluded' AND watchlist.created_at <= now()))
                ORDER BY decision.instrument_id, decision.as_of DESC, decision.published_at DESC, decision.id DESC
            ), chosen AS (
                SELECT * FROM latest
                ORDER BY owned DESC,
                    CASE WHEN research_rank ~ '^[1-9][0-9]*$' AND pg_input_is_valid(research_rank, 'integer') THEN research_rank::integer END NULLS LAST,
                    ticker LIMIT 3
            ) SELECT chosen.ticker, chosen.owned, decision.as_of,
                left(coalesce(nullif(decision.input_manifest #>> '{inputs,theses,0,thesis_json,core_thesis}', ''),
                    decision.fundamental->'evidence_for'->0->>'statement'), 2000) AS thesis,
                left(coalesce(nullif(decision.input_manifest #>> '{inputs,theses,0,thesis_json,scenarios,bear,rationale}', ''),
                    CASE WHEN decision.fundamental->'evidence_against'->0->>'statement'
                         NOT LIKE 'Unvalidated thesis condition: %'
                         THEN decision.fundamental->'evidence_against'->0->>'statement' END), 2000) AS countercase,
                coalesce(decision.fundamental->'invalidation'->>'statement',
                    decision.input_manifest #>> '{inputs,theses,0,thesis_json,invalidation_rules,0,text}') AS invalidation,
                coalesce(nullif(nullif(decision.capital_action->>'catalyst', ''),
                    'next decision catalyst or confirmed price update'),
                    decision.input_manifest #>> '{inputs,theses,0,thesis_json,catalysts,0,title}') AS catalyst,
                decision.resolution->'blockers' AS blockers
            FROM chosen JOIN analysis.ticker_decision decision ON decision.id = chosen.id
            ORDER BY chosen.owned DESC,
                CASE WHEN chosen.research_rank ~ '^[1-9][0-9]*$' AND pg_input_is_valid(chosen.research_rank, 'integer') THEN chosen.research_rank::integer END NULLS LAST,
                chosen.ticker
        """).fetchall()]
        holdings = portfolio_rows(config, connection=connection) if ideas else []

    by_id = {row["strategy_revision_id"]: row for row in trials}
    output = []
    automatic = config.analysis.options_decision_system.strategy_auto_promotion_enabled
    for row in strategies:
        stages = [evaluation_summary(item) for item in evaluations if item["strategy_revision_id"] == row["id"]]
        trial = by_id.get(row["id"], {})
        failed = list(dict.fromkeys([
            *(gate for stage in stages for gate in stage["failed_gates"]),
            *[key for key, value in _mapping(trial.get("gates")).items() if _mapping(value).get("passed") is False],
        ]))
        if not failed and trial.get("status") in {"failed", "rejected"}:
            failed = [f"trial_{trial['status']}"]
        output.append({
            "strategy_revision_id": row["id"], "strategy_key": row["strategy_key"], "revision": row["revision"],
            "name": row["name"], "status": row["status"], "hypothesis": row["hypothesis"],
            "last_policy_change": row["last_policy_change"],
            "automatic_paper_tuning": bool(automatic and row["authority_group"] in {"options-radar-core", "ticker-stock-alpha"}),
            "evaluations": _bounded_stage_evaluations(stages), "failed_gates": failed[:8],
            "trial_status": trial.get("status"), "included_count": trial.get("included"),
            "excluded_count": trial.get("excluded"), "expected_count": trial.get("expected_member_count"),
            "next_observation": (
                "This trial has a terminal evidence gap. Preserve its results; repair collection before starting a new prospective trial."
                if any(stage["verdict"] == "blocked_terminal_evidence" for stage in stages)
                else "Rerun with stock counterfactual outcomes; the previous target cannot qualify this stock model."
                if any(stage["evidence_basis"] == "obsolete_stock_target" for stage in stages)
                else "Collect independent episodes with matured outcomes and rerun the failed gates."
                if failed else "Collect forward shadow and completed paper outcomes before any promotion."
                if stages else "Run the offline evaluation before collecting candidate paper evidence."
            ),
        })
    weights = {row["symbol"]: row.get("portfolio_weight") for row in holdings} if all(row.get("price") is not None for row in holdings) else {}
    for row in ideas:
        blockers = row.pop("blockers") or []
        blocker = next((reason for reason in blockers if reason not in {"cash_comparator", "cash_selected"}), "cash_comparator")
        row["next_action"] = next_action_for(blocker)
        row["holding_weight_pct"] = weights.get(row["ticker"])
    return {
        "as_of": datetime.now(UTC), "paper_only": True, "strategies": output,
        "strategy_count": strategies[0]["total_count"] if strategies else 0,
        "ideas": ideas, "review_activity": {**reviews, "window_days": 30,
            "helpful_rate": reviews["helpful"] / reviews["rated"] if reviews["rated"] else None},
    }


def evaluation_summary(row: dict[str, Any]) -> dict[str, Any]:
    metrics, evidence = _mapping(row.get("metrics")), _evidence(row.get("evidence"))
    lineage = _mapping(row.get("lineage"))
    validation = _mapping(metrics.get("validation"))
    stage = str(row["evaluation_type"])
    stock = stage == "out_of_sample" and evidence.get("walk_forward") is True
    independent_stock = stock and metrics.get("target_version") == TARGET_VERSION and evidence.get("purge_embargo") is True
    independent_options = stage in OPTIONS_EVIDENCE_BASIS and evidence.get("options_comparison_version") == OPTIONS_COMPARISON_VERSION
    sample, lower, brier, denominator, unknown, window_complete = None, None, None, None, None, None
    basis = "obsolete_stock_target" if stock and metrics.get("target_version") != TARGET_VERSION else "independence_unconfirmed"
    if independent_stock:
        basis = "independent_stock_episodes"
        # Calibration training size is not the denominator of these OOS
        # returns and probability errors. Older missing counts stay unknown.
        sample = _count(metrics.get("oos_sample_size"))
        if sample:
            lower = _number(metrics.get("lower_confidence_net_utility_after_costs"))
            brier = _number(_mapping(metrics.get("calibration_metrics")).get("brier_score"))
    elif independent_options:
        basis = OPTIONS_EVIDENCE_BASIS[stage]
        sample = _count(_mapping(metrics.get("proposed")).get("sample_size"))
        denominator = _count(metrics.get("comparison_denominator"))
        unknown = _count(metrics.get("unmatched_episodes"))
        window_complete = metrics.get("comparison_window_complete") if type(metrics.get("comparison_window_complete")) is bool else None
        if sample:
            brier = _number(metrics.get("brier"))
            if window_complete is True and unknown == 0 and denominator is not None and denominator >= sample:
                lower = _number(metrics.get("comparison_lower_95"))
    brier = brier if brier is not None and 0 <= brier <= 1 else None
    failed = [name for name, gate in _mapping(validation.get("gates")).items() if _mapping(gate).get("passed") is False]
    verdict = str(row.get("verdict") or "unavailable")
    if stage == "strategy_signal":
        if verdict in {"unavailable", "blocked"} and not failed:
            failed = [f"{stage}: {verdict}"]
    elif verdict in {"blocked", "blocked_terminal_evidence", "failed", "rejected", "unavailable"} and not failed:
        failed = [f"{row['evaluation_type']}: {verdict}"]
    return {
        "stage": stage, "verdict": verdict, "evaluated_at": row.get("evaluated_at"),
        "evaluation_id": str(row["evaluation_id"]) if row.get("evaluation_id") is not None else None,
        "run_id": str(row["run_id"]) if row.get("run_id") is not None else None,
        "scope": row.get("scope") or lineage.get("scope"), "mode": row.get("mode") or lineage.get("mode"),
        "available_at": row.get("available_at"), "input_hash": row.get("input_hash"),
        "output_hash": row.get("output_hash"),
        "period_start": row.get("period_start"), "period_end": row.get("period_end"),
        "actionability": metrics.get("actionability") if stage == "strategy_signal" else None,
        "signal_value": _number(metrics.get("value")) if stage == "strategy_signal" else None,
        "signal_direction": metrics.get("direction") if stage == "strategy_signal" and isinstance(metrics.get("direction"), str) else None,
        "independent_sample_count": sample,
        "net_return_lower_bound": lower,
        "brier_score": brier,
        "evidence_basis": basis,
        "comparison_denominator": denominator, "unmatched_episodes": unknown,
        "comparison_window_complete": window_complete,
        "failed_gates": failed[:8], "blockers": [str(item) for item in lineage.get("blockers", ()) if str(item).strip()],
        "evidence": evidence,
    }


def _bounded_stage_evaluations(stages: list[dict[str, Any]], per_stage: int = 8) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    bounded: list[dict[str, Any]] = []
    for stage in stages:
        key = str(stage.get("stage") or "unknown")
        if counts.get(key, 0) >= per_stage:
            continue
        counts[key] = counts.get(key, 0) + 1
        bounded.append(stage)
    return bounded


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _evidence(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"items": value}
    return {}


def _count(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 and number.is_integer() else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None
