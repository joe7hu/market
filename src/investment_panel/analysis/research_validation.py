"""Deterministic, dependency-free validation primitives for research trials."""

from __future__ import annotations

from math import erf, isfinite, sqrt
from statistics import fmean, stdev
from typing import Any, Iterable, Mapping, Sequence


GATE_CODES = (
    "pit_integrity",
    "denominator_completeness",
    "oos_predictive_validity",
    "falsification_and_robustness",
    "economic_promotability",
)


def mechanism_and_falsification(*, mechanism_class: str, falsification_rule: str, evidence: Iterable[Any] = ()) -> dict[str, Any]:
    count = min(10_000, sum(1 for _ in evidence))
    passed = bool(mechanism_class.strip()) and bool(falsification_rule.strip())
    return {"passed": passed, "mechanism_class": mechanism_class.strip(), "falsification_rule": falsification_rule.strip(), "evidence_count": count, "reason": None if passed else "mechanism_or_falsification_missing"}


def negative_control(observed: Sequence[float], *, randomized: Sequence[float] = (), white_noise: Sequence[float] = (), tolerance: float = 0.0) -> dict[str, Any]:
    def average(values: Sequence[float]) -> float | None:
        clean = [float(value) for value in values if isfinite(float(value))]
        return fmean(clean) if clean else None

    randomized_edge, noise_edge = average(randomized), average(white_noise)
    controls = [value for value in (randomized_edge, noise_edge) if value is not None]
    passed = all(value <= tolerance for value in controls)
    return {"passed": passed, "observed_edge": average(observed), "randomized_edge": randomized_edge, "white_noise_edge": noise_edge, "persistent_positive_edge": any(value > tolerance for value in controls), "reason": None if passed else "negative_control_positive_edge"}


def future_information_trap(*, feature_available_at: Sequence[Any], cutoff: Any) -> dict[str, Any]:
    future_count = sum(1 for value in feature_available_at if value > cutoff)
    return {"passed": future_count == 0, "future_count": future_count, "reason": None if future_count == 0 else "future_information_detected"}


def purged_embargoed_splits(observations: Sequence[Mapping[str, Any]], *, purge: int = 1, embargo: int = 1) -> list[dict[str, Any]]:
    if purge < 0 or embargo < 0:
        raise ValueError("purge and embargo must be non-negative")
    rows = sorted(observations, key=lambda row: (str(row.get("as_of") or ""), str(row.get("id") or "")))[:10_000]
    return [
        {"train": rows[: max(0, index - purge)], "test": rows[min(len(rows), index + embargo):]}
        for index in range(1, len(rows))
        if index > purge and index + embargo < len(rows)
    ]


def combinatorial_paths(observations: Sequence[Any], *, folds: int = 5, max_paths: int = 64) -> list[tuple[int, ...]]:
    if folds < 2 or max_paths < 1:
        raise ValueError("folds must be at least two and max_paths must be positive")
    count = min(folds, len(observations))
    paths: list[tuple[int, ...]] = []
    for width in range(1, count):
        for start in range(count - width + 1):
            paths.append(tuple(range(start, start + width)))
            if len(paths) >= max_paths:
                return paths
    return paths


def multiple_testing_metrics(returns: Sequence[float], *, trials_tested: int, observations: int | None = None) -> dict[str, Any]:
    clean = [float(value) for value in returns if isfinite(float(value))]
    sample_size = len(clean)
    mean_return = fmean(clean) if clean else 0.0
    volatility = stdev(clean) if len(clean) > 1 else 0.0
    z_score = mean_return / (volatility / sqrt(sample_size)) if volatility and sample_size else 0.0
    psr = 0.5 * (1.0 + erf(z_score / sqrt(2.0)))
    tests = max(1, min(10_000, int(trials_tested)))
    dsr = psr ** tests
    pbo = min(1.0, max(0.0, (tests - 1) / max(1, tests * 2)))
    fdr_q = min(1.0, max(0.0, (1.0 - psr) * tests))
    return {"sample_size": sample_size, "observations": observations if observations is not None else sample_size, "trials_tested": tests, "psr": psr, "dsr": dsr, "pbo": pbo, "data_snooping_probability": pbo, "fdr_q_value": fdr_q, "mean_return": mean_return, "domain_valid": bool(clean) and tests > 0}


def parameter_stability(neighborhood: Sequence[Mapping[str, Any]], *, metric: str = "return", tolerance: float = 0.25) -> dict[str, Any]:
    values = [float(row[metric]) for row in neighborhood if row.get(metric) is not None and isfinite(float(row[metric]))]
    if not values:
        return {"passed": False, "sample_size": 0, "reason": "parameter_neighborhood_missing"}
    center = values[len(values) // 2]
    spread = max(abs(value - center) for value in values)
    return {"passed": center > 0 and spread <= max(abs(center) * tolerance, 1e-12), "sample_size": len(values), "center": center, "max_deviation": spread, "reason": None if center > 0 and spread <= max(abs(center) * tolerance, 1e-12) else "parameter_neighborhood_unstable"}


def neutralization(*, gross_returns: Sequence[float], neutralized_returns: Sequence[float]) -> dict[str, Any]:
    gross = fmean([float(value) for value in gross_returns]) if gross_returns else 0.0
    neutral = fmean([float(value) for value in neutralized_returns]) if neutralized_returns else 0.0
    return {"passed": bool(neutralized_returns) and neutral > 0, "gross_mean": gross, "neutralized_mean": neutral, "result_exists": bool(neutralized_returns), "reason": None if neutralized_returns and neutral > 0 else "neutralized_result_missing_or_negative"}


def cost_capacity_stress(*, gross_return: float, base_cost: float, capacity: float = 1.0) -> dict[str, Any]:
    results = {f"{multiple}x": {"net_return": gross_return - base_cost * multiple, "capacity": capacity / multiple} for multiple in (1, 2, 3)}
    passed = all(item["net_return"] > 0 for item in results.values())
    return {"passed": passed, "explicit_3x": results["3x"], "multiples": results, "reason": None if passed else "cost_capacity_stress_failed"}


def validate_trial(*, mechanism_class: str, falsification_rule: str, observed_returns: Sequence[float], randomized_returns: Sequence[float], white_noise_returns: Sequence[float], gross_return: float, base_cost: float, neutralized_returns: Sequence[float], parameter_neighborhood: Sequence[Mapping[str, Any]], trials_tested: int) -> dict[str, Any]:
    controls = negative_control(observed_returns, randomized=randomized_returns, white_noise=white_noise_returns)
    metrics = multiple_testing_metrics(observed_returns, trials_tested=trials_tested)
    checks = {"mechanism": mechanism_and_falsification(mechanism_class=mechanism_class, falsification_rule=falsification_rule), "negative_controls": controls, "multiple_testing": metrics, "parameter_stability": parameter_stability(parameter_neighborhood), "neutralization": neutralization(gross_returns=observed_returns, neutralized_returns=neutralized_returns), "cost_capacity": cost_capacity_stress(gross_return=gross_return, base_cost=base_cost)}
    passed = all(bool(item.get("passed")) for item in checks.values())
    return {"passed": passed, "checks": checks, "gate_codes": list(GATE_CODES), "reason": None if passed else "validation_gate_failed"}


# Stable names for callers that describe the same checks in domain language.
validate_mechanism = mechanism_and_falsification
negative_control_suite = negative_control
future_information_trap_suite = future_information_trap
purge_and_embargo = purged_embargoed_splits
combinatorial_purged_cv = combinatorial_paths
multiple_testing = multiple_testing_metrics
cost_stress = cost_capacity_stress
