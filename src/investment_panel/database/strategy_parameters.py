"""Canonical strategy parameter normalization shared by evaluation and runtime."""

from __future__ import annotations

from copy import deepcopy
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


def merge_strategy_parameters(base: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    """Return one canonical parameter shape with proposed gates applied."""

    merged = deepcopy(dict(base))
    for key in list(merged):
        if canonical_gate_name(str(key)) in EVALUABLE_GATES:
            merged.pop(key)
    gates = normalize_gates(base)
    for key, value in changes.items():
        canonical = canonical_gate_name(str(key))
        if canonical in EVALUABLE_GATES:
            gates[canonical] = value
        else:
            merged[str(key)] = value
    merged["gates"] = gates
    return merged


def _stricter(gate: str, first: Any, second: Any) -> Any:
    try:
        left, right = float(first), float(second)
    except (TypeError, ValueError):
        return second
    if gate in MINIMUM_GATES:
        return first if left >= right else second
    return first if left <= right else second
