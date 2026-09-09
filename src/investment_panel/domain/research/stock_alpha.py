"""Deterministic point-in-time stock-alpha walk-forward evaluation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from math import erfc, exp, isfinite, sqrt
from random import Random
from statistics import fmean, stdev
from typing import Any, Callable, Iterable, Mapping

from investment_panel.domain.research.stats import brier_score
from investment_panel.domain.research.research_validation import combinatorial_path_records, multiple_testing_metrics


MODEL_VERSION = "ticker-stock-alpha.v3"
FEATURE_VERSION = "daily-trend-v1"
COST_MODEL_VERSION = "stock-close-estimated-cost-v1"
TARGET_VERSION = "stock-counterfactual-20-session-net.v1"
TARGET_HORIZON_SESSIONS = 20
CONTROL_STATISTIC_VERSION = "oos-brier-improvement-over-generator-null.v1"
RESEARCH_FEATURES = (
    "momentum_5d",
    "momentum_20d",
    "relative_strength_20d",
    "relative_strength_60d",
    "kaufman_er_20d",
)


def research_score(features: Mapping[str, Any]) -> float | None:
    """Return a research-only score; missing evidence is never imputed."""

    if str(features.get("feature_version") or "") != FEATURE_VERSION:
        return None
    values = {name: _number(features.get(name)) for name in RESEARCH_FEATURES}
    if any(value is None for value in values.values()):
        return None
    momentum_20d = float(values["momentum_20d"])
    direction = 1.0 if momentum_20d > 0 else -1.0
    acceleration = direction * (float(values["momentum_5d"]) - momentum_20d / 4.0)
    return round(
        acceleration
        + direction * float(values["relative_strength_20d"])
        + 0.5 * direction * float(values["relative_strength_60d"])
        + 0.25 * float(values["kaufman_er_20d"]),
        8,
    )


def walk_forward(
    observations: Iterable[Mapping[str, Any]],
    *,
    cutoff: datetime,
    min_train: int = 20,
    fold_size: int = 10,
    purge: timedelta = timedelta(days=1),
    embargo: timedelta = timedelta(days=1),
    min_cohort: int = 20,
) -> dict[str, Any]:
    """Evaluate expanding folds with PIT outcomes and hierarchical calibration."""

    reference = _aware(cutoff)
    rows = independent_observations(observations, cutoff=reference)
    pit_rejections: list[dict[str, Any]] = []
    predictions: list[dict[str, Any]] = []
    folds_data: list[dict[str, Any]] = []

    def row_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
        return (row["ticker"], row["horizon"], row["cohort_id"], row["as_of"])

    def evaluate_rows(
        test_rows: Iterable[Mapping[str, Any]], training: Iterable[Mapping[str, Any]],
        *, test_start: datetime, test_end: datetime,
        boundary_by_key: Mapping[tuple[Any, ...], datetime], path_label: str | None = None,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        training_rows = list(training)
        for raw_row in test_rows:
            row = dict(raw_row)
            boundary = boundary_by_key.get(row_key(row), test_start)
            reason = (
                "outcome_not_available_at_cutoff" if row["outcome_available_at"] > reference else
                "feature_not_available_at_cutoff" if row["feature_available_at"] > reference else
                "feature_not_available_at_decision" if row["feature_available_at"] > row["as_of"] else
                "feature_not_available_at_fold_boundary" if row["feature_available_at"] > boundary else
                None
            )
            if reason:
                pit_rejections.append({"ticker": row["ticker"], "as_of": row["as_of"].isoformat(), "reason": reason})
                continue
            raw_probability = _probability(float(row["research_score"]))
            calibrated, path, parent, effective_sample = hierarchical_calibration(
                raw_probability,
                training_rows,
                horizon=row["horizon"],
                cohort_id=row["cohort_id"],
                min_cohort=min_cohort,
            )
            if calibrated is None:
                continue
            modeled_cost = float(row["modeled_cost"])
            output.append({
                **row,
                "raw_probability": raw_probability,
                "calibrated_probability": calibrated,
                "cohort_path": path,
                "fallback_parent": parent,
                "effective_sample_size": effective_sample,
                "net_utility_after_costs": float(row["realized_return"]) - modeled_cost,
                "fold_train_end": (test_start - purge).isoformat(),
                "fold_test_start": test_start.isoformat(),
                "fold_test_end": test_end.isoformat(),
                "embargo_until": (test_end + embargo).isoformat(),
                **({"validation_path": path_label} if path_label else {}),
            })
        return output

    for start in range(min_train, len(rows), max(1, fold_size)):
        test = rows[start:start + max(1, fold_size)]
        if not test:
            continue
        test_start = test[0]["as_of"]
        training = [
            row for row in rows[:start]
            if row["outcome_available_at"] <= test_start - purge
            and row["as_of"] + embargo <= test_start
            and row["feature_available_at"] <= row["as_of"]
            and row["feature_available_at"] <= test_start - purge
        ]
        test_end = test[-1]["as_of"]
        fold_predictions = evaluate_rows(
            test, training, test_start=test_start, test_end=test_end,
            boundary_by_key={row_key(row): test_start for row in test},
        )
        predictions.extend(fold_predictions)
        folds_data.append({
            "test": test, "test_start": test_start, "test_end": test_end,
            "predictions": fold_predictions,
        })

    pairs = [(row["calibrated_probability"], row["outcome"]) for row in predictions]
    utilities = [float(row["net_utility_after_costs"]) for row in predictions]
    horizon_means = {
        horizon: fmean(float(row["realized_return"]) for row in predictions if row["horizon"] == horizon)
        for horizon in sorted({row["horizon"] for row in predictions})
    }
    for row in predictions:
        row["neutralized_return"] = round(
            float(row["realized_return"]) - horizon_means[row["horizon"]], 8,
        )
    lower_utility = None
    if utilities:
        lower_utility = fmean(utilities)
        if len(utilities) > 1:
            lower_utility -= 1.96 * stdev(utilities) / sqrt(len(utilities))
    period_start = min((row["as_of"] for row in predictions), default=None)
    period_end = max((row["as_of"] for row in predictions), default=None)
    calibration_error = (
        round(fmean(abs(prediction - outcome) for prediction, outcome in pairs), 6)
        if pairs else None
    )
    metrics = {
        "brier_score": brier_score(pairs),
        "calibration_error": calibration_error,
        "effective_sample_size": min(
            (int(row["effective_sample_size"]) for row in predictions),
            default=0,
        ),
        "oos_sample_size": len(predictions),
        "lower_confidence_net_utility_after_costs": round(lower_utility, 8) if lower_utility is not None else None,
    }
    path_folds = [fold for fold in folds_data if fold["predictions"]]
    fold_returns = [fmean(float(row["net_utility_after_costs"]) for row in fold["predictions"]) for fold in path_folds]
    fold_keys = [fold["test_start"].isoformat() for fold in path_folds]
    path_records = combinatorial_path_records(
        fold_returns, folds=min(5, len(fold_returns)), max_paths=64,
        purge=1, embargo=1,
    ) if len(fold_returns) >= 2 else []
    validation_paths: list[float] = []
    validation_path_records: list[dict[str, Any]] = []
    for index, record in enumerate(path_records):
        selected_folds = [path_folds[fold] for fold in record["test_folds"]]
        test_rows = [row for fold in selected_folds for row in fold["test"]]
        test_start = min(fold["test_start"] for fold in selected_folds)
        test_end = max(fold["test_end"] for fold in selected_folds)
        selected_keys = {row_key(row) for row in test_rows}
        training = [
            row for row in rows
            if row_key(row) not in selected_keys
            and row["feature_available_at"] <= row["as_of"]
            and row["feature_available_at"] <= test_start - purge
            and not any(
                (row["as_of"] <= fold["test_end"] and row["outcome_available_at"] >= fold["test_start"] - purge)
                or (fold["test_end"] < row["as_of"] <= fold["test_end"] + embargo)
                for fold in selected_folds
            )
        ]
        boundaries = {
            row_key(row): fold["test_start"]
            for fold in selected_folds for row in fold["test"]
        }
        path_predictions = evaluate_rows(
            test_rows, training, test_start=test_start, test_end=test_end,
            boundary_by_key=boundaries, path_label=f"cpcv-{index}",
        )
        path_returns = [float(row["net_utility_after_costs"]) for row in path_predictions]
        if not path_returns:
            continue
        path_return = round(fmean(path_returns), 8)
        path_probe = multiple_testing_metrics(path_returns, trials_tested=1, path_returns=[path_return], p_values=[0.5])
        path_p_value = max(0.0, min(1.0, 1.0 - path_probe["psr"]))
        path_metrics = multiple_testing_metrics(
            path_returns, trials_tested=1, path_returns=[path_return], p_values=[path_p_value],
        )
        validation_paths.append(path_return)
        validation_path_records.append({
            **record,
            "path_id": f"cpcv-{index}",
            "test_fold_starts": [fold_keys[fold] for fold in record["test_folds"]],
            "purge_days": record["purge_folds"],
            "embargo_days": record["embargo_folds"],
            "return": path_return,
            "metrics": {
                "sample_size": len(path_returns),
                "mean_return": path_return,
                "positive_count": sum(value > 0 for value in path_returns),
                "psr": path_metrics["psr"],
                "p_value": path_p_value,
                "fit_train_count": len(training),
                "evaluated_test_count": len(path_predictions),
                "domain_valid": True,
            },
        })
    baseline_comparisons = {}
    for name in ("cash", "market", "sector", "trend"):
        paired = [row for row in predictions if row["baseline_returns"].get(name) is not None]
        baseline_comparisons[name] = {
            "sample_count": len(paired),
            "reference": "net_cash_return" if name == "cash" else "gross_benchmark_return",
            "mean_return": round(fmean(float(row["baseline_returns"][name]) for row in paired), 8) if paired else None,
            "mean_stock_net_excess": round(fmean(
                float(row["net_utility_after_costs"]) - float(row["baseline_returns"][name])
                for row in paired
            ), 8) if paired else None,
        }
    artifact = {
        "model_version": MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "cost_model_version": COST_MODEL_VERSION,
        "target_version": TARGET_VERSION,
        "horizon_sessions": TARGET_HORIZON_SESSIONS,
        "target": "positive_return_after_costs",
        "horizons": sorted({row["horizon"] for row in rows}),
        "oos_period_start": period_start.isoformat() if period_start else None,
        "oos_period_end": period_end.isoformat() if period_end else None,
        "calibration_metrics": metrics,
        "baseline_comparisons": baseline_comparisons,
        "cohort_path": max(
            (list(row["cohort_path"]) for row in predictions),
            key=len,
            default=[],
        ),
        "fallback_parent": next(
            (row["fallback_parent"] for row in reversed(predictions) if row["fallback_parent"]),
            None,
        ),
        "sample_windows": [
            _jsonable({**{key: row[key] for key in (
                "ticker", "opportunity_episode_id", "horizon", "horizon_sessions",
                "sample_window_start", "sample_window_end", "outcome_available_at",
            )}, **({"terminal_evidence": row["terminal_evidence"]} if row.get("terminal_evidence") else {})})
            for row in rows
        ],
        "predictions": [_jsonable(row) for row in predictions],
        "validation_paths": validation_paths,
        "validation_path_records": validation_path_records,
        "pit_rejections": pit_rejections[:10_000],
    }
    forecasts = []
    for horizon in sorted({row["horizon"] for row in predictions}):
        horizon_rows = [row for row in predictions if row["horizon"] == horizon]
        probability = round(fmean(float(row["calibrated_probability"]) for row in horizon_rows), 8)
        forecasts.append({
            "horizon": horizon,
            "forecast_value": probability,
            "forecast_distribution": {
                "positive_return_after_costs": probability,
                "non_positive_return_after_costs": round(1.0 - probability, 8),
            },
            "probability_semantics": "P(positive_return_after_costs)",
            "source_prediction_count": len(horizon_rows),
        })
    artifact["forecasts"] = forecasts
    artifact["forecast"] = next((item for item in forecasts if item["horizon"] == "TACTICAL"), forecasts[0] if forecasts else None)
    artifact["artifact_hash"] = content_hash(artifact)
    return artifact


def build_control_results(
    observations: Iterable[Mapping[str, Any]], *, cutoff: datetime, repeats: int = 8,
    min_train: int | None = None, fold_size: int | None = None, min_cohort: int | None = None,
) -> dict[str, Any]:
    """Run the production fold evaluator on two independent control datasets.

    Randomized-label controls change the target labels before fitting. The
    white-noise controls replace both research features and realized price
    returns with deterministic independent draws from the observed scale. Both
    controls execute ``walk_forward`` itself, including fold calibration and
    path generation. Their statistic is the out-of-sample improvement in
    Brier loss over the control generator's known null target probability:
    ``(null_probability - target)**2 - (forecast - target)**2``. A positive value means
    forecast skill remains after the perturbation and fails the zero-tolerance
    control gate. The legacy ``*_returns`` fields carry this prediction score,
    not unchanged stock returns; primary economic returns and costs are separate.
    Full-corpus prevalence and the Gaussian null are used only for falsification,
    never as model fitting inputs or live forecast features.
    """

    if repeats < 2 or repeats > 32:
        raise ValueError("control repeats must be in [2, 32]")
    source_rows = [
        row for row in independent_observations(observations, cutoff=cutoff)
        if row["outcome_available_at"] <= _aware(cutoff)
        and row["feature_available_at"] <= row["as_of"]
    ]
    rows = []
    for raw in source_rows:
        try:
            realized = float(raw.get("realized_return", 0.0))
            cost = float(raw.get("modeled_cost", 0.0))
            label = float(raw.get("outcome"))
        except (TypeError, ValueError):
            continue
        if not all(isfinite(value) for value in (realized, cost, label)):
            continue
        rows.append((realized, cost, 1.0 if label > 0.5 else -1.0))
    if not rows:
        return {"randomized_label_returns": [], "white_noise_market_returns": [], "control_metadata": {}}
    seed = content_hash({"rows": source_rows, "control": MODEL_VERSION})
    generator = Random(seed)
    randomized: list[float] = []
    noise: list[float] = []
    return_values = [row[0] for row in rows]
    scale = stdev(return_values) if len(return_values) > 1 else abs(return_values[0])
    scale = max(scale, 1e-12)
    labels = [row[2] for row in rows]
    randomized_null_probability = fmean(label > 0 for label in labels)
    feature_values = {
        name: [float(dict(raw.get("features") or {}).get(name)) for raw in source_rows if dict(raw.get("features") or {}).get(name) is not None]
        for name in RESEARCH_FEATURES
    }
    feature_scale = {
        name: max(stdev(values) if len(values) > 1 else abs(values[0]), 1e-12)
        for name, values in feature_values.items() if values
    }
    feature_mean = {name: fmean(values) for name, values in feature_values.items() if values}
    run_min_train = min_train if min_train is not None else max(2, min(20, len(rows) // 3))
    run_fold_size = fold_size if fold_size is not None else max(1, min(10, len(rows) // 5))
    run_min_cohort = min_cohort if min_cohort is not None else max(1, min(20, run_min_train))
    metadata: dict[str, Any] = {
        "repeats": repeats,
        "source_sample_count": len(rows),
        "statistic_version": CONTROL_STATISTIC_VERSION,
        "statistic": "(null_probability - target)^2 - (calibrated_probability - target)^2",
        "units": "brier_loss_improvement",
        "null_reference": {
            "scope": "falsification_controls_only",
            "randomized_label_probability": randomized_null_probability,
            "white_noise_return_mean": 0.0,
            "white_noise_return_scale": scale,
            "white_noise_positive_after_cost_probability": "0.5 * erfc(modeled_cost / (return_scale * sqrt(2)))",
        },
        "randomized_label": {"runs": 0, "sample_count": 0, "path_count": 0, "input_hashes": []},
        "white_noise_market": {"runs": 0, "sample_count": 0, "path_count": 0, "input_hashes": []},
    }
    for _ in range(repeats):
        shuffled = list(labels)
        generator.shuffle(shuffled)
        randomized_rows = [dict(raw, outcome=float(label > 0)) for raw, label in zip(source_rows, shuffled)]
        randomized_artifact = walk_forward(
            randomized_rows, cutoff=cutoff, min_train=run_min_train,
            fold_size=run_fold_size, min_cohort=run_min_cohort,
        )
        randomized_values = _control_prediction_scores(
            randomized_artifact, null_probability=lambda _row: randomized_null_probability,
        )
        randomized.extend(randomized_values)
        randomized_meta = metadata["randomized_label"]
        randomized_meta["runs"] += 1
        randomized_meta["sample_count"] += len(randomized_values)
        randomized_meta["path_count"] += len(randomized_artifact.get("validation_paths") or [])
        randomized_meta["input_hashes"].append(content_hash(randomized_rows))

        white_noise_rows: list[dict[str, Any]] = []
        for raw in source_rows:
            features = dict(raw.get("features") or {})
            for name in RESEARCH_FEATURES:
                if name not in feature_mean:
                    continue
                features[name] = generator.gauss(feature_mean[name], feature_scale[name])
            realized = generator.gauss(0.0, scale)
            white_noise_rows.append(dict(
                raw, features=features, realized_return=realized,
                outcome=float(realized > float(raw["modeled_cost"])),
            ))
        white_noise_artifact = walk_forward(
            white_noise_rows, cutoff=cutoff, min_train=run_min_train,
            fold_size=run_fold_size, min_cohort=run_min_cohort,
        )
        white_noise_values = _control_prediction_scores(
            white_noise_artifact,
            null_probability=lambda row: .5 * erfc(float(row["modeled_cost"]) / (scale * sqrt(2.0))),
        )
        noise.extend(white_noise_values)
        noise_meta = metadata["white_noise_market"]
        noise_meta["runs"] += 1
        noise_meta["sample_count"] += len(white_noise_values)
        noise_meta["path_count"] += len(white_noise_artifact.get("validation_paths") or [])
        noise_meta["input_hashes"].append(content_hash(white_noise_rows))
    return {
        "randomized_label_returns": randomized,
        "white_noise_market_returns": noise,
        "control_metadata": metadata,
    }


def _control_prediction_scores(
    artifact: Mapping[str, Any], *, null_probability: Callable[[Mapping[str, Any]], float],
) -> list[float]:
    return [
        (null_probability(row) - float(row["outcome"])) ** 2
        - (float(row["calibrated_probability"]) - float(row["outcome"])) ** 2
        for row in artifact.get("predictions") or []
    ]


def hierarchical_calibration(
    probability: float,
    training: Iterable[Mapping[str, Any]],
    *,
    horizon: str,
    cohort_id: str,
    min_cohort: int = 20,
) -> tuple[float | None, list[str], str | None, int]:
    """Use exact cohort, then horizon, then global evidence without invention."""

    rows = list(training)
    cohorts = (
        (f"cohort:{cohort_id}", [row for row in rows if row["cohort_id"] == cohort_id]),
        (f"horizon:{horizon}", [row for row in rows if row["horizon"] == horizon]),
        ("global", rows),
    )
    path = [name for name, _rows in cohorts]
    for index, (name, candidates) in enumerate(cohorts):
        usable = [row for row in candidates if row.get("outcome") is not None]
        if len(usable) < min_cohort:
            continue
        observed = fmean(float(row["outcome"]) for row in usable)
        calibrated = min(1.0, max(0.0, (probability * len(usable) + observed * min_cohort) / (len(usable) + min_cohort)))
        parent = cohorts[index + 1][0] if index + 1 < len(cohorts) else None
        return round(calibrated, 8), path[:index + 1], parent, len(usable)
    return None, path, None, 0


def content_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def independent_observations(
    observations: Iterable[Mapping[str, Any]], *, cutoff: datetime,
) -> list[dict[str, Any]]:
    """Keep the earliest non-overlapping 20-session windows within each episode."""

    reference = _aware(cutoff)
    rows = [_normalise(row) for row in observations]
    rows = [row for row in rows if row is not None and row["as_of"] <= reference]
    rows.sort(key=lambda row: (row["as_of"], row["ticker"], row["horizon"], content_hash(row)))
    episodes: dict[tuple[str, str], dict[str, Any]] = {}
    independent: list[dict[str, Any]] = []
    for row in rows:
        key = (row["opportunity_episode_id"], row["horizon"])
        previous = episodes.get(key)
        if previous is not None and row["as_of"] == previous["as_of"] and row != previous:
            raise ValueError("conflicting observations for one stock-alpha episode")
        if previous is None or (
            row["as_of"] > previous["as_of"]
            and row["as_of"] >= previous["sample_window_end"]
        ):
            episodes[key] = row
            independent.append(row)
    return independent


def _normalise(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    if raw.get("target_version") != TARGET_VERSION:
        raise ValueError("incompatible stock-alpha target version")
    if str(raw.get("horizon") or "").upper() != "TACTICAL" or raw.get("horizon_sessions") != TARGET_HORIZON_SESSIONS:
        raise ValueError("stock-alpha requires the tactical 20-session target")
    features = dict(raw.get("features") or {})
    score = research_score(features)
    required = {
        "ticker": str(raw.get("ticker") or "").strip().upper(),
        "opportunity_episode_id": str(raw.get("opportunity_episode_id") or "").strip(),
        "horizon": str(raw.get("horizon") or "").strip().upper(),
        "cohort_id": str(raw.get("cohort_id") or "").strip(),
        "as_of": _maybe_aware(raw.get("as_of")),
        "outcome_available_at": _maybe_aware(raw.get("outcome_available_at")),
        "feature_available_at": _maybe_aware(raw.get("feature_available_at")),
        "outcome": _number(raw.get("outcome")),
        "realized_return": _number(raw.get("realized_return")),
        "modeled_cost": _number(raw.get("modeled_cost")),
    }
    if score is None or any(value in {None, ""} for value in required.values()):
        return None
    if not 0.0 <= float(required["outcome"]) <= 1.0 or float(required["modeled_cost"]) < 0:
        return None
    measured_through = raw.get("outcome_measured_through")
    window_end = (
        _maybe_aware(measured_through) if measured_through is not None
        else required["outcome_available_at"]
    )
    if window_end is None or not required["as_of"] < window_end <= required["outcome_available_at"]:
        return None
    return {
        **required, "features": features, "research_score": score,
        "outcome_measured_through": window_end if measured_through is not None else None,
        "sample_window_start": required["as_of"], "sample_window_end": window_end,
        "target_version": TARGET_VERSION, "horizon_sessions": TARGET_HORIZON_SESSIONS,
        "benchmark_membership_hash": raw.get("benchmark_membership_hash"),
        **({"terminal_evidence": dict(raw["terminal_evidence"])} if raw.get("terminal_evidence") else {}),
        "baseline_returns": {
            name: _number(dict(raw.get("baseline_returns") or {}).get(name))
            for name in ("cash", "market", "sector", "trend")
        },
    }


def _probability(score: float) -> float:
    return round(1.0 / (1.0 + exp(-max(-20.0, min(20.0, score)))), 8)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _maybe_aware(value: Any) -> datetime | None:
    try:
        return _aware(value)
    except (TypeError, ValueError):
        return None


def _aware(value: Any) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stock-alpha timestamps must be timezone-aware")
    return parsed.astimezone(UTC)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
