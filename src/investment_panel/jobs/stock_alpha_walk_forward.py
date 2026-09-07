"""Register and optionally promote one PIT stock-alpha challenger."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hmac
from math import isfinite
import os
from statistics import fmean
from typing import Any, Iterable, Mapping

from psycopg.types.json import Jsonb

from investment_panel.analysis.research_validation import validate_trial
from investment_panel.analysis.research_validation import multiple_testing_metrics
from investment_panel.analysis.stock_alpha import (
    COST_MODEL_VERSION,
    CONTROL_STATISTIC_VERSION,
    FEATURE_VERSION,
    MODEL_VERSION,
    TARGET_HORIZON_SESSIONS,
    TARGET_VERSION,
    build_control_results,
    content_hash,
    independent_observations,
    walk_forward,
)
from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.core.instruments import normalize_symbol
from investment_panel.database.instruments import reconcile_instrument
from investment_panel.database.confirmed_daily_prices import confirmed_forward_bars, forward_trading_dates
from investment_panel.core.decision import build_strategy_forecast, market_session_bounds, opportunity_episode_id
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE, activate_application_role


STRATEGY_KEY = "ticker-stock-alpha"
EVALUATOR_ID = "stock_alpha_walk_forward"
EVALUATOR_CODE_VERSION = "stock_alpha_walk_forward.v3"


def run(
    runtime: DatabaseRuntime,
    observations: Iterable[Mapping[str, Any]],
    *,
    cutoff: datetime,
    promote: bool = False,
    authorization_mode: str | None = None,
    promotion_cutoff: datetime | None = None,
    min_train: int = 20,
    fold_size: int = 10,
    min_cohort: int = 20,
    universe_members: Iterable[str] | None = None,
    trial_plan: Iterable[Mapping[str, Any]] | None = None,
    control_results: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one idempotent evaluation and promote only with explicit paper authority."""

    reference = _aware(cutoff)
    promotion_reference = _aware(promotion_cutoff) if promotion_cutoff is not None else reference
    if promotion_cutoff is not None and not reference <= promotion_reference <= datetime.now(UTC):
        raise ValueError("promotion cutoff must follow the input cutoff and cannot be in the future")
    source_rows = independent_observations(observations, cutoff=reference)
    configurations = _trial_configurations(
        min_train=min_train, fold_size=fold_size, min_cohort=min_cohort,
        trial_plan=trial_plan,
    )
    input_hash = content_hash({"cutoff": reference, "observations": source_rows, "trial_plan": configurations})
    members = sorted({str(member).strip().upper() for member in universe_members or () if str(member).strip()})
    dataset_hash = _dataset_hash(source_rows, members=members, configurations=configurations)
    raw_controls = dict(control_results or {})
    controls = {
        key: [float(value) for value in raw_controls.get(key, ())]
        for key in ("randomized_label_returns", "white_noise_market_returns")
    }
    raw_control_metadata = raw_controls.get("control_metadata")
    control_metadata = dict(raw_control_metadata) if isinstance(raw_control_metadata, Mapping) and raw_control_metadata else None
    promotion_blockers: list[str] = []
    artifacts = []
    for configuration in configurations:
        try:
            evaluated = walk_forward(
                source_rows,
                cutoff=reference,
                min_train=configuration["min_train"],
                fold_size=configuration["fold_size"],
                min_cohort=configuration["min_cohort"],
            )
        except (TypeError, ValueError, KeyError, OverflowError) as exc:
            evaluated = _failed_artifact(reference, str(exc))
        artifacts.append({**configuration, "artifact": evaluated})
    family_p_values = [
        max(0.0, min(1.0, 1.0 - multiple_testing_metrics(
            [float(row["net_utility_after_costs"]) for row in item["artifact"].get("predictions", [])],
            trials_tested=len(configurations),
        )["psr"]))
        for item in artifacts
    ]
    parameter_neighborhood = [
        {"configuration": {key: item[key] for key in ("trial_key", "min_train", "fold_size", "min_cohort")},
         "return": item["artifact"]["calibration_metrics"].get("lower_confidence_net_utility_after_costs")}
        for item in artifacts
    ]
    trial_runs = [
        {
            "trial_key": item["trial_key"],
            "min_train": item["min_train"], "fold_size": item["fold_size"], "min_cohort": item["min_cohort"],
            "artifact": item["artifact"],
            "validation": _validate_walk_forward_artifact(
                item["artifact"], source_rows=source_rows, cutoff=reference,
                expected_members=members, trial_keys=[config["trial_key"] for config in configurations],
                parameter_neighborhood=parameter_neighborhood, controls=controls,
                control_metadata=control_metadata,
                family_p_values=family_p_values,
            ),
        }
        for item in artifacts
    ]
    artifact = trial_runs[0]["artifact"]
    metrics = dict(artifact["calibration_metrics"])
    validation = trial_runs[0]["validation"]
    complete = all(item["validation"]["passed"] for item in trial_runs)
    evaluation_metrics = {
        "artifact_id": f"{STRATEGY_KEY}:{artifact['artifact_hash']}",
        "artifact_hash": artifact["artifact_hash"],
        "input_hash": input_hash,
        "dataset_hash": dataset_hash,
        "model_version": MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "cost_model_version": COST_MODEL_VERSION,
        "target_version": TARGET_VERSION,
        "horizon_sessions": TARGET_HORIZON_SESSIONS,
        "target": artifact["target"],
        "cohort_id": "hierarchical-stock-alpha",
        "cohort_path": artifact["cohort_path"],
        "fallback_parent": artifact["fallback_parent"],
        "effective_sample_size": metrics["effective_sample_size"],
        "oos_sample_size": metrics.get("oos_sample_size"),
        "calibration_metrics": {
            "brier_score": metrics["brier_score"],
            "calibration_error": metrics["calibration_error"],
        },
        "baseline_comparisons": artifact.get("baseline_comparisons") or {},
        "sample_windows": artifact.get("sample_windows") or [],
        "lower_confidence_net_utility_after_costs": metrics[
            "lower_confidence_net_utility_after_costs"
        ],
        "valid_through": None,
        "forecast": artifact.get("forecast"),
        "forecasts": artifact.get("forecasts") or [],
        "validation": validation,
    }
    parameters = {
        "artifact_id": evaluation_metrics["artifact_id"],
        "artifact_hash": artifact["artifact_hash"],
        "input_hash": input_hash,
        "dataset_hash": dataset_hash,
        "model_version": MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "cost_model_version": COST_MODEL_VERSION,
        "target_version": TARGET_VERSION,
        "horizon_sessions": TARGET_HORIZON_SESSIONS,
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
        # Activate the explicit NOINHERIT application role before any
        # stock-alpha persistence. This makes the production run and its
        # protected evaluator writer use the same configured authority.
        activate_application_role(connection)
        independent_members = _independent_universe_members(connection, cutoff=reference)
        if not independent_members:
            raise ValueError("stock-alpha requires an independent PIT universe tape")
        if members != independent_members:
            raise ValueError("submitted universe does not match the independent PIT universe tape")
        research_ids = _ensure_research_prerequisites(
            connection, cutoff=reference, input_hash=input_hash,
            trial_runs=trial_runs, observations=source_rows, universe_members=members,
            controls=controls, control_metadata=control_metadata,
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
            result_id=research_ids[3], artifact=evaluation_metrics, cutoff=reference, validation=validation, seal=complete,
        )

        evaluation = connection.execute(
            """
            SELECT id::text, verdict, evaluated_at, available_at FROM analysis.strategy_evaluation
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
                RETURNING id::text, verdict, evaluated_at, available_at
                """,
                [
                    strategy["id"], research_ids[0], research_ids[1], research_ids[2], dossier_id,
                    evaluation_metrics["artifact_id"], artifact["artifact_hash"], input_hash,
                    datetime.now(UTC), artifact["oos_period_start"], artifact["oos_period_end"],
                    "pass" if complete else "incomplete", Jsonb(evaluation_metrics),
                    Jsonb({
                        "paper_only": True,
                        "walk_forward": True,
                        "purge_embargo": True,
                        "artifact_hash": artifact["artifact_hash"],
                    }),
                ],
            ).fetchone()
        forecast_ids = _persist_strategy_forecasts(
            connection, strategy_revision_id=int(strategy["id"]), evaluation_id=evaluation["id"],
            artifact=artifact, input_hash=input_hash, members=members, cutoff=reference,
        )
        if complete and not forecast_ids:
            raise ValueError("stock-alpha walk-forward produced no persisted model-owned forecasts")
        forecast_pit = connection.execute(
            """SELECT count(*) AS count,
                      bool_and(generated_at <= %s AND available_at <= %s) AS available_at_cutoff
               FROM analysis.strategy_forecast
               WHERE strategy_revision_id = %s AND strategy_evaluation_id = %s
                 AND input_cutoff = %s""",
            [promotion_reference, promotion_reference, strategy["id"], evaluation["id"], reference],
        ).fetchone()
        if complete and (forecast_pit["count"] != len(forecast_ids) or not forecast_pit["available_at_cutoff"]):
            promotion_blockers.append("forecast_evidence_not_available_at_cutoff")
        if complete and (evaluation["evaluated_at"] > promotion_reference or evaluation["available_at"] > promotion_reference):
            promotion_blockers.append("evaluation_evidence_not_available_at_cutoff")
        promotion_id = None
        if promote and complete and not promotion_blockers:
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
                        datetime.now(UTC), artifact["oos_period_start"], artifact["oos_period_end"],
                        Jsonb({
                            "artifact_hash": artifact["artifact_hash"],
                            "input_hash": input_hash,
                            "authorization_mode": mode,
                            "promotion_cutoff": promotion_reference.isoformat(),
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
        "promotion_blockers": promotion_blockers,
        "promotion_reason": promotion_blockers[0] if promotion_blockers else None,
        "verdict": str(evaluation["verdict"]),
        "complete": complete,
        "input_hash": input_hash,
        "artifact": artifact,
    }


def _ensure_research_prerequisites(
    connection: Any, *, cutoff: datetime, input_hash: str,
    trial_runs: list[Mapping[str, Any]], observations: list[Mapping[str, Any]],
    universe_members: list[str], controls: Mapping[str, Iterable[float]],
    control_metadata: Mapping[str, Any] | None = None,
) -> tuple[Any, Any, Any, Any]:
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
    trial_keys = [str(item["trial_key"]) for item in trial_runs]
    connection.execute(
        """INSERT INTO analysis.experiment_manifest
           (experiment_family_id, expected_trial_count, expected_trial_keys, manifest_hash, available_at)
           VALUES (%s, %s, %s, %s, now()) ON CONFLICT DO NOTHING""",
        [family, len(trial_keys), Jsonb(sorted(trial_keys)), content_hash(sorted(trial_keys))],
    )
    manifest = connection.execute(
        """SELECT expected_trial_count, expected_trial_keys, manifest_hash
           FROM analysis.experiment_manifest WHERE experiment_family_id = %s""",
        [family],
    ).fetchone()
    if (
        manifest is None
        or manifest["expected_trial_count"] != len(trial_keys)
        or list(manifest["expected_trial_keys"]) != sorted(trial_keys)
        or str(manifest["manifest_hash"]).lower() != content_hash(sorted(trial_keys))
    ):
        raise ValueError("immutable experiment manifest does not match planned trial attempts")
    instrument_ids = [reconcile_instrument(connection, symbol, name=symbol, asset_class="equity") for symbol in universe_members]
    expected_members = sorted(str(identifier) for identifier in instrument_ids)
    primary_trial_id = None
    primary_result_id = None
    for item in trial_runs:
        trial_key = str(item["trial_key"])
        trial_hash = content_hash({"input_hash": input_hash, "trial_key": trial_key, "parameters": item})
        trial = connection.execute(
            """INSERT INTO analysis.research_trial
               (experiment_family_id, trial_key, input_cutoff, code_version, input_hash, parameters, available_at)
               VALUES (%s, %s, %s, %s, %s, %s, now())
               ON CONFLICT (experiment_family_id, trial_key) DO NOTHING
               RETURNING id, status, input_cutoff, input_hash, parameters""",
            [family, trial_key, cutoff, MODEL_VERSION, trial_hash, Jsonb({key: item[key] for key in ("min_train", "fold_size", "min_cohort")})],
        ).fetchone()
        if trial is None:
            trial = connection.execute(
                """SELECT id, status, input_cutoff, input_hash, parameters
                   FROM analysis.research_trial WHERE experiment_family_id = %s AND trial_key = %s""",
                [family, trial_key],
            ).fetchone()
        if (
            trial is None
            or trial["input_cutoff"] != cutoff
            or str(trial["input_hash"]) != trial_hash
            or dict(trial["parameters"] or {}) != {key: item[key] for key in ("min_train", "fold_size", "min_cohort")}
        ):
            raise ValueError("immutable research trial manifest does not match planned attempt")
        trial_id = trial["id"]
        if primary_trial_id is None:
            primary_trial_id = trial_id
        connection.execute(
            """INSERT INTO analysis.trial_universe_manifest
               (research_trial_id, cutoff, expected_member_count, expected_members, manifest_hash, available_at)
               VALUES (%s, %s, %s, %s, %s, now()) ON CONFLICT DO NOTHING""",
            [trial_id, cutoff, len(expected_members), Jsonb(expected_members), content_hash(expected_members)],
        )
        by_ticker = {str(row.get("ticker") or "").strip().upper(): row for row in observations}
        for rank, instrument_id in enumerate(sorted(instrument_ids), start=1):
            ticker_row = connection.execute("SELECT symbol FROM catalog.instrument WHERE id = %s", [instrument_id]).fetchone()
            ticker = str(ticker_row["symbol"]) if ticker_row else ""
            source = by_ticker.get(ticker)
            eligible = source is not None
            connection.execute(
                """INSERT INTO analysis.universe_observation
                   (research_trial_id, instrument_id, cutoff, eligible, rank, exclusion_reason,
                    observed_at, available_at, input_hash, outcome)
                   VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp(), now(), %s, %s)
                   ON CONFLICT (research_trial_id, cutoff, instrument_id) DO NOTHING""",
                [trial_id, instrument_id, cutoff, eligible, rank if eligible else None,
                 None if eligible else "no independent PIT observation", trial_hash,
                 Jsonb({"source_present": eligible, "ranked_out": not eligible})],
            )
        validation = dict(item["validation"])
        control_outcome = {
            "passed": bool(controls.get("randomized_label_returns")) and bool(controls.get("white_noise_market_returns")),
            "randomized_label_returns": [float(value) for value in controls.get("randomized_label_returns", ())],
            "white_noise_market_returns": [float(value) for value in controls.get("white_noise_market_returns", ())],
            "source": "stock_alpha_walk_forward",
            "repeats": int((control_metadata or {}).get("repeats", 0)),
            "metadata": dict(control_metadata or {}),
        }
        control_result = connection.execute(
            """INSERT INTO analysis.trial_result
               (research_trial_id, result_kind, observed_at, available_at, input_hash, metrics, outcome)
               VALUES (%s, 'negative_controls', clock_timestamp(), clock_timestamp(), %s, %s, %s)
               ON CONFLICT (research_trial_id, result_kind, result_version) DO NOTHING
               RETURNING id""",
            [trial_id, trial_hash,
             Jsonb({
                 "randomized_label_count": len(control_outcome["randomized_label_returns"]),
                 "white_noise_count": len(control_outcome["white_noise_market_returns"]),
                 "control_metadata": dict(control_metadata or {}),
                 "randomized_label_returns": control_outcome["randomized_label_returns"],
                 "white_noise_market_returns": control_outcome["white_noise_market_returns"],
             }),
             Jsonb(control_outcome)],
        ).fetchone()
        if control_result is None:
            control_result = connection.execute(
                """SELECT id, input_hash, outcome FROM analysis.trial_result
                   WHERE research_trial_id = %s AND result_kind = 'negative_controls'
                   ORDER BY result_version LIMIT 1""",
                [trial_id],
            ).fetchone()
            if control_result is None or str(control_result["input_hash"]) != trial_hash or dict(control_result["outcome"] or {}) != control_outcome:
                raise ValueError("immutable negative-control result conflicts with the planned trial outcome")
        result = connection.execute(
            """INSERT INTO analysis.trial_result
               (research_trial_id, result_kind, observed_at, available_at, input_hash, metrics, outcome)
               VALUES (%s, 'validation', clock_timestamp(), now(), %s, %s, %s)
               ON CONFLICT (research_trial_id, result_kind, result_version) DO NOTHING
               RETURNING id""",
            [trial_id, trial_hash, Jsonb(dict(validation.get("checks") or {})), Jsonb(validation)],
        ).fetchone()
        if result is None:
            result = connection.execute(
                """SELECT id, input_hash, metrics, outcome
                   FROM analysis.trial_result WHERE research_trial_id = %s AND result_kind = 'validation' ORDER BY result_version LIMIT 1""",
                [trial_id],
            ).fetchone()
            if (
                result is None
                or str(result["input_hash"]) != trial_hash
                or dict(result["metrics"] or {}) != dict(validation.get("checks") or {})
                or dict(result["outcome"] or {}) != validation
            ):
                raise ValueError("immutable trial result conflicts with the planned trial outcome")
        if primary_result_id is None:
            primary_result_id = result["id"]
        if validation.get("passed"):
            _persist_research_evidence(
                connection, trial_id=trial_id, result_id=result["id"],
                trial_input_hash=trial_hash, trial_run=item,
                all_trial_runs=trial_runs, observations=observations,
                universe_hash=content_hash(expected_members), validation=validation,
                controls=controls,
            )
        if trial["status"] == "running":
            connection.execute(
                "UPDATE analysis.research_trial SET status = %s, failure_reason = %s, finished_at = clock_timestamp(), outcome = %s WHERE id = %s",
                ["succeeded" if validation.get("passed") else "failed", None if validation.get("passed") else validation.get("reason"), Jsonb(validation), trial_id],
            )
    return hypothesis, family, primary_trial_id, primary_result_id


def _persist_research_evidence(
    connection: Any, *, trial_id: Any, result_id: Any, trial_input_hash: str,
    trial_run: Mapping[str, Any], all_trial_runs: list[Mapping[str, Any]],
    observations: list[Mapping[str, Any]], universe_hash: str,
    validation: Mapping[str, Any], controls: Mapping[str, Iterable[float]],
) -> None:
    """Write raw evaluator output, then derive the evidence manifest from it.

    The validation object is used only as a consistency check below. It is
    never used to construct a source row. This keeps a caller from making a
    shape-valid validation projection into promotion evidence.
    """

    primary_artifact = dict(trial_run.get("artifact") or {})
    signing_key = os.environ.get("MARKET_RESEARCH_EVALUATOR_SIGNING_KEY", "").strip()
    if not signing_key:
        raise ValueError("stock-alpha evaluator signing key is not configured")
    predictions = [row for row in primary_artifact.get("predictions") or () if isinstance(row, Mapping)]
    observed = [float(row["net_utility_after_costs"]) for row in predictions if row.get("net_utility_after_costs") is not None]
    path_records = [dict(row) for row in primary_artifact.get("validation_path_records") or () if isinstance(row, Mapping)]
    path_returns = [float(value) for value in primary_artifact.get("validation_paths") or ()]
    p_values = [float((row.get("metrics") or {})["p_value"]) for row in path_records if (row.get("metrics") or {}).get("p_value") is not None]
    feature_rows = [
        {key: row.get(key) for key in (
            "ticker", "opportunity_episode_id", "horizon", "cohort_id", "as_of",
            "sample_window_start", "sample_window_end", "feature_available_at", "features",
        )}
        for row in observations
    ]
    feature_hash = content_hash(feature_rows)
    neutralized = [float(row["neutralized_return"]) for row in predictions if row.get("neutralized_return") is not None]
    stability = [
        (dict(item.get("artifact") or {}).get("calibration_metrics") or {}).get(
            "lower_confidence_net_utility_after_costs"
        )
        for item in all_trial_runs
    ]
    mechanism_samples = path_returns + [float(value) for value in controls.get("randomized_label_returns", ())] + [
        float(value) for value in controls.get("white_noise_market_returns", ())
    ]
    multiple = multiple_testing_metrics(
        observed, trials_tested=len(all_trial_runs), path_returns=path_returns, p_values=p_values,
    )
    common = {
        "trial_input_hash": trial_input_hash,
        "input_hash": trial_input_hash,
        "universe_hash": universe_hash,
        "feature_hash": feature_hash,
        "evaluator_id": EVALUATOR_ID,
        "evaluator_code_version": EVALUATOR_CODE_VERSION,
    }
    rows = (
        ("controls", len(list(controls.get("randomized_label_returns", ()))) + len(list(controls.get("white_noise_market_returns", ()))), {
            **common,
            "evidence_kind": "controls",
            "randomized_label_samples": [float(value) for value in controls.get("randomized_label_returns", ())],
            "white_noise_samples": [float(value) for value in controls.get("white_noise_market_returns", ())],
        }),
        ("cpcv_paths", len(path_records), {
            **common, "evidence_kind": "cpcv_paths", "path_count": len(path_records), "path_records": path_records,
        }),
        ("neutralization", len(neutralized), {
            **common, "evidence_kind": "neutralization", "samples": neutralized,
        }),
        ("parameter_stability", len(stability), {
            **common, "evidence_kind": "parameter_stability", "samples": stability,
        }),
        ("mechanism_falsification", len(mechanism_samples), {
            **common, "evidence_kind": "mechanism_falsification", "samples": mechanism_samples,
        }),
        ("multiple_testing", len(path_returns), {
            **common, "evidence_kind": "multiple_testing", "path_returns": path_returns,
            "p_values": p_values, "metrics": multiple,
        }),
    )
    existing_run = connection.execute(
        """SELECT id
           FROM analysis.run
           WHERE run_type = 'research_evaluator'
             AND input_hash = %s
             AND inputs->>'trial_result_id' = %s
           ORDER BY started_at, id
           LIMIT 1""",
        [trial_input_hash, str(result_id)],
    ).fetchone()
    if existing_run is not None:
        # Replays use the original immutable evaluator run. This keeps the
        # writer's exact trial/result/run identity while retaining idempotent
        # publication of the same research result.
        run = existing_run["id"]
    else:
        run = connection.execute(
            """INSERT INTO analysis.run
               (run_type, input_cutoff, code_version, feature_versions, input_hash, inputs,
                started_at, finished_at, status, summary)
               VALUES ('research_evaluator', (SELECT input_cutoff FROM analysis.research_trial WHERE id = %s),
                       %s, %s, %s, %s, clock_timestamp(), clock_timestamp(), 'succeeded', %s)
               RETURNING id""",
            [trial_id, EVALUATOR_CODE_VERSION, Jsonb({"model": MODEL_VERSION, "feature": FEATURE_VERSION}),
             trial_input_hash, Jsonb({"trial_result_id": str(result_id), "evidence_groups": 6}),
             Jsonb({"trial_result_id": str(result_id), "evaluator_id": EVALUATOR_ID})],
        ).fetchone()["id"]
    sample_counts = {kind: sample_count for kind, sample_count, _ in rows}
    persisted_sources: dict[str, tuple[Any, Any, Mapping[str, Any]]] = {}
    for kind, sample_count, payload in rows:
        if sample_count <= 0:
            raise ValueError(f"{kind} evidence is missing raw evaluator output")
        authorization_payload = connection.execute(
            """SELECT analysis.research_evaluator_authorization_payload(
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s) AS payload""",
            [trial_id, result_id, run, kind, EVALUATOR_ID, EVALUATOR_CODE_VERSION,
             trial_input_hash, universe_hash, feature_hash, sample_count, Jsonb(payload)],
        ).fetchone()["payload"]
        signature = hmac.new(
            signing_key.encode("utf-8"),
            str(authorization_payload).encode("utf-8"),
            "sha256",
        ).hexdigest()
        source = connection.execute(
            """SELECT id, output_hash, available_at
               FROM analysis.write_research_evaluator_output(
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s)""",
            [trial_id, result_id, run, kind, EVALUATOR_ID, EVALUATOR_CODE_VERSION,
             trial_input_hash, universe_hash, feature_hash, sample_count, Jsonb(payload),
             signature],
        ).fetchone()
        if source is None:
            raise ValueError(f"{kind} evaluator output was not persisted")
        persisted_sources[kind] = (source["id"], source["output_hash"], payload)
    if len(persisted_sources) != 6:
        raise ValueError("independent evaluator output set is incomplete")
    for kind, (source_id, source_hash, payload) in sorted(persisted_sources.items()):
        source = {
            "id": source_id, "evidence_kind": kind,
            "evaluator_id": EVALUATOR_ID, "evaluator_code_version": EVALUATOR_CODE_VERSION,
            "input_hash": trial_input_hash, "universe_hash": universe_hash,
            "feature_hash": feature_hash, "sample_count": sample_counts[kind],
            "domain_valid": True, "raw_output": payload, "output_hash": source_hash,
        }
        existing = connection.execute(
            """SELECT evaluator_output_id, evaluator_id, evaluator_code_version,
                      input_hash, universe_hash, feature_hash, sample_count,
                      domain_valid, payload, evidence_hash
               FROM analysis.research_evidence_manifest
               WHERE trial_result_id = %s AND evidence_kind = %s""",
            [result_id, source["evidence_kind"]],
        ).fetchone()
        values = (
            source["id"], source["evaluator_id"], source["evaluator_code_version"],
            source["input_hash"], source["universe_hash"], source["feature_hash"],
            source["sample_count"], source["domain_valid"], source["raw_output"], source["output_hash"],
        )
        if existing is not None:
            existing_values = tuple(existing[key] for key in (
                "evaluator_output_id", "evaluator_id", "evaluator_code_version", "input_hash",
                "universe_hash", "feature_hash", "sample_count", "domain_valid", "payload", "evidence_hash",
            ))
            if existing_values != values:
                raise ValueError("immutable evidence manifest conflicts with evaluator output")
            continue
        connection.execute(
            """INSERT INTO analysis.research_evidence_manifest
               (research_trial_id, trial_result_id, evidence_kind,
                evaluator_id, evaluator_code_version, input_hash, universe_hash,
                feature_hash, evaluator_output_id, sample_count, domain_valid,
                payload, evidence_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [trial_id, result_id, source["evidence_kind"], source["evaluator_id"],
             source["evaluator_code_version"], source["input_hash"], source["universe_hash"],
             source["feature_hash"], source["id"], source["sample_count"], source["domain_valid"],
             Jsonb(source["raw_output"]), source["output_hash"]],
        )
    source_validation = connection.execute(
        "SELECT analysis.research_evidence_complete(%s) AS complete", [result_id]
    ).fetchone()["complete"]
    if not source_validation:
        raise ValueError("independent research evaluator output set is incomplete")
    # The manifest trigger compares all source arrays and metrics with the
    # linked validation result. Full family and attempt completion is checked
    # only after every planned trial has reached a terminal state.
    if not connection.execute(
        "SELECT analysis.research_evidence_complete(%s) AS complete", [result_id]
    ).fetchone()["complete"]:
        raise ValueError("independent research evidence manifest is incomplete")


def _ensure_research_dossier(
    connection: Any, *, strategy_revision_id: int, trial_id: Any,
    result_id: Any, artifact: Mapping[str, Any], cutoff: datetime, validation: Mapping[str, Any], seal: bool,
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
            [strategy_revision_id, trial_id, sections, Jsonb({
                "paper_only": True,
                "required_metrics": ["psr", "dsr", "pbo", "data_snooping_probability", "fdr_q_value", "cost_capacity"],
                "required_cost_multiples": ["1x", "2x", "3x"],
            }), artifact["artifact_id"], artifact["artifact_hash"]],
        ).fetchone()
    if dossier["status"] == "draft" and not validation.get("passed"):
        return dossier["id"]
    if dossier["status"] == "draft":
        checks = dict(validation.get("checks") or {})
        evidence_rows = connection.execute(
            """SELECT id, evidence_kind, evidence_hash
               FROM analysis.research_evidence_manifest
               WHERE trial_result_id = %s ORDER BY evidence_kind""",
            [result_id],
        ).fetchall()
        if len(evidence_rows) != 6:
            raise ValueError("dossier requires six independent research evidence manifests")
        evidence_hashes = [str(row["evidence_hash"]) for row in evidence_rows]
        for code in ("pit_integrity", "denominator_completeness", "oos_predictive_validity", "falsification_and_robustness", "economic_promotability"):
            gate = dict((validation.get("gates") or {}).get(code) or {})
            # Store the complete immutable check object on every gate. This
            # lets PostgreSQL compare gate evidence to the linked trial result,
            # instead of accepting a small or generic passed=true subset.
            evidence_checks = checks
            domain_valid = all(bool((value or {}).get("domain_valid")) for value in evidence_checks.values())
            connection.execute(
                """INSERT INTO analysis.validation_gate_result
                   (dossier_id, gate_code, verdict, metrics, evidence)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            [dossier["id"], code, "pass" if gate.get("passed") else "fail", Jsonb({
                **gate, "domain_valid": domain_valid, "checks": evidence_checks,
                "validation_result_id": str(result_id),
                "validation_result_input_hash": str(connection.execute(
                    "SELECT input_hash FROM analysis.trial_result WHERE id = %s", [result_id]
                ).fetchone()["input_hash"]),
                "evidence_manifest_hashes": evidence_hashes,
            }), Jsonb({
                "trial_result_id": str(result_id),
                "input_hash": str(connection.execute(
                    "SELECT input_hash FROM analysis.trial_result WHERE id = %s", [result_id]
                ).fetchone()["input_hash"]),
                "checks": list(evidence_checks),
                "evidence_manifest_ids": [str(row["id"]) for row in evidence_rows],
            })],
            )
        if seal:
            connection.execute("UPDATE analysis.validation_dossier SET status = 'sealed', sealed_at = clock_timestamp() WHERE id = %s", [dossier["id"]])
    return dossier["id"]


def _trial_configurations(*, min_train: int, fold_size: int, min_cohort: int,
                          trial_plan: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if trial_plan is None:
        raw = [
            {"trial_key": "baseline", "min_train": min_train, "fold_size": fold_size, "min_cohort": min_cohort},
            {"trial_key": "neighbor-minus", "min_train": max(2, min_train - 1), "fold_size": fold_size, "min_cohort": min_cohort},
            {"trial_key": "neighbor-plus", "min_train": min_train + 1, "fold_size": fold_size, "min_cohort": min_cohort},
        ]
    else:
        raw = [dict(item) for item in trial_plan]
    if not raw or len(raw) > 64:
        raise ValueError("trial manifest must contain 1..64 planned attempts")
    output: list[dict[str, Any]] = []
    keys: set[str] = set()
    for item in raw:
        key = str(item.get("trial_key") or "").strip()
        if not key or key in keys:
            raise ValueError("trial manifest keys must be unique and non-empty")
        keys.add(key)
        values = {name: int(item.get(name, 0)) for name in ("min_train", "fold_size", "min_cohort")}
        if any(value < 1 or value > 10000 for value in values.values()):
            raise ValueError("trial parameters are outside bounded domain")
        output.append({"trial_key": key, **values})
    return output


def _dataset_hash(
    observations: list[Mapping[str, Any]], *, members: list[str],
    configurations: list[Mapping[str, Any]],
) -> str:
    """Separate new learning evidence from the time the scheduler checked it."""
    return content_hash({
        "observations": observations, "universe_members": members,
        "trial_plan": configurations, "model_version": MODEL_VERSION,
        "target_version": TARGET_VERSION, "feature_version": FEATURE_VERSION,
        "cost_model_version": COST_MODEL_VERSION, "evaluator_version": EVALUATOR_CODE_VERSION,
        "control_repeats": 8, "control_statistic_version": CONTROL_STATISTIC_VERSION,
    })


def _failed_artifact(cutoff: datetime, reason: str) -> dict[str, Any]:
    artifact = {
        "model_version": MODEL_VERSION, "feature_version": FEATURE_VERSION,
        "cost_model_version": COST_MODEL_VERSION, "target": "positive_return_after_costs",
        "target_version": TARGET_VERSION, "horizon_sessions": TARGET_HORIZON_SESSIONS,
        "horizons": [], "oos_period_start": None, "oos_period_end": None,
        "calibration_metrics": {"brier_score": None, "calibration_error": None,
                                 "effective_sample_size": 0, "oos_sample_size": 0,
                                 "lower_confidence_net_utility_after_costs": None},
        "cohort_path": [], "fallback_parent": None, "predictions": [], "forecasts": [],
        "forecast": None, "validation_paths": [], "failure_reason": reason,
    }
    artifact["artifact_hash"] = content_hash(artifact)
    return artifact


def _validate_walk_forward_artifact(
    artifact: Mapping[str, Any], *, source_rows: list[Mapping[str, Any]], cutoff: datetime,
    expected_members: list[str], trial_keys: list[str], parameter_neighborhood: list[Mapping[str, Any]],
    controls: Mapping[str, list[float]], family_p_values: list[float],
    control_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    predictions = list(artifact.get("predictions") or [])
    observed = [float(row["net_utility_after_costs"]) for row in predictions if row.get("net_utility_after_costs") is not None]
    neutralized = [float(row["neutralized_return"]) for row in predictions if row.get("neutralized_return") is not None]
    paths = [float(value) for value in artifact.get("validation_paths") or []]
    path_records = [dict(value) for value in artifact.get("validation_path_records") or [] if isinstance(value, Mapping)]
    if not paths and observed:
        # No fabricated path is permitted. This keeps the PBO gate closed until
        # the walk-forward producer emits its real purged/combinatorial paths.
        paths = []
    path_p_values = [
        float((record.get("metrics") or {})["p_value"])
        for record in path_records
        if (record.get("metrics") or {}).get("p_value") is not None
    ]
    available = [row.get("feature_available_at") for row in source_rows]
    gross = [float(row.get("realized_return", 0.0)) for row in predictions]
    base_cost = fmean([float(row.get("modeled_cost", 0.0)) for row in predictions]) if predictions else 0.0
    return validate_trial(
        mechanism_class="PIT stock-alpha momentum mechanism",
        falsification_rule="randomized labels, white-noise markets, and future-information trap",
        observed_returns=observed,
        randomized_returns=controls.get("randomized_label_returns", []),
        white_noise_returns=controls.get("white_noise_market_returns", []),
        gross_return=fmean(gross) if gross else 0.0,
        base_cost=base_cost,
        neutralized_returns=neutralized,
        parameter_neighborhood=parameter_neighborhood,
        trials_tested=len(trial_keys),
        feature_available_at=available,
        decision_times=[row.get("as_of") for row in source_rows],
        cutoff=cutoff,
        expected_members=expected_members,
        observed_members=expected_members,
        expected_attempts=trial_keys,
        # A failed attempt is still a completed, terminal attempt.  The
        # manifest gate measures accounting completeness; the other gates
        # carry the failure evidence and prevent promotion.
        completed_attempts=trial_keys,
        path_returns=paths,
        path_records=path_records,
        # Path-level p-values are produced by each independent CPCV rerun and
        # therefore feed the FDR/data-snooping evidence. The family values
        # remain a bounded fallback for callers supplying an artifact without
        # path records; production artifacts always have both.
        p_values=path_p_values or family_p_values,
        control_metadata=control_metadata,
        policy={"min_psr": 0.5, "min_dsr": 0.5, "max_pbo": 0.5},
    )


def _persist_strategy_forecasts(connection: Any, *, strategy_revision_id: int, evaluation_id: str,
                                artifact: Mapping[str, Any], input_hash: str, members: list[str], cutoff: datetime) -> list[str]:
    """Persist the exact model distribution emitted by walk_forward."""
    forecasts = [item for item in artifact.get("forecasts") or [] if isinstance(item, Mapping)]
    if not forecasts:
        return []
    timestamp = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    persisted: list[str] = []
    for ticker in members:
        instrument = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = %s", [ticker]).fetchone()
        if instrument is None:
            continue
        for item in forecasts:
            model = build_strategy_forecast(
                ticker=ticker, opportunity_episode_id=opportunity_episode_id(ticker),
                strategy_revision_id=strategy_revision_id, strategy_evaluation_id=evaluation_id,
                target=str(artifact["target"]), horizon=str(item["horizon"]),
                forecast_value=float(item["forecast_value"]), forecast_distribution=dict(item["forecast_distribution"]),
                probability_semantics=str(item["probability_semantics"]), model_artifact_id=f"{STRATEGY_KEY}:{artifact['artifact_hash']}",
                artifact_hash=str(artifact["artifact_hash"]), input_hash=input_hash,
                as_of=cutoff, generated_at=timestamp, available_at=timestamp,
            )
            existing = connection.execute(
                """SELECT id, strategy_revision_id, strategy_evaluation_id, instrument_id,
                          opportunity_episode_id, target, horizon, forecast_value,
                          forecast_distribution, probability_semantics, model_artifact_id,
                          artifact_hash, input_hash, as_of, input_cutoff, generated_at, available_at
                   FROM analysis.strategy_forecast WHERE id = %s""",
                [model.strategy_forecast_id],
            ).fetchone()
            if existing is None:
                existing = connection.execute(
                    """SELECT id, strategy_revision_id, strategy_evaluation_id, instrument_id,
                                 opportunity_episode_id, target, horizon, forecast_value,
                                 forecast_distribution, probability_semantics, model_artifact_id,
                                 artifact_hash, input_hash, as_of, input_cutoff, generated_at, available_at
                       FROM analysis.strategy_forecast
                       WHERE strategy_revision_id = %s AND instrument_id = %s
                         AND opportunity_episode_id = %s AND horizon = %s
                         AND input_cutoff = %s AND artifact_hash = %s""",
                    [model.strategy_revision_id, instrument["id"], model.opportunity_episode_id,
                     model.horizon, model.input_cutoff, model.artifact_hash],
                ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO analysis.strategy_forecast
                       (id, strategy_revision_id, strategy_evaluation_id, instrument_id, opportunity_episode_id,
                        target, horizon, forecast_value, forecast_distribution, probability_semantics,
                        model_artifact_id, artifact_hash, input_hash, as_of, input_cutoff, generated_at, available_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [model.strategy_forecast_id, model.strategy_revision_id, model.strategy_evaluation_id, instrument["id"],
                     model.opportunity_episode_id, model.target, model.horizon, model.forecast_value,
                     Jsonb(model.forecast_distribution), model.probability_semantics, model.model_artifact_id,
                     model.artifact_hash, model.input_hash, model.as_of, model.input_cutoff, model.generated_at, model.available_at],
                )
                persisted.append(model.strategy_forecast_id)
            else:
                # A replay may discover the canonical row through the stable
                # revision/instrument/horizon uniqueness key. Rebuild the
                # model with that row's actual wall-clock evidence timestamps
                # before comparing the content address; never return a
                # different row under a caller-supplied identity.
                if str(existing["id"]) != model.strategy_forecast_id:
                    model = build_strategy_forecast(
                        ticker=ticker, opportunity_episode_id=model.opportunity_episode_id,
                        strategy_revision_id=model.strategy_revision_id,
                        strategy_evaluation_id=model.strategy_evaluation_id,
                        target=model.target, horizon=model.horizon,
                        forecast_value=model.forecast_value,
                        forecast_range=model.forecast_range,
                        forecast_distribution=model.forecast_distribution,
                        probability_semantics=model.probability_semantics,
                        model_artifact_id=model.model_artifact_id,
                        artifact_hash=model.artifact_hash, input_hash=model.input_hash,
                        as_of=model.as_of, generated_at=existing["generated_at"],
                        available_at=existing["available_at"],
                    )
                immutable = {
                    "strategy_revision_id": model.strategy_revision_id,
                    "strategy_evaluation_id": model.strategy_evaluation_id,
                    "instrument_id": instrument["id"],
                    "opportunity_episode_id": model.opportunity_episode_id,
                    "target": model.target,
                    "horizon": model.horizon,
                    "forecast_value": model.forecast_value,
                    "forecast_distribution": model.forecast_distribution,
                    "probability_semantics": model.probability_semantics,
                    "model_artifact_id": model.model_artifact_id,
                    "artifact_hash": model.artifact_hash,
                    "input_hash": model.input_hash,
                    "as_of": model.as_of,
                    "input_cutoff": model.input_cutoff,
                    "generated_at": model.generated_at,
                    "available_at": model.available_at,
                }
                for field, expected in immutable.items():
                    actual = existing[field]
                    if field == "strategy_evaluation_id":
                        actual, expected = str(actual) if actual is not None else None, str(expected) if expected is not None else None
                    elif field in {"as_of", "input_cutoff"}:
                        actual, expected = actual.astimezone(UTC), expected.astimezone(UTC)
                    if actual != expected:
                        raise ValueError(f"stock-alpha forecast persistence conflicts with immutable authority: {field}")
                persisted.append(str(existing["id"]))
    if len(persisted) != len(members) * len(forecasts):
        raise ValueError("stock-alpha forecast persistence is incomplete")
    return persisted


def load_observations(runtime: DatabaseRuntime, *, cutoff: datetime) -> list[dict[str, Any]]:
    """Read only PIT-resolved outcomes with explicit research feature evidence."""

    with runtime.read(JOB_PROFILE) as connection:
        rows = connection.execute(
            """
            SELECT instrument.id AS instrument_id, instrument.symbol AS ticker, outcome.horizon, outcome.horizon_sessions,
                   instrument.delisted_at, instrument.delisting_price,
                   instrument.delisting_available_at, instrument.delisting_source,
                   decision.as_of, decision.opportunity_episode_id,
                   outcome.available_at AS outcome_available_at,
                   outcome.measured_through AS outcome_measured_through,
                   outcome.stock_counterfactual_return AS realized_return,
                   outcome.market_return, outcome.sector_return,
                   outcome.metadata,
                   feature.feature_version, feature.momentum_5d, feature.momentum_20d,
                   feature.relative_strength_20d, feature.relative_strength_60d,
                   feature.kaufman_er_20d,
                   feature.feature_available_at,
                   benchmark.membership_hash
            FROM analysis.ticker_outcome outcome
            JOIN analysis.ticker_decision decision ON decision.id = outcome.ticker_decision_id
            JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
            JOIN LATERAL (
                SELECT candidate.feature_version, candidate.momentum_5d,
                       candidate.momentum_20d, candidate.relative_strength_20d,
                       candidate.relative_strength_60d, candidate.kaufman_er_20d,
                       feature_run.finished_at AS feature_available_at
                FROM analysis.symbol_feature candidate
                JOIN analysis.run feature_run ON feature_run.id = candidate.run_id
                WHERE candidate.instrument_id = decision.instrument_id
                  AND candidate.feature_set = 'daily_trend'
                  AND candidate.feature_version = %s
                  AND candidate.as_of <= decision.as_of
                  AND feature_run.input_cutoff <= decision.as_of
                  AND feature_run.finished_at IS NOT NULL
                  AND feature_run.finished_at <= decision.as_of
                  AND candidate.data_quality_status = 'complete'
                ORDER BY candidate.as_of DESC, feature_run.input_cutoff DESC, candidate.id DESC
                LIMIT 1
            ) feature ON TRUE
            JOIN LATERAL (
                SELECT membership_hash, exact_membership
                FROM analysis.ticker_benchmark_snapshot candidate
                WHERE candidate.benchmark_key = 'market-equity-etf'
                  AND candidate.as_of <= decision.as_of
                  AND candidate.available_at <= decision.as_of
                ORDER BY candidate.as_of DESC, candidate.id DESC LIMIT 1
            ) benchmark ON benchmark.exact_membership ? instrument.symbol
            WHERE outcome.state = 'resolved'
              AND outcome.available_at <= %s
              AND decision.as_of <= %s
              AND decision.published_at <= %s
              AND outcome.horizon = 'TACTICAL'
              AND outcome.horizon_sessions = %s
              AND outcome.stock_counterfactual_return IS NOT NULL
            ORDER BY decision.as_of, instrument.symbol, outcome.horizon, outcome.horizon_sessions
            """,
            [FEATURE_VERSION, cutoff, cutoff, cutoff, TARGET_HORIZON_SESSIONS],
        ).fetchall()
        rows = [
            mature for row in rows
            if (mature := _mature_stock_target(connection, row, cutoff=cutoff)) is not None
        ]
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
        net_return = metadata.get("cost_adjusted_stock_counterfactual_return")
        if not features or net_return is None:
            continue
        try:
            realized = float(row["realized_return"])
            net_return = float(net_return)
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(isfinite(value) for value in (realized, net_return)) or net_return > realized:
            continue
        output.append({
            "ticker": row["ticker"],
            "opportunity_episode_id": row["opportunity_episode_id"] or opportunity_episode_id(row["ticker"]),
            "horizon": row["horizon"],
            "horizon_sessions": row["horizon_sessions"],
            "target_version": TARGET_VERSION,
            "cohort_id": f"{row['horizon']}:{metadata.get('sector_slice') or 'unknown'}:{metadata.get('regime_slice') or 'unknown'}",
            "as_of": row["as_of"],
            "outcome_available_at": row["outcome_available_at"],
            "outcome_measured_through": row["outcome_measured_through"] if row["outcome_measured_through"] is not None else metadata.get("observed_through"),
            "feature_available_at": row["feature_available_at"],
            **({"terminal_evidence": row["terminal_evidence"]} if row.get("terminal_evidence") else {}),
            "outcome": net_return > 0,
            "realized_return": realized,
            "modeled_cost": realized - net_return,
            "features": features,
            "benchmark_membership_hash": row["membership_hash"],
            "baseline_returns": {
                "cash": metadata.get("cost_adjusted_cash_return"),
                "market": row["market_return"],
                "sector": row["sector_return"],
                "trend": metadata.get("trend_counterfactual_return"),
            },
        })
    return independent_observations(output, cutoff=cutoff)


def _mature_stock_target(
    connection: Any, row: Mapping[str, Any], *, cutoff: datetime,
) -> dict[str, Any] | None:
    """Recheck legacy maturity against the canonical bars known at its clock."""

    metadata = dict(row["metadata"] or {})
    try:
        measured = _aware(row["outcome_measured_through"] or metadata.get("observed_through"))
    except (TypeError, ValueError):
        return None
    if not row["as_of"] < measured <= row["outcome_available_at"]:
        return None
    if metadata.get("delisting_status") == "delisted_terminal":
        return _terminal_stock_target(row, measured=measured, cutoff=cutoff)
    bars = confirmed_forward_bars(
        connection, row["instrument_id"], as_of=row["as_of"],
        cutoff=row["outcome_available_at"], sessions=TARGET_HORIZON_SESSIONS,
    )
    return dict(row) if (
        len(bars) == TARGET_HORIZON_SESSIONS
        and bars[-1]["observed_at"] == measured
    ) else None


def _terminal_stock_target(
    row: Mapping[str, Any], *, measured: datetime, cutoff: datetime,
) -> dict[str, Any] | None:
    """Carry a verified terminal value to the original fixed-horizon boundary."""

    metadata = dict(row["metadata"] or {})
    mark = dict(dict(metadata.get("expression_marks") or {}).get("STOCK") or {})
    try:
        delisted_at = _aware(row["delisted_at"])
        evidence_available = _aware(row["delisting_available_at"])
        mark_observed = _aware(mark["observed_at"])
        mark_available = _aware(mark["available_at"])
        price, entry_price = float(row["delisting_price"]), float(mark["entry_price"])
        mark_price, realized = float(mark["mark_price"]), float(row["realized_return"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    horizon_end = market_session_bounds(forward_trading_dates(
        row["as_of"], count=TARGET_HORIZON_SESSIONS,
    )[-1])[1].astimezone(UTC)
    if (
        mark.get("status") != "delisted_terminal"
        or not str(row.get("delisting_source") or "").strip()
        or not all(isfinite(value) for value in (price, entry_price, mark_price, realized))
        or price < 0 or entry_price <= 0 or mark_price != price
        or abs(realized - (price / entry_price - 1.0)) > 1e-10
        or measured != delisted_at or mark_observed != delisted_at
        or mark_available != evidence_available
        or not row["as_of"] < delisted_at <= horizon_end <= _aware(cutoff)
        or not delisted_at <= evidence_available <= row["outcome_available_at"]
    ):
        return None
    return {
        **row,
        # The terminal cash value is final, but the target remains a full
        # 20-session observation. Keep the original mark clock in lineage.
        "outcome_measured_through": horizon_end,
        "outcome_available_at": max(row["outcome_available_at"], horizon_end),
        "terminal_evidence": {
            "source_id": row["delisting_source"], "instrument_id": row["instrument_id"],
            "observed_at": delisted_at, "available_at": evidence_available,
            "price": price, "entry_price": entry_price,
            "original_outcome_available_at": row["outcome_available_at"],
        },
    }


def load_universe_members(runtime: DatabaseRuntime, *, cutoff: datetime) -> list[str]:
    """Load the independent PIT membership tape used for the denominator."""
    with runtime.read(JOB_PROFILE) as connection:
        row = connection.execute(
            """SELECT exact_membership
               FROM analysis.ticker_benchmark_snapshot
               WHERE benchmark_key = 'market-equity-etf'
                 AND as_of <= %s AND available_at <= %s
               ORDER BY as_of DESC, available_at DESC, id DESC LIMIT 1""",
            [cutoff, cutoff],
        ).fetchone()
    return sorted({normalize_symbol(str(symbol)) for symbol in (row["exact_membership"] if row else []) if str(symbol).strip()})


def _independent_universe_members(connection: Any, *, cutoff: datetime) -> list[str]:
    row = connection.execute(
        """SELECT exact_membership
           FROM analysis.ticker_benchmark_snapshot
           WHERE benchmark_key = 'market-equity-etf'
             AND as_of <= %s AND available_at <= %s
           ORDER BY as_of DESC, available_at DESC, id DESC LIMIT 1""",
        [cutoff, cutoff],
    ).fetchone()
    return sorted({normalize_symbol(str(symbol)) for symbol in (row["exact_membership"] if row else []) if str(symbol).strip()})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--cutoff")
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--authorization-mode", choices=("PAPER", "ADVISORY"))
    args = parser.parse_args()
    cutoff = _aware(args.cutoff or datetime.now(UTC))
    runtime = runtime_for_config(load_config(args.config))
    observations = load_observations(runtime, cutoff=cutoff)
    controls = build_control_results(observations, cutoff=cutoff)
    if not controls["randomized_label_returns"] or not controls["white_noise_market_returns"]:
        raise ValueError("stock-alpha production run failed closed: non-empty repeated controls unavailable")
    result = run(
        runtime, observations, cutoff=cutoff,
        promote=args.promote, authorization_mode=args.authorization_mode,
        universe_members=load_universe_members(runtime, cutoff=cutoff),
        control_results=controls,
    )
    print(result)


def scheduled(config_path: str | None = None) -> dict[str, Any]:
    """Run the stock-alpha path on its scheduled, paper-only boundary."""

    config = load_config(config_path)
    runtime = runtime_for_config(config)
    cutoff = datetime.now(UTC)
    observations = load_observations(runtime, cutoff=cutoff)
    members = load_universe_members(runtime, cutoff=cutoff)
    auto_promote = config.analysis.options_decision_system.strategy_auto_promotion_enabled
    dataset_hash = _dataset_hash(
        observations, members=members,
        configurations=_trial_configurations(min_train=20, fold_size=10, min_cohort=20, trial_plan=None),
    )
    with runtime.read(JOB_PROFILE) as connection:
        previous = connection.execute(
            """SELECT strategy.id AS strategy_revision_id, strategy.revision AS strategy_revision,
                      strategy.status, evaluation.id::text AS strategy_evaluation_id,
                      evaluation.verdict, trial.input_cutoff
               FROM analysis.strategy_revision strategy
               JOIN analysis.strategy_evaluation evaluation ON evaluation.strategy_revision_id = strategy.id
               JOIN analysis.research_trial trial ON trial.id = evaluation.research_trial_id
               WHERE strategy.strategy_key = %s AND strategy.parameters->>'dataset_hash' = %s
                 AND evaluation.evaluation_type = 'out_of_sample'
               ORDER BY strategy.revision DESC, evaluation.evaluated_at DESC LIMIT 1""",
            [STRATEGY_KEY, dataset_hash],
        ).fetchone()
    if previous and not (auto_promote and previous["verdict"] == "pass" and previous["status"] == "candidate"):
        return {
            "status": "ok" if previous["verdict"] == "pass" else "partial",
            "reason": "no_new_stock_alpha_evidence", "skipped": True,
            "observations": len(observations), "dataset_hash": dataset_hash,
            "complete": previous["verdict"] == "pass",
            "strategy_revision_id": previous["strategy_revision_id"],
            "strategy_revision": previous["strategy_revision"],
            "strategy_evaluation_id": previous["strategy_evaluation_id"],
        }
    if previous:
        cutoff = previous["input_cutoff"]
    controls = build_control_results(observations, cutoff=cutoff)
    controls_missing = not controls["randomized_label_returns"] or not controls["white_noise_market_returns"]
    result = run(
        runtime, observations, cutoff=cutoff,
        universe_members=members,
        control_results=controls,
    )
    if auto_promote and result["complete"]:
        # Evaluation and publication finish before the separate PAPER authority
        # clock. The replay keeps the original trial and actual forecast times.
        result = run(
            runtime, observations, cutoff=cutoff, promote=True,
            authorization_mode="PAPER", promotion_cutoff=datetime.now(UTC),
            universe_members=members, control_results=controls,
        )
    return {
        "status": "partial" if controls_missing or not result["complete"] else "ok",
        "reason": "repeated_control_observations_unavailable" if controls_missing else None,
        "observations": len(observations),
        "control_metadata": controls.get("control_metadata") or {},
        **result,
    }


def _aware(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stock-alpha cutoff must be timezone-aware")
    return parsed.astimezone(UTC)


if __name__ == "__main__":
    main()
