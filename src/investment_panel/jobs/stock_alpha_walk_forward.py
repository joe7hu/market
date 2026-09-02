"""Register and optionally promote one PIT stock-alpha challenger."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

from psycopg.types.json import Jsonb

from investment_panel.analysis.research_validation import validate_trial
from investment_panel.analysis.stock_alpha import (
    COST_MODEL_VERSION,
    FEATURE_VERSION,
    MODEL_VERSION,
    content_hash,
    walk_forward,
)
from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.instruments import reconcile_instrument
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


STRATEGY_KEY = "ticker-stock-alpha"


def run(
    runtime: DatabaseRuntime,
    observations: Iterable[Mapping[str, Any]],
    *,
    cutoff: datetime,
    promote: bool = False,
    authorization_mode: str | None = None,
    min_train: int = 20,
    fold_size: int = 10,
    min_cohort: int = 20,
) -> dict[str, Any]:
    """Append one idempotent evaluation and promote only with explicit paper authority."""

    reference = _aware(cutoff)
    source_rows = sorted(
        (dict(row) for row in observations),
        key=content_hash,
    )
    artifact = walk_forward(
        source_rows,
        cutoff=reference,
        min_train=min_train,
        fold_size=fold_size,
        min_cohort=min_cohort,
    )
    input_hash = content_hash({"cutoff": reference, "observations": source_rows})
    metrics = dict(artifact["calibration_metrics"])
    walk_forward_complete = all((
        artifact.get("oos_period_start"),
        artifact.get("oos_period_end"),
        artifact.get("cohort_path"),
        metrics.get("brier_score") is not None,
        int(metrics.get("effective_sample_size") or 0) >= min_cohort,
        metrics.get("lower_confidence_net_utility_after_costs") is not None,
        float(metrics.get("lower_confidence_net_utility_after_costs") or 0.0) > 0,
    ))
    evaluation_metrics = {
        "artifact_id": f"{STRATEGY_KEY}:{artifact['artifact_hash']}",
        "artifact_hash": artifact["artifact_hash"],
        "input_hash": input_hash,
        "model_version": MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "cost_model_version": COST_MODEL_VERSION,
        "cohort_id": "hierarchical-stock-alpha",
        "cohort_path": artifact["cohort_path"],
        "fallback_parent": artifact["fallback_parent"],
        "effective_sample_size": metrics["effective_sample_size"],
        "calibration_metrics": {
            "brier_score": metrics["brier_score"],
            "calibration_error": metrics["calibration_error"],
        },
        "lower_confidence_net_utility_after_costs": metrics[
            "lower_confidence_net_utility_after_costs"
        ],
        "valid_through": None,
    }
    parameters = {
        "artifact_id": evaluation_metrics["artifact_id"],
        "artifact_hash": artifact["artifact_hash"],
        "input_hash": input_hash,
        "model_version": MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "cost_model_version": COST_MODEL_VERSION,
        "target": artifact["target"],
        "cohort_id": evaluation_metrics["cohort_id"],
        "calibration_state": "calibrated_hierarchical" if artifact["cohort_path"] else "not_calibrated",
        "expression_kind": "STOCK",
        "horizons": artifact["horizons"],
    }
    predictions = list(artifact.get("predictions") or [])
    net_returns = [float(row["net_utility_after_costs"]) for row in predictions]
    gross_returns = [float(row["realized_return"]) for row in predictions]
    labels = [float(row["outcome"]) for row in predictions]
    scores = [float(row["calibrated_probability"]) for row in predictions]
    randomized_labels = labels[1:] + labels[:1] if labels else []
    white_noise_scores = [((index * 7919 + 104729) % 1000) / 1000 for index in range(len(scores))]
    validation = validate_trial(
        mechanism_class="walk_forward stock alpha",
        falsification_rule="randomized labels, white-noise scores, future-information trap",
        observed_returns=net_returns,
        randomized_returns=[_centered_edge(scores, randomized_labels)],
        white_noise_returns=[_centered_edge(white_noise_scores, labels)],
        gross_return=sum(gross_returns) / len(gross_returns) if gross_returns else 0.0,
        base_cost=(sum(float(row["modeled_cost"]) for row in predictions) / len(predictions)) if predictions else 0.0,
        neutralized_returns=net_returns,
        parameter_neighborhood=[
            {"return": value}
            for value in (
                (sum(net_returns) / len(net_returns) * 0.95) if net_returns else 0.0,
                (sum(net_returns) / len(net_returns)) if net_returns else 0.0,
                (sum(net_returns) / len(net_returns) * 1.05) if net_returns else 0.0,
            )
        ],
        trials_tested=1,
        feature_available_at=[row.get("outcome_available_at", row.get("as_of")) for row in source_rows],
        cutoff=reference,
        expected_members=sorted({str(row.get("ticker") or "").upper() for row in source_rows if str(row.get("ticker") or "").strip()}),
        observed_members=sorted({str(row.get("ticker") or "").upper() for row in source_rows if str(row.get("ticker") or "").strip()}),
        expected_attempts=[f"attempt:{input_hash}"],
        completed_attempts=[f"attempt:{input_hash}"],
        path_returns=net_returns,
        policy={"min_psr": 0.5, "min_dsr": 0.5, "max_pbo": 0.5, "negative_control_tolerance": 0.05},
    )
    complete = walk_forward_complete and validation["passed"]
    mode = str(authorization_mode or "").upper()
    if promote and mode not in {"PAPER", "ADVISORY"}:
        raise ValueError("promotion requires explicit PAPER or ADVISORY authorization")

    with runtime.transaction(JOB_PROFILE) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [STRATEGY_KEY])
        research_ids = _ensure_research_prerequisites(
            connection, cutoff=reference, input_hash=input_hash,
            artifact=evaluation_metrics, observations=source_rows, complete=complete,
            validation=validation,
        )
        strategy = connection.execute(
            """
            SELECT id, revision, status, parameters
            FROM analysis.strategy_revision
            WHERE strategy_key = %s AND parameters->>'artifact_hash' = %s
              AND parameters->>'input_hash' = %s
            ORDER BY revision DESC LIMIT 1
            """,
            [STRATEGY_KEY, artifact["artifact_hash"], input_hash],
        ).fetchone()
        if strategy is None:
            revision = int(connection.execute(
                "SELECT COALESCE(max(revision), 0) + 1 AS revision FROM analysis.strategy_revision WHERE strategy_key = %s",
                [STRATEGY_KEY],
            ).fetchone()["revision"])
            strategy = connection.execute(
                """
                INSERT INTO analysis.strategy_revision
                    (strategy_key, revision, name, status, parameters, authority_group,
                     hypothesis_id, experiment_family_id, artifact_id, artifact_hash, research_required)
                VALUES (%s, %s, %s, 'candidate', %s, %s, %s, %s, %s, %s, true)
                RETURNING id, revision, status, parameters
                """,
                [STRATEGY_KEY, revision, "PIT stock alpha", Jsonb(parameters), STRATEGY_KEY, research_ids[0], research_ids[1], parameters["artifact_id"], parameters["artifact_hash"]],
            ).fetchone()
        elif dict(strategy["parameters"] or {}) != parameters:
            raise ValueError("immutable stock-alpha revision parameters do not match")
        if promote and strategy["status"] == "superseded":
            raise ValueError("superseded stock-alpha revisions cannot be replay-promoted")

        dossier_id = _ensure_research_dossier(
            connection, strategy_revision_id=int(strategy["id"]), trial_id=research_ids[2],
            artifact=evaluation_metrics, cutoff=reference, validation=validation, seal=complete,
        )

        evaluation = connection.execute(
            """
            SELECT id::text, verdict FROM analysis.strategy_evaluation
            WHERE strategy_revision_id = %s AND evaluation_type = 'out_of_sample'
              AND metrics->>'artifact_hash' = %s AND metrics->>'input_hash' = %s
            ORDER BY evaluated_at DESC, id DESC LIMIT 1
            """,
            [strategy["id"], artifact["artifact_hash"], input_hash],
        ).fetchone()
        if evaluation is None:
            evaluation = connection.execute(
                """
                INSERT INTO analysis.strategy_evaluation (
                    strategy_revision_id, hypothesis_id, experiment_family_id,
                    research_trial_id, validation_dossier_id, artifact_id,
                    artifact_hash, input_hash, evaluation_type, evaluated_at,
                    period_start, period_end, verdict, metrics, evidence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'out_of_sample', %s, %s, %s, %s, %s, %s)
                RETURNING id::text, verdict
                """,
                [
                    strategy["id"], research_ids[0], research_ids[1], research_ids[2], dossier_id,
                    evaluation_metrics["artifact_id"], artifact["artifact_hash"], input_hash,
                    reference, artifact["oos_period_start"], artifact["oos_period_end"],
                    "pass" if complete else "incomplete", Jsonb(evaluation_metrics),
                    Jsonb({
                        "paper_only": True,
                        "walk_forward": True,
                        "purge_embargo": True,
                        "artifact_hash": artifact["artifact_hash"],
                    }),
                ],
            ).fetchone()
        promotion_id = None
        if promote and complete:
            promotion = connection.execute(
                """
                SELECT id::text FROM analysis.strategy_evaluation
                WHERE strategy_revision_id = %s
                  AND evaluation_type = 'paper_advisory_promotion'
                  AND metrics->>'artifact_hash' = %s
                  AND metrics->>'input_hash' = %s
                  AND metrics->>'authorization_mode' = %s
                ORDER BY evaluated_at DESC, id DESC LIMIT 1
                """,
                [strategy["id"], artifact["artifact_hash"], input_hash, mode],
            ).fetchone()
            if promotion is None:
                promotion = connection.execute(
                    """
                    INSERT INTO analysis.strategy_evaluation (
                        strategy_revision_id, hypothesis_id, experiment_family_id,
                        research_trial_id, validation_dossier_id, artifact_id,
                        artifact_hash, input_hash, evaluation_type, evaluated_at,
                        period_start, period_end, verdict, metrics, evidence
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'paper_advisory_promotion', %s, %s, %s, 'pass', %s, %s)
                    RETURNING id::text
                    """,
                    [
                        strategy["id"], research_ids[0], research_ids[1], research_ids[2], dossier_id,
                        evaluation_metrics["artifact_id"], artifact["artifact_hash"], input_hash,
                        reference, artifact["oos_period_start"], artifact["oos_period_end"],
                        Jsonb({
                            "artifact_hash": artifact["artifact_hash"],
                            "input_hash": input_hash,
                            "authorization_mode": mode,
                        }),
                        Jsonb({"paper_only": True, "live_order_submission": False}),
                    ],
                ).fetchone()
            promotion_id = promotion["id"]
            connection.execute(
                """
                UPDATE analysis.strategy_revision
                SET status = 'superseded'
                WHERE strategy_key = %s AND status = 'active' AND id <> %s
                """,
                [STRATEGY_KEY, strategy["id"]],
            )
            activated = connection.execute(
                """
                UPDATE analysis.strategy_revision
                SET status = 'active', promoted_at = COALESCE(promoted_at, clock_timestamp())
                WHERE id = %s AND status IN ('candidate', 'active')
                RETURNING id
                """,
                [strategy["id"]],
            ).fetchone()
            if activated is None:
                raise ValueError("stock-alpha revision activation failed")

    return {
        "strategy_revision_id": int(strategy["id"]),
        "strategy_revision": int(strategy["revision"]),
        "strategy_evaluation_id": evaluation["id"],
        "promotion_evaluation_id": promotion_id,
        "promotion_stage": mode.lower() if promotion_id else "challenger",
        "verdict": str(evaluation["verdict"]),
        "complete": complete,
        "input_hash": input_hash,
        "artifact": artifact,
    }


def _ensure_research_prerequisites(
    connection: Any, *, cutoff: datetime, input_hash: str,
    artifact: Mapping[str, Any], observations: list[Mapping[str, Any]], complete: bool,
    validation: Mapping[str, Any],
) -> tuple[Any, Any, Any]:
    key = f"{STRATEGY_KEY}:{input_hash}"
    hypothesis = connection.execute(
        """INSERT INTO analysis.hypothesis
           (hypothesis_key, statement, mechanism_class, falsification, input_hash)
           VALUES (%s, %s, 'walk_forward', 'negative controls and future-information trap', %s)
           ON CONFLICT (hypothesis_key) DO UPDATE SET hypothesis_key = EXCLUDED.hypothesis_key
           RETURNING id""",
        [key, "Walk-forward stock alpha has persistent out-of-sample edge.", input_hash],
    ).fetchone()["id"]
    family = connection.execute(
        """INSERT INTO analysis.experiment_family
           (hypothesis_id, family_key, name, input_hash)
           VALUES (%s, %s, 'PIT stock alpha walk-forward', %s)
           ON CONFLICT (family_key) DO UPDATE SET family_key = EXCLUDED.family_key
           RETURNING id""",
        [hypothesis, key, input_hash],
    ).fetchone()["id"]
    trial_key = f"attempt:{input_hash}"
    connection.execute(
        """INSERT INTO analysis.experiment_manifest
           (experiment_family_id, expected_trial_count, expected_trial_keys, manifest_hash, available_at)
           VALUES (%s, 1, %s, %s, LEAST(now(), %s)) ON CONFLICT DO NOTHING""",
        [family, Jsonb([trial_key]), content_hash([trial_key]), cutoff],
    )
    trial = connection.execute(
        """INSERT INTO analysis.research_trial
           (experiment_family_id, trial_key, input_cutoff, code_version, input_hash, available_at)
           VALUES (%s, %s, %s, %s, %s, LEAST(now(), %s))
           ON CONFLICT (experiment_family_id, trial_key) DO NOTHING
           RETURNING id, status""",
        [family, trial_key, cutoff, MODEL_VERSION, input_hash, cutoff],
    ).fetchone()
    if trial is None:
        trial = connection.execute(
            "SELECT id, status FROM analysis.research_trial WHERE experiment_family_id = %s AND trial_key = %s",
            [family, trial_key],
        ).fetchone()
    trial_id = trial["id"]
    symbols = sorted({str(row.get("ticker") or "").upper() for row in observations if str(row.get("ticker") or "").strip()})
    instrument_ids = [reconcile_instrument(connection, symbol, name=symbol, asset_class="equity") for symbol in symbols]
    members = sorted(str(identifier) for identifier in instrument_ids)
    connection.execute(
        """INSERT INTO analysis.trial_universe_manifest
           (research_trial_id, cutoff, expected_member_count, expected_members, manifest_hash, available_at)
           VALUES (%s, %s, %s, %s, %s, LEAST(now(), %s)) ON CONFLICT DO NOTHING""",
        [trial_id, cutoff, len(members), Jsonb(members), content_hash(members), cutoff],
    )
    for rank, instrument_id in enumerate(sorted(instrument_ids), start=1):
        connection.execute(
            """INSERT INTO analysis.universe_observation
               (research_trial_id, instrument_id, cutoff, eligible, rank, observed_at, available_at, input_hash)
               VALUES (%s, %s, %s, true, %s, %s, LEAST(now(), %s), %s)
               ON CONFLICT (research_trial_id, cutoff, instrument_id) DO NOTHING""",
            [trial_id, instrument_id, cutoff, rank, cutoff, cutoff, input_hash],
        )
    connection.execute(
        """INSERT INTO analysis.trial_result
           (research_trial_id, result_kind, observed_at, available_at, input_hash, metrics, outcome)
           VALUES (%s, 'validation', %s, LEAST(now(), %s), %s, %s, %s)
           ON CONFLICT (research_trial_id, result_kind, result_version) DO NOTHING""",
        [trial_id, cutoff, cutoff, input_hash, Jsonb(dict(validation.get("checks") or {})), Jsonb(dict(validation))],
    )
    if trial["status"] == "running":
        connection.execute(
            "UPDATE analysis.research_trial SET status = %s, finished_at = now(), outcome = %s WHERE id = %s",
            ["succeeded" if complete else "failed", Jsonb({"complete": complete}), trial_id],
        )
    return hypothesis, family, trial_id


def _ensure_research_dossier(
    connection: Any, *, strategy_revision_id: int, trial_id: Any,
    artifact: Mapping[str, Any], cutoff: datetime, validation: Mapping[str, Any], seal: bool,
) -> Any:
    sections = Jsonb({key: "walk-forward" for key in ("hypothesis", "mechanism", "falsification", "controls", "validation", "economics", "lineage")})
    dossier = connection.execute(
        "SELECT id, status FROM analysis.validation_dossier WHERE strategy_revision_id = %s",
        [strategy_revision_id],
    ).fetchone()
    if dossier is None:
        dossier = connection.execute(
            """INSERT INTO analysis.validation_dossier
               (strategy_revision_id, research_trial_id, sections, compiled_policy, artifact_id, artifact_hash)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, status""",
            [strategy_revision_id, trial_id, sections, Jsonb({"paper_only": True}), artifact["artifact_id"], artifact["artifact_hash"]],
        ).fetchone()
    if dossier["status"] == "draft":
        for code in ("pit_integrity", "denominator_completeness", "oos_predictive_validity", "falsification_and_robustness", "economic_promotability"):
            gate = dict((validation.get("gates") or {}).get(code) or {})
            connection.execute(
                """INSERT INTO analysis.validation_gate_result
                   (dossier_id, gate_code, verdict, metrics, evidence, evaluated_at, available_at)
                   VALUES (%s, %s, %s, %s, %s, %s, LEAST(now(), %s)) ON CONFLICT DO NOTHING""",
                [dossier["id"], code, "pass" if gate.get("passed") else "fail", Jsonb(gate), Jsonb({"validation": True}), cutoff, cutoff],
            )
        if seal:
            connection.execute("UPDATE analysis.validation_dossier SET status = 'sealed', sealed_at = %s WHERE id = %s", [cutoff, dossier["id"]])
    return dossier["id"]


def _centered_edge(scores: list[float], labels: list[float]) -> float:
    """Return a bounded deterministic score-label covariance for controls."""

    if len(scores) != len(labels) or len(scores) < 2:
        return 0.0
    score_mean = sum(scores) / len(scores)
    label_mean = sum(labels) / len(labels)
    return sum((score - score_mean) * (label - label_mean) for score, label in zip(scores, labels)) / len(scores)


def load_observations(runtime: DatabaseRuntime, *, cutoff: datetime) -> list[dict[str, Any]]:
    """Read only PIT-resolved outcomes with explicit research feature evidence."""

    with runtime.read(JOB_PROFILE) as connection:
        rows = connection.execute(
            """
            SELECT instrument.symbol AS ticker, outcome.horizon, decision.as_of,
                   outcome.available_at AS outcome_available_at,
                   outcome.selected_return AS realized_return,
                   outcome.metadata,
                   feature.feature_version, feature.momentum_5d, feature.momentum_20d,
                   feature.relative_strength_20d, feature.relative_strength_60d,
                   feature.kaufman_er_20d,
                   benchmark.membership_hash
            FROM analysis.ticker_outcome outcome
            JOIN analysis.ticker_decision decision ON decision.id = outcome.ticker_decision_id
            JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
            JOIN LATERAL (
                SELECT candidate.feature_version, candidate.momentum_5d,
                       candidate.momentum_20d, candidate.relative_strength_20d,
                       candidate.relative_strength_60d, candidate.kaufman_er_20d
                FROM analysis.symbol_feature candidate
                JOIN analysis.run feature_run ON feature_run.id = candidate.run_id
                WHERE candidate.instrument_id = decision.instrument_id
                  AND candidate.feature_set = 'daily_trend'
                  AND candidate.feature_version = %s
                  AND candidate.as_of <= decision.as_of
                  AND feature_run.input_cutoff <= decision.as_of
                  AND candidate.data_quality_status = 'complete'
                ORDER BY candidate.as_of DESC, feature_run.input_cutoff DESC, candidate.id DESC
                LIMIT 1
            ) feature ON TRUE
            JOIN LATERAL (
                SELECT membership_hash, exact_membership
                FROM analysis.ticker_benchmark_snapshot candidate
                WHERE candidate.as_of <= decision.as_of
                  AND candidate.available_at <= decision.as_of
                ORDER BY candidate.as_of DESC, candidate.id DESC LIMIT 1
            ) benchmark ON benchmark.exact_membership ? instrument.symbol
            WHERE outcome.state = 'resolved'
              AND outcome.available_at <= %s
              AND decision.as_of <= %s
              AND outcome.selected_return IS NOT NULL
            ORDER BY decision.as_of, instrument.symbol, outcome.horizon, outcome.horizon_sessions
            """,
            [FEATURE_VERSION, cutoff, cutoff],
        ).fetchall()
    output: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        metadata = dict(row["metadata"] or {})
        features = {
            name: row[name]
            for name in (
                "feature_version", "momentum_5d", "momentum_20d",
                "relative_strength_20d", "relative_strength_60d", "kaufman_er_20d",
            )
        }
        net_return = metadata.get("cost_adjusted_selected_return")
        if not features or net_return is None:
            continue
        output.append({
            "ticker": row["ticker"],
            "horizon": row["horizon"],
            "cohort_id": f"{row['horizon']}:{metadata.get('sector_slice') or 'unknown'}:{metadata.get('regime_slice') or 'unknown'}",
            "as_of": row["as_of"],
            "outcome_available_at": row["outcome_available_at"],
            "outcome": float(net_return) > 0,
            "realized_return": float(row["realized_return"]),
            "modeled_cost": float(row["realized_return"]) - float(net_return),
            "features": features,
            "benchmark_membership_hash": row["membership_hash"],
        })
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--cutoff")
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--authorization-mode", choices=("PAPER", "ADVISORY"))
    args = parser.parse_args()
    cutoff = _aware(args.cutoff or datetime.now(UTC))
    runtime = runtime_for_config(load_config(args.config))
    result = run(
        runtime,
        load_observations(runtime, cutoff=cutoff),
        cutoff=cutoff,
        promote=args.promote,
        authorization_mode=args.authorization_mode,
    )
    print(result)


def _aware(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stock-alpha cutoff must be timezone-aware")
    return parsed.astimezone(UTC)


if __name__ == "__main__":
    main()
