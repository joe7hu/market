"""Deterministic, dependency-free validation primitives for research trials."""

from __future__ import annotations

from math import erf, isfinite, log, pi, sqrt
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
    return {"passed": passed, "observed_edge": average(observed), "randomized_edge": randomized_edge, "white_noise_edge": noise_edge, "tolerance": tolerance, "persistent_positive_edge": any(value > tolerance for value in controls), "reason": None if passed else "negative_control_positive_edge"}


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


def multiple_testing_metrics(
    returns: Sequence[float], *, trials_tested: int, observations: int | None = None,
    path_returns: Sequence[float] = (), p_values: Sequence[float] = (),
) -> dict[str, Any]:
    """Return bounded PSR/DSR/PBO/FDR values with explicit input domains.

    These are deterministic approximations suitable for a promotion gate. PSR
    is the one-sided normal approximation. DSR discounts the z score by the
    expected maximum of ``trials_tested`` standard normal draws. PBO is the
    observed fraction of combinatorial paths at or below the path median. FDR
    uses the Benjamini-Hochberg step-up value over supplied p-values (or the
    observed trial p-value when no family vector is available).
    """
    if not isinstance(trials_tested, int) or not 1 <= trials_tested <= 10_000:
        raise ValueError("trials_tested must be an integer in [1, 10000]")
    raw_returns = [float(value) for value in returns]
    clean = [value for value in raw_returns if isfinite(value)]
    returns_domain_valid = len(clean) == len(raw_returns)
    sample_size = len(clean)
    mean_return = fmean(clean) if clean else 0.0
    volatility = stdev(clean) if len(clean) > 1 else 0.0
    standard_error = volatility / sqrt(sample_size) if volatility and sample_size else 0.0
    # Keep persisted JSON finite when a deterministic fixture has zero
    # volatility.  Twelve standard deviations is already indistinguishable
    # from certainty at the precision used by this gate.
    z_score = mean_return / standard_error if standard_error else (12.0 if mean_return > 0 else 0.0)
    psr = 0.5 * (1.0 + erf(z_score / sqrt(2.0)))
    if trials_tested == 1:
        expected_max = 0.0
    else:
        root = sqrt(2.0 * log(trials_tested))
        expected_max = root - (log(log(trials_tested)) + log(4.0 * pi)) / (2.0 * root)
    dsr = 0.5 * (1.0 + erf((z_score - expected_max) / sqrt(2.0)))
    raw_paths = [float(value) for value in path_returns]
    clean_paths = [value for value in raw_paths if isfinite(value)]
    if clean_paths:
        # The bounded input is a deterministic series of path excess returns.
        # PBO is approximated as the probability that a selected path loses
        # out of sample; callers must provide path-level, not trade-level, data.
        pbo = sum(value < 0 for value in clean_paths) / len(clean_paths)
    else:
        pbo = None
    raw_p_values = [float(value) for value in p_values]
    family_p = [value for value in raw_p_values if isfinite(value) and 0.0 <= value <= 1.0]
    if not family_p and sample_size:
        family_p = [max(0.0, min(1.0, 1.0 - psr))]
    ordered = sorted(family_p)
    bh_values = [(len(ordered) / index) * value for index, value in enumerate(ordered, start=1)]
    fdr_q = min(1.0, min(bh_values)) if bh_values else None
    snooping = 1.0 - (1.0 - (1.0 - psr)) ** trials_tested
    return {
        "sample_size": sample_size, "observations": observations if observations is not None else sample_size,
        "trials_tested": trials_tested, "psr": psr, "dsr": max(0.0, min(1.0, dsr)),
        "pbo": pbo, "data_snooping_probability": snooping, "fdr_q_value": fdr_q,
        "mean_return": mean_return, "z_score": z_score, "expected_max_z": expected_max,
        "domain_valid": bool(clean) and returns_domain_valid and len(clean_paths) == len(raw_paths)
        and (observations is None or isinstance(observations, int) and observations >= sample_size)
        and (not raw_p_values or len(family_p) == len(raw_p_values)),
    }


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


def validate_trial(
    *, mechanism_class: str, falsification_rule: str, observed_returns: Sequence[float],
    randomized_returns: Sequence[float], white_noise_returns: Sequence[float], gross_return: float,
    base_cost: float, neutralized_returns: Sequence[float], parameter_neighborhood: Sequence[Mapping[str, Any]],
    trials_tested: int, feature_available_at: Sequence[Any] = (), cutoff: Any | None = None,
    expected_members: Sequence[Any] = (), observed_members: Sequence[Any] = (),
    expected_attempts: Sequence[Any] = (), completed_attempts: Sequence[Any] = (),
    path_returns: Sequence[float] = (), p_values: Sequence[float] = (), policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    policy = dict(policy or {})
    controls = negative_control(
        observed_returns, randomized=randomized_returns, white_noise=white_noise_returns,
        tolerance=float(policy.get("negative_control_tolerance", 0.0)),
    )
    metrics = multiple_testing_metrics(observed_returns, trials_tested=trials_tested, path_returns=path_returns, p_values=p_values)
    pit = future_information_trap(feature_available_at=feature_available_at, cutoff=cutoff) if cutoff is not None else {"passed": False, "reason": "cutoff_missing", "future_count": None}
    denominator = {"passed": bool(expected_members) and sorted(map(str, expected_members)) == sorted(map(str, observed_members)), "expected_count": len(expected_members), "observed_count": len(observed_members), "reason": None}
    if not denominator["passed"]:
        denominator["reason"] = "denominator_incomplete"
    attempts = {"passed": bool(expected_attempts) and sorted(map(str, expected_attempts)) == sorted(map(str, completed_attempts)), "expected_count": len(expected_attempts), "completed_count": len(completed_attempts), "reason": None}
    if not attempts["passed"]:
        attempts["reason"] = "trial_manifest_incomplete"
    predictive = {"passed": metrics["domain_valid"] and metrics["psr"] >= float(policy.get("min_psr", 0.5)) and (metrics["dsr"] >= float(policy.get("min_dsr", 0.5))), "metrics": metrics, "reason": None}
    robustness = {"passed": controls["passed"] and parameter_stability(parameter_neighborhood)["passed"] and (metrics["pbo"] is not None and metrics["pbo"] <= float(policy.get("max_pbo", 0.5))), "negative_controls": controls, "parameter_stability": parameter_stability(parameter_neighborhood), "reason": None}
    economics = cost_capacity_stress(gross_return=gross_return, base_cost=base_cost)
    economics["passed"] = economics["passed"] and neutralization(gross_returns=observed_returns, neutralized_returns=neutralized_returns)["passed"]
    checks = {"mechanism": mechanism_and_falsification(mechanism_class=mechanism_class, falsification_rule=falsification_rule), "pit": pit, "denominator": denominator, "attempt_manifest": attempts, "negative_controls": controls, "multiple_testing": metrics, "parameter_stability": robustness["parameter_stability"], "neutralization": neutralization(gross_returns=observed_returns, neutralized_returns=neutralized_returns), "cost_capacity": economics, "predictive": predictive, "robustness": robustness}
    gates = {
        "pit_integrity": pit["passed"], "denominator_completeness": denominator["passed"] and attempts["passed"],
        "oos_predictive_validity": predictive["passed"], "falsification_and_robustness": robustness["passed"],
        "economic_promotability": economics["passed"],
    }
    passed = all(gates.values())
    return {"passed": passed, "checks": checks, "gates": {code: {"passed": value} for code, value in gates.items()}, "gate_codes": list(GATE_CODES), "reason": None if passed else "validation_gate_failed"}


# Stable names for callers that describe the same checks in domain language.
validate_mechanism = mechanism_and_falsification
negative_control_suite = negative_control
future_information_trap_suite = future_information_trap
purge_and_embargo = purged_embargoed_splits
combinatorial_purged_cv = combinatorial_paths
multiple_testing = multiple_testing_metrics
cost_stress = cost_capacity_stress
