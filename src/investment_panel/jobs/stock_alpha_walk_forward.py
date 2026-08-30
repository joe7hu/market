"""Register and optionally promote one PIT stock-alpha challenger."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

from psycopg.types.json import Jsonb

from investment_panel.analysis.stock_alpha import (
    COST_MODEL_VERSION,
    FEATURE_VERSION,
    MODEL_VERSION,
    content_hash,
    walk_forward,
)
from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
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
    complete = all((
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
    mode = str(authorization_mode or "").upper()
    if promote and mode not in {"PAPER", "ADVISORY"}:
        raise ValueError("promotion requires explicit PAPER or ADVISORY authorization")

    with runtime.transaction(JOB_PROFILE) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [STRATEGY_KEY])
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
                    (strategy_key, revision, name, status, parameters, authority_group)
                VALUES (%s, %s, %s, 'candidate', %s, %s)
                RETURNING id, revision, status, parameters
                """,
                [STRATEGY_KEY, revision, "PIT stock alpha", Jsonb(parameters), STRATEGY_KEY],
            ).fetchone()
        elif dict(strategy["parameters"] or {}) != parameters:
            raise ValueError("immutable stock-alpha revision parameters do not match")
        if promote and strategy["status"] == "superseded":
            raise ValueError("superseded stock-alpha revisions cannot be replay-promoted")

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
                    strategy_revision_id, evaluation_type, evaluated_at,
                    period_start, period_end, verdict, metrics, evidence
                ) VALUES (%s, 'out_of_sample', %s, %s, %s, %s, %s, %s)
                RETURNING id::text, verdict
                """,
                [
                    strategy["id"], reference, artifact["oos_period_start"], artifact["oos_period_end"],
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
                        strategy_revision_id, evaluation_type, evaluated_at,
                        period_start, period_end, verdict, metrics, evidence
                    ) VALUES (%s, 'paper_advisory_promotion', %s, %s, %s, 'pass', %s, %s)
                    RETURNING id::text
                    """,
                    [
                        strategy["id"], reference, artifact["oos_period_start"], artifact["oos_period_end"],
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
