"""Canonical strategy parameter normalization shared by evaluation and runtime."""

from __future__ import annotations

from copy import deepcopy
from math import isfinite
from typing import Any, Mapping


GATE_ALIASES = {
    "dte_min": "min_dte",
    "dte_max": "max_dte",
    "reject_spread_pct": "max_spread_pct",
    "reject_iv_percentile": "max_iv_percentile",
}
MINIMUM_GATES = {"min_open_interest", "min_volume", "min_dte", "delta_min"}
MAXIMUM_GATES = {
    "max_spread_pct", "max_dte", "delta_max", "max_required_move_pct",
    "max_iv_percentile",
}
EVALUABLE_GATES = MINIMUM_GATES | MAXIMUM_GATES
PARAMETER_FAILURE_VERDICTS = frozenset({"invalid_parameters", "unsupported_parameters",
    "implementation_version_mismatch", "parameter_lineage_mismatch"})


def canonical_gate_name(name: str) -> str:
    return GATE_ALIASES.get(name, name)


def normalize_gates(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Collapse nested/flat aliases, retaining the strictest duplicate gate."""

    normalized: dict[str, Any] = {}
    nested = parameters.get("gates")
    sources = [dict(nested)] if isinstance(nested, Mapping) else []
    sources.append(dict(parameters))
    for source in sources:
        for key, value in source.items():
            canonical = canonical_gate_name(str(key))
            if canonical not in EVALUABLE_GATES or value is None:
                continue
            if canonical not in normalized:
                normalized[canonical] = value
                continue
            normalized[canonical] = _stricter(canonical, normalized[canonical], value)
    return normalized


def _change_items(changes: Mapping[str, Any]) -> list[tuple[str, Any]]:
    nested = changes.get("gates")
    if "gates" in changes and not isinstance(nested, Mapping):
        raise ValueError("invalid strategy gate: gates must be an object")
    return [*(list(nested.items()) if isinstance(nested, Mapping) else []),
            *((str(k), v) for k, v in changes.items() if k != "gates")]


def _validated_gate(key: str, value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid strategy gate: {key} must be a number") from exc
    if isinstance(value, bool) or not isfinite(number) or number < 0:
        raise ValueError(f"invalid strategy gate: {key} must be finite and nonnegative")
    canonical = canonical_gate_name(key)
    if canonical in {"min_dte", "max_dte", "min_volume", "min_open_interest"} and not number.is_integer():
        raise ValueError(f"invalid strategy gate: {key} must be an integer")
    if canonical in {"delta_min", "delta_max", "max_spread_pct"} and number > 1:
        raise ValueError(f"invalid strategy gate: {key} uses fractional units from 0 to 1")
    if canonical == "max_iv_percentile" and number > 100:
        raise ValueError(f"invalid strategy gate: {key} uses percentile units from 0 to 100")
    return number


def merge_strategy_parameters(base: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize typed changes and reject impossible gate combinations.

    Unsupported changes are retained for an explicit capability rejection, never
    silently interpreted as runtime behavior. Preflight owns admission.
    """
    merged = deepcopy(dict(base))
    for key in list(merged):
        if canonical_gate_name(str(key)) in EVALUABLE_GATES:
            merged.pop(key)
    gates = normalize_gates(base)
    proposed: dict[str, Any] = {}
    for key, value in _change_items(changes):
        canonical = canonical_gate_name(str(key))
        if canonical in EVALUABLE_GATES:
            if value is None:
                continue
            _validated_gate(str(key), value)
            proposed[canonical] = _stricter(canonical, proposed[canonical], value) if canonical in proposed else value
        else:
            merged[str(key)] = value
    gates.update(proposed)
    for key, value in gates.items():
        _validated_gate(key, value)
    for minimum, maximum in (("min_dte", "max_dte"), ("delta_min", "delta_max")):
        if gates.get(minimum) is not None and gates.get(maximum) is not None:
            if _validated_gate(minimum, gates[minimum]) > _validated_gate(maximum, gates[maximum]):
                raise ValueError(f"invalid strategy gate: {minimum} exceeds {maximum}")
    merged["gates"] = gates
    return merged


def parameter_preflight(base: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    """Exact configuration failures are different from outcome-data requirements."""
    try:
        merged = merge_strategy_parameters(base, changes)
        capability = mutation_capability(dict(base), dict(changes))
    except (ValueError, TypeError, OverflowError) as error:
        return {"status": "invalid_parameters", "errors": [str(error)], "blocked_parameters": sorted(str(k) for k in changes)}
    verdict = capability["blocking_verdict"]
    return {"status": verdict or "supported", "errors": [],
            "blocked_parameters": capability["blocked_parameters"], "parameters": merged}


def _stricter(gate: str, first: Any, second: Any) -> Any:
    try:
        left, right = float(first), float(second)
    except (TypeError, ValueError):
        return second
    if gate in MINIMUM_GATES:
        return first if left >= right else second
    return first if left <= right else second


_METADATA_CHANGES = {"candidate_note", "filter_reason"}


def mutation_capability(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    base_gates = normalize_gates(base)
    unsupported: list[str] = []
    loosened: list[str] = []
    evaluated = 0
    # Evaluate the normalized duplicate aliases once using their strictest value.
    merged = merge_strategy_parameters(base, changes)
    changed_keys = {canonical_gate_name(str(k)) for k, v in _change_items(changes)
                    if canonical_gate_name(str(k)) in EVALUABLE_GATES and v is not None}
    canonical_changes = {key: merged["gates"][key] for key in sorted(changed_keys)}
    items = [(k, v) for k, v in _change_items(changes) if canonical_gate_name(k) not in EVALUABLE_GATES]
    items.extend(canonical_changes.items())
    for key, value in items:
        if key in _METADATA_CHANGES:
            continue
        canonical = canonical_gate_name(key)
        if canonical not in EVALUABLE_GATES:
            unsupported.append(key)
            continue
        if value is None:
            continue
        _validated_gate(key, value)
        evaluated += 1
        baseline = base_gates.get(canonical)
        if baseline is None:
            loosened.append(key)
            continue
        if canonical in MINIMUM_GATES and float(value) < float(baseline):
            loosened.append(key)
        if canonical in MAXIMUM_GATES and float(value) > float(baseline):
            loosened.append(key)
    if unsupported or evaluated == 0:
        return {
            "blocking_verdict": "unsupported_parameters",
            "blocked_parameters": unsupported or sorted(changes),
        }
    if loosened:
        return {
            "blocking_verdict": "requires_rejected_or_shadow_outcomes",
            "blocked_parameters": loosened,
        }
    return {"blocking_verdict": None, "blocked_parameters": []}
