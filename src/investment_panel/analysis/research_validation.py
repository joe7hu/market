"""Deterministic, dependency-free validation primitives for research trials."""

from __future__ import annotations

from itertools import combinations
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
    passed = bool(mechanism_class.strip()) and bool(falsification_rule.strip()) and count > 0
    return {"passed": passed, "domain_valid": count > 0, "mechanism_class": mechanism_class.strip(), "falsification_rule": falsification_rule.strip(), "evidence_count": count, "reason": None if passed else "mechanism_or_falsification_evidence_missing"}


def negative_control(observed: Sequence[float], *, randomized: Sequence[float] = (), white_noise: Sequence[float] = (), tolerance: float = 0.0) -> dict[str, Any]:
    def average(values: Sequence[float]) -> float | None:
        clean = [float(value) for value in values if isfinite(float(value))]
        return fmean(clean) if clean else None

    randomized_values = [float(value) for value in randomized]
    white_noise_values = [float(value) for value in white_noise]
    controls_present = bool(randomized_values) and bool(white_noise_values)
    controls_domain_valid = all(isfinite(value) for value in (*randomized_values, *white_noise_values))
    randomized_edge, noise_edge = average(randomized_values), average(white_noise_values)
    controls = [value for value in (randomized_edge, noise_edge) if value is not None]
    passed = controls_present and controls_domain_valid and len(controls) == 2 and all(value <= tolerance for value in controls)
    reason = None if passed else (
        "negative_controls_missing" if not controls_present else
        "negative_controls_domain_invalid" if not controls_domain_valid else
        "negative_control_positive_edge"
    )
    return {
        "passed": passed, "observed_edge": average(observed),
        "randomized_edge": randomized_edge, "white_noise_edge": noise_edge,
        "randomized_sample_count": len(randomized_values),
        "white_noise_sample_count": len(white_noise_values),
        "tolerance": tolerance, "controls_present": controls_present,
        "domain_valid": controls_domain_valid,
        "persistent_positive_edge": any(value > tolerance for value in controls),
        "reason": reason,
    }


def future_information_trap(*, feature_available_at: Sequence[Any], cutoff: Any, decision_times: Sequence[Any] = ()) -> dict[str, Any]:
    values = list(feature_available_at)
    decisions = list(decision_times)
    if decisions and len(decisions) != len(values):
        return {"passed": False, "domain_valid": False, "future_count": None, "observed_count": len(values), "reason": "feature_decision_domain_invalid"}
    try:
        future_count = sum(
            1 for index, value in enumerate(values)
            if value is None or value > cutoff or (decisions and value > decisions[index])
        )
    except TypeError:
        return {"passed": False, "domain_valid": False, "future_count": None, "observed_count": len(values), "reason": "feature_availability_domain_invalid"}
    passed = bool(values) and future_count == 0
    return {"passed": passed, "domain_valid": bool(values) and len(decisions) in {0, len(values)}, "future_count": future_count, "observed_count": len(values), "reason": None if passed else ("feature_availability_missing" if not values else "future_information_detected")}


def purged_embargoed_splits(observations: Sequence[Mapping[str, Any]], *, purge: int = 1, embargo: int = 1) -> list[dict[str, Any]]:
    if purge < 0 or embargo < 0:
        raise ValueError("purge and embargo must be non-negative")
    rows = sorted(observations, key=lambda row: (str(row.get("as_of") or ""), str(row.get("id") or "")))[:10_000]
    return [
        {"train": rows[: max(0, index - purge)], "test": rows[min(len(rows), index + embargo):]}
        for index in range(1, len(rows))
        if index > purge and index + embargo < len(rows)
    ]


def combinatorial_paths(
    observations: Sequence[Any], *, folds: int = 5, max_paths: int = 64,
    purge: int = 1, embargo: int = 1,
) -> list[tuple[int, ...]]:
    if folds < 2 or max_paths < 1:
        raise ValueError("folds must be at least two and max_paths must be positive")
    if purge < 0 or embargo < 0:
        raise ValueError("purge and embargo must be non-negative")
    count = min(folds, len(observations))
    if count < 2:
        return []
    paths: list[tuple[int, ...]] = []
    # CSCV-style paths are combinations of test folds.  A path is retained
    # only when a purged and embargoed training complement remains.  The
    # inputs are already fold-level observations, so purge and embargo are
    # measured in fold boundaries here; the producer records the underlying
    # time policy in each path record.
    for width in range(1, count):
        for path in combinations(range(count), width):
            blocked = set(path)
            for index in path:
                blocked.update(range(max(0, index - purge), min(count, index + embargo + 1)))
            if len(blocked) == count:
                continue
            paths.append(path)
            if len(paths) >= max_paths:
                return paths
    return paths


def combinatorial_path_records(
    observations: Sequence[Any], *, folds: int = 5, max_paths: int = 64,
    purge: int = 1, embargo: int = 1,
) -> list[dict[str, Any]]:
    """Return bounded CSCV paths with their purged/embargoed train folds."""

    count = min(folds, len(observations))
    paths = combinatorial_paths(
        observations, folds=folds, max_paths=max_paths, purge=purge, embargo=embargo,
    )
    records: list[dict[str, Any]] = []
    for path in paths:
        blocked = set(path)
        for index in path:
            blocked.update(range(max(0, index - purge), min(count, index + embargo + 1)))
        records.append({
            "test_folds": list(path),
            "train_folds": [index for index in range(count) if index not in blocked],
            "purge_folds": purge,
            "embargo_folds": embargo,
        })
    return records


def multiple_testing_metrics(
    returns: Sequence[float], *, trials_tested: int, observations: int | None = None,
    path_returns: Sequence[float] = (), p_values: Sequence[float] = (),
) -> dict[str, Any]:
    """Return bounded PSR/DSR/PBO/FDR values with explicit input domains.

    These are deterministic bounded approximations suitable for a promotion gate. PSR
    is the one-sided normal approximation. DSR discounts the z score by the
    expected maximum of ``trials_tested`` standard normal draws. PBO is the
    observed fraction of supplied combinatorial paths with negative excess return. FDR
    uses the Benjamini-Hochberg step-up value over supplied family p-values;
    missing family p-values invalidate the evidence.
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
    raw_paths = [float(value) for value in path_returns]
    clean_paths = [value for value in raw_paths if isfinite(value)]
    paths_domain_valid = bool(raw_paths) and len(clean_paths) == len(raw_paths)
    # DSR is based on the independent path distribution when supplied.  This
    # prevents post-hoc trade observations from masquerading as independent
    # trials.  The fallback is retained for the small public primitive only;
    # the production walk-forward always supplies real path returns.
    dsr_returns = clean_paths if clean_paths else clean
    dsr_mean = fmean(dsr_returns) if dsr_returns else 0.0
    dsr_volatility = stdev(dsr_returns) if len(dsr_returns) > 1 else 0.0
    dsr_se = dsr_volatility / sqrt(len(dsr_returns)) if dsr_volatility else 0.0
    dsr_z_score = dsr_mean / dsr_se if dsr_se else (12.0 if dsr_mean > 0 else 0.0)
    dsr = 0.5 * (1.0 + erf((dsr_z_score - expected_max) / sqrt(2.0)))
    if clean_paths:
        # The bounded input is a deterministic series of path excess returns.
        # PBO is approximated as the probability that a selected path loses
        # out of sample; callers must provide path-level, not trade-level, data.
        pbo = sum(value < 0 for value in clean_paths) / len(clean_paths)
    else:
        pbo = None
    raw_p_values = [float(value) for value in p_values]
    family_p = [value for value in raw_p_values if isfinite(value) and 0.0 <= value <= 1.0]
    p_values_domain_valid = bool(raw_p_values) and len(family_p) == len(raw_p_values)
    ordered = sorted(family_p)
    bh_values = [(len(ordered) / index) * value for index, value in enumerate(ordered, start=1)]
    fdr_q = min(1.0, min(bh_values)) if bh_values else None
    # The minimum supplied trial p-value is the observed best-trial statistic.
    # Apply the bounded Sidak family-wise approximation across the immutable
    # attempt count. Missing p-values remain invalid evidence.
    best_trial_false_positive = min(family_p) if family_p else None
    snooping = (
        1.0 - (1.0 - best_trial_false_positive) ** trials_tested
        if best_trial_false_positive is not None else None
    )
    observed_count = observations if observations is not None else sample_size
    observations_domain_valid = isinstance(observed_count, int) and observed_count >= sample_size >= 1
    domain_valid = bool(clean) and returns_domain_valid and paths_domain_valid and p_values_domain_valid and observations_domain_valid
    return {
        "sample_size": sample_size, "observations": observed_count,
        "trials_tested": trials_tested, "psr": psr, "dsr": max(0.0, min(1.0, dsr)),
        "pbo": pbo, "data_snooping_probability": snooping,
        "data_snooping_statistic": best_trial_false_positive,
        "fdr_q_value": fdr_q,
        "mean_return": mean_return, "z_score": z_score, "dsr_z_score": dsr_z_score,
        "expected_max_z": expected_max,
        "path_count": len(clean_paths), "p_value_count": len(family_p),
        "paths_domain_valid": paths_domain_valid,
        "p_values_domain_valid": p_values_domain_valid,
        "domain_valid": domain_valid,
        "dsr_reference": "path_returns" if clean_paths else "returns",
    }


def parameter_stability(neighborhood: Sequence[Mapping[str, Any]], *, metric: str = "return", tolerance: float = 0.25) -> dict[str, Any]:
    rows = list(neighborhood)
    values = []
    for row in rows:
        try:
            value = float(row[metric])
        except (KeyError, TypeError, ValueError):
            value = None
        if value is None or not isfinite(value):
            return {"passed": False, "domain_valid": False, "sample_size": 0, "reason": "parameter_neighborhood_missing"}
        values.append(value)
    if len(values) < 3:
        return {"passed": False, "domain_valid": False, "sample_size": len(values), "reason": "parameter_neighborhood_incomplete"}
    center = values[len(values) // 2]
    spread = max(abs(value - center) for value in values)
    passed = center > 0 and spread <= max(abs(center) * tolerance, 1e-12)
    return {"passed": passed, "domain_valid": True, "sample_size": len(values), "center": center, "max_deviation": spread, "reason": None if passed else "parameter_neighborhood_unstable"}


def neutralization(*, gross_returns: Sequence[float], neutralized_returns: Sequence[float]) -> dict[str, Any]:
    gross = fmean([float(value) for value in gross_returns]) if gross_returns else 0.0
    neutral = fmean([float(value) for value in neutralized_returns]) if neutralized_returns else 0.0
    finite = bool(neutralized_returns) and len(gross_returns) == len(neutralized_returns) and all(isfinite(float(value)) for value in neutralized_returns)
    return {"passed": finite, "gross_mean": gross, "neutralized_mean": neutral, "result_exists": bool(neutralized_returns), "domain_valid": finite, "reason": None if finite else "neutralized_result_missing_or_invalid"}


def cost_capacity_stress(*, gross_return: float, base_cost: float, capacity: float = 1.0) -> dict[str, Any]:
    values = (float(gross_return), float(base_cost), float(capacity))
    domain_valid = all(isfinite(value) for value in values) and base_cost >= 0 and capacity > 0
    results = {f"{multiple}x": {"net_return": gross_return - base_cost * multiple, "capacity": capacity / multiple} for multiple in (1, 2, 3)}
    passed = domain_valid and all(item["net_return"] > 0 for item in results.values())
    return {"passed": passed, "domain_valid": domain_valid, "explicit_3x": results["3x"], "multiples": results, "reason": None if passed else ("cost_capacity_domain_invalid" if not domain_valid else "cost_capacity_stress_failed")}


def validate_trial(
    *, mechanism_class: str, falsification_rule: str, observed_returns: Sequence[float],
    randomized_returns: Sequence[float], white_noise_returns: Sequence[float], gross_return: float,
    base_cost: float, neutralized_returns: Sequence[float], parameter_neighborhood: Sequence[Mapping[str, Any]],
    trials_tested: int, feature_available_at: Sequence[Any] = (), decision_times: Sequence[Any] = (), cutoff: Any | None = None,
    expected_members: Sequence[Any] = (), observed_members: Sequence[Any] = (),
    expected_attempts: Sequence[Any] = (), completed_attempts: Sequence[Any] = (),
    path_returns: Sequence[float] = (), path_records: Sequence[Mapping[str, Any]] = (),
    p_values: Sequence[float] = (), policy: Mapping[str, Any] | None = None,
    control_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    policy = dict(policy or {})
    controls = negative_control(
        observed_returns, randomized=randomized_returns, white_noise=white_noise_returns,
        tolerance=float(policy.get("negative_control_tolerance", 0.0)),
    )
    if control_metadata is not None:
        metadata = dict(control_metadata)
        randomized_meta = dict(metadata.get("randomized_label") or {})
        noise_meta = dict(metadata.get("white_noise_market") or {})
        metadata_valid = (
            int(metadata.get("repeats", 0)) >= 2
            and int(randomized_meta.get("runs", 0)) == int(metadata.get("repeats", 0))
            and int(noise_meta.get("runs", 0)) == int(metadata.get("repeats", 0))
            and int(randomized_meta.get("sample_count", 0)) == len(randomized_returns)
            and int(noise_meta.get("sample_count", 0)) == len(white_noise_returns)
            and len(randomized_meta.get("input_hashes") or []) == int(metadata.get("repeats", 0))
            and len(noise_meta.get("input_hashes") or []) == int(metadata.get("repeats", 0))
        )
        controls["control_metadata"] = metadata
        controls["metadata_domain_valid"] = metadata_valid
        controls["domain_valid"] = controls["domain_valid"] and metadata_valid
        controls["passed"] = controls["passed"] and metadata_valid
    metrics = multiple_testing_metrics(observed_returns, trials_tested=trials_tested, observations=len(observed_returns), path_returns=path_returns, p_values=p_values)
    pit = future_information_trap(feature_available_at=feature_available_at, decision_times=decision_times, cutoff=cutoff) if cutoff is not None else {"passed": False, "domain_valid": False, "reason": "cutoff_missing", "future_count": None}
    denominator = {"passed": bool(expected_members) and sorted(map(str, expected_members)) == sorted(map(str, observed_members)), "domain_valid": bool(expected_members), "expected_count": len(expected_members), "observed_count": len(observed_members), "reason": None}
    if not denominator["passed"]:
        denominator["reason"] = "denominator_incomplete"
    attempts = {"passed": bool(expected_attempts) and sorted(map(str, expected_attempts)) == sorted(map(str, completed_attempts)), "domain_valid": bool(expected_attempts), "expected_count": len(expected_attempts), "completed_count": len(completed_attempts), "reason": None}
    if not attempts["passed"]:
        attempts["reason"] = "trial_manifest_incomplete"
    predictive = {"passed": metrics["domain_valid"] and metrics["psr"] >= float(policy.get("min_psr", 0.5)) and (metrics["dsr"] >= float(policy.get("min_dsr", 0.5))), "domain_valid": metrics["domain_valid"], "metrics": metrics, "reason": None}
    stability = parameter_stability(parameter_neighborhood)
    path_records = [dict(record) for record in path_records]
    cpcv = {
        "passed": bool(path_records) and all(
            isinstance(record.get("test_folds"), list)
            and isinstance(record.get("train_folds"), list)
            and set(record["test_folds"]).isdisjoint(record["train_folds"])
            and int((record.get("metrics") or {}).get("evaluated_test_count", 0)) > 0
            and int((record.get("metrics") or {}).get("fit_train_count", 0)) > 0
            for record in path_records
        ),
        "domain_valid": bool(path_records),
        "path_count": len(path_records), "path_records": path_records,
        "reason": None if path_records else "combinatorial_path_evidence_missing",
    }
    robustness_passed = controls["passed"] and stability["passed"] and cpcv["passed"] and (metrics["pbo"] is not None and metrics["pbo"] <= float(policy.get("max_pbo", 0.5)))
    robustness = {"passed": robustness_passed, "domain_valid": controls["domain_valid"] and stability["domain_valid"] and cpcv["domain_valid"], "negative_controls": controls, "parameter_stability": stability, "combinatorial_paths": cpcv, "reason": None if robustness_passed else "falsification_or_robustness_failed"}
    mechanism = mechanism_and_falsification(
        mechanism_class=mechanism_class,
        falsification_rule=falsification_rule,
        evidence=(*path_returns, *randomized_returns, *white_noise_returns),
    )
    neutralized = neutralization(gross_returns=observed_returns, neutralized_returns=neutralized_returns)
    neutralized["sample_size"] = len(neutralized_returns)
    economics = cost_capacity_stress(gross_return=gross_return, base_cost=base_cost)
    economics["passed"] = economics["passed"] and neutralized["passed"]
    robustness["passed"] = robustness["passed"] and mechanism["passed"] and pit["passed"]
    checks = {"mechanism": mechanism, "pit": pit, "denominator": denominator, "attempt_manifest": attempts, "negative_controls": controls, "multiple_testing": metrics, "parameter_stability": stability, "neutralization": neutralized, "cost_capacity": economics, "combinatorial_paths": cpcv, "predictive": predictive, "robustness": robustness}
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
