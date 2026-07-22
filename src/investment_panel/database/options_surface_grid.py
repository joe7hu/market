"""Bounded provider-IV surface grid projection for the history repository."""

from __future__ import annotations

from typing import Any, Sequence


def surface_grid_payload(
    *, symbol: str, snapshot_id: int, rows: Sequence[dict[str, Any]], option_types: Sequence[str]
) -> dict[str, Any]:
    x_values = sorted({float(row["log_moneyness"]) for row in rows if row.get("log_moneyness") is not None})
    y_values = sorted({int(row["dte"]) for row in rows})
    surfaces: dict[str, list[list[float | None]]] = {}
    for kind in option_types:
        grouped = {
            (int(row["dte"]), float(row["log_moneyness"])): row.get("provider_iv")
            for row in rows
            if row["option_type"] == kind
            and row.get("provider_iv") is not None
            and row.get("log_moneyness") is not None
        }
        surfaces[kind] = [
            [_interpolate([(x, iv) for (dte, x), iv in grouped.items() if dte == y], point) for point in x_values]
            for y in y_values
        ]
    observed = [
        {key: row.get(key) for key in ("expiration", "option_type", "dte", "log_moneyness", "provider_iv", "strike")}
        for row in rows
        if row.get("provider_iv") is not None
    ]
    return {
        "snapshot_id": snapshot_id,
        "symbol": symbol.upper(),
        "x": x_values,
        "y": y_values,
        "surfaces": surfaces,
        "observed": observed,
    }


def _interpolate(points: Sequence[tuple[float, Any]], x: float) -> float | None:
    cleaned = sorted((float(point_x), float(value)) for point_x, value in points if value is not None)
    if not cleaned or x < cleaned[0][0] or x > cleaned[-1][0]:
        return None
    for left, right in zip(cleaned, cleaned[1:]):
        if left[0] <= x <= right[0]:
            if right[0] == left[0]:
                return left[1]
            return left[1] + (right[1] - left[1]) * ((x - left[0]) / (right[0] - left[0]))
    return cleaned[0][1] if x == cleaned[0][0] else cleaned[-1][1]
