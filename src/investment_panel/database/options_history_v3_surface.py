"""Same-capture surface shape metrics for history-v3."""

from __future__ import annotations

from math import isfinite
from statistics import mean
from typing import Any

from investment_panel.analysis.history_v3 import MIN_ELIGIBLE_POINTS, eligible_rows


def nearest_delta_iv(rows: list[dict[str, Any]]) -> float | None:
    eligible = [
        row for row in rows
        if row.get("provider_delta") is not None and row.get("provider_iv") is not None
    ]
    if not eligible:
        return None
    nearest = min(eligible, key=lambda row: abs(abs(float(row["provider_delta"])) - 0.25))
    return _finite_float(nearest["provider_iv"])


def surface_shape_metrics(
    grouped: dict[tuple[Any, str], list[dict[str, Any]]],
    coherent_spots: dict[tuple[Any, str], float | None],
    *,
    minimum_points: int = MIN_ELIGIBLE_POINTS,
) -> dict[tuple[Any, str], dict[str, float | None]]:
    """Derive same-capture 25-delta skew and per-type term slopes."""
    output: dict[tuple[Any, str], dict[str, float | None]] = {}
    points_by_type: dict[str, dict[int, list[tuple[tuple[Any, str], float]]]] = {}
    for key, rows in grouped.items():
        spot = coherent_spots.get(key)
        quality_rows, _blockers, _metrics = eligible_rows(
            rows, spot=spot, option_type=key[1],
        )
        if spot is None or len(quality_rows) < minimum_points:
            output[key] = {"skew_25": None, "term_slope": None}
            continue
        nearest = min(quality_rows, key=lambda row: abs(float(row["strike"]) - spot))
        atm_iv = _finite_float(nearest.get("provider_iv")) if spot is not None else None
        delta_iv = nearest_delta_iv(quality_rows)
        skew = delta_iv - atm_iv if delta_iv is not None and atm_iv is not None else None
        output[key] = {"skew_25": skew, "term_slope": None}
        if atm_iv is not None and atm_iv > 0:
            dte = int(nearest["dte"])
            points_by_type.setdefault(key[1], {}).setdefault(dte, []).append((key, atm_iv))
    for by_dte in points_by_type.values():
        curve = sorted((dte, mean(value for _key, value in values)) for dte, values in by_dte.items())
        for index, (dte, _atm_iv) in enumerate(curve):
            neighbors = _slope_neighbors(curve, index)
            slope = _curve_slope(*neighbors) if neighbors else None
            for key, _value in by_dte[dte]:
                output[key]["term_slope"] = slope
    return output


def _slope_neighbors(
    curve: list[tuple[int, float]], index: int,
) -> tuple[tuple[int, float], tuple[int, float]] | None:
    if len(curve) < 2:
        return None
    if index == 0:
        return curve[0], curve[1]
    if index == len(curve) - 1:
        return curve[-2], curve[-1]
    return curve[index - 1], curve[index + 1]


def _curve_slope(left: tuple[int, float], right: tuple[int, float]) -> float | None:
    width = right[0] - left[0]
    return (right[1] - left[1]) / width if width > 0 else None


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None
