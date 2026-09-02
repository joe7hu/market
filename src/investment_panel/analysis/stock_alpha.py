"""Deterministic point-in-time stock-alpha walk-forward evaluation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from math import exp, isfinite, sqrt
from statistics import fmean, stdev
from typing import Any, Iterable, Mapping

from investment_panel.analysis.stats import brier_score
from investment_panel.analysis.research_validation import combinatorial_path_records


MODEL_VERSION = "ticker-stock-alpha.v2"
FEATURE_VERSION = "daily-trend-v1"
COST_MODEL_VERSION = "stock-cost-slippage.v1"
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
    rows = [_normalise(row) for row in observations]
    rows = [row for row in rows if row is not None and row["as_of"] <= reference]
    rows.sort(key=lambda row: (row["as_of"], row["ticker"], row["horizon"], row["cohort_id"]))
    predictions: list[dict[str, Any]] = []
    for start in range(min_train, len(rows), max(1, fold_size)):
        test = rows[start:start + max(1, fold_size)]
        if not test:
            continue
        test_start = test[0]["as_of"]
        training = [
            row for row in rows[:start]
            if row["outcome_available_at"] <= test_start - purge
            and row["as_of"] + embargo <= test_start
        ]
        test_end = test[-1]["as_of"]
        for row in test:
            if (
                row["outcome_available_at"] > reference
                or row["feature_available_at"] > reference
                or row["as_of"] > test_end
            ):
                continue
            raw_probability = _probability(float(row["research_score"]))
            calibrated, path, parent, effective_sample = hierarchical_calibration(
                raw_probability,
                training,
                horizon=row["horizon"],
                cohort_id=row["cohort_id"],
                min_cohort=min_cohort,
            )
            if calibrated is None:
                continue
            modeled_cost = float(row["modeled_cost"])
            predictions.append({
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
    fold_returns = [
        fmean(float(row["net_utility_after_costs"]) for row in predictions if row["fold_test_start"] == fold)
        for fold in sorted({row["fold_test_start"] for row in predictions})
    ]
    fold_keys = sorted({row["fold_test_start"] for row in predictions})
    path_records = combinatorial_path_records(
        fold_returns, folds=min(5, len(fold_returns)), max_paths=64,
        purge=1, embargo=1,
    ) if len(fold_returns) >= 2 else []
    validation_paths = [
        round(fmean(fold_returns[index] for index in record["test_folds"]), 8)
        for record in path_records
    ]
    validation_path_records = [
        {
            **record,
            "test_fold_starts": [fold_keys[fold] for fold in record["test_folds"]],
            "purge_days": record["purge_folds"],
            "embargo_days": record["embargo_folds"],
            "return": validation_paths[index],
        }
        for index, record in enumerate(path_records)
    ]
    artifact = {
        "model_version": MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "cost_model_version": COST_MODEL_VERSION,
        "target": "positive_return_after_costs",
        "horizons": sorted({row["horizon"] for row in rows}),
        "oos_period_start": period_start.isoformat() if period_start else None,
        "oos_period_end": period_end.isoformat() if period_end else None,
        "calibration_metrics": metrics,
        "cohort_path": max(
            (list(row["cohort_path"]) for row in predictions),
            key=len,
            default=[],
        ),
        "fallback_parent": next(
            (row["fallback_parent"] for row in reversed(predictions) if row["fallback_parent"]),
            None,
        ),
        "predictions": [_jsonable(row) for row in predictions],
        "validation_paths": validation_paths,
        "validation_path_records": validation_path_records,
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


def _normalise(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    features = dict(raw.get("features") or {})
    score = research_score(features)
    required = {
        "ticker": str(raw.get("ticker") or "").strip().upper(),
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
    return {**required, "features": features, "research_score": score}


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
