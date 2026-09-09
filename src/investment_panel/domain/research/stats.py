"""Pure statistics helpers for honest validation and calibration."""

from __future__ import annotations

import math


DEFAULT_Z = 1.96


def wilson_interval(successes: int, n: int, *, z: float = DEFAULT_Z) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    successes = max(0, min(successes, n))
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def two_proportion_significant(
    s1: int, n1: int, s2: int, n2: int, *, z: float = DEFAULT_Z, min_per_arm: int = 20
) -> bool:
    if n1 < min_per_arm or n2 < min_per_arm:
        return False
    p1, p2 = s1 / n1, s2 / n2
    pooled = (s1 + s2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1.0 / n1 + 1.0 / n2))
    if se <= 0:
        return False
    return abs(p1 - p2) / se >= z


def wilson_intervals_overlap(s1: int, n1: int, s2: int, n2: int, *, z: float = DEFAULT_Z) -> bool:
    lo1, hi1 = wilson_interval(s1, n1, z=z)
    lo2, hi2 = wilson_interval(s2, n2, z=z)
    return not (hi1 < lo2 or hi2 < lo1)


def isotonic_increasing(points: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    if not points:
        return []
    ordered = sorted(points, key=lambda point: point[0])
    blocks: list[list[float]] = []
    for x, y, weight in ordered:
        weight = max(weight, 1e-9)
        blocks.append([weight * y, weight, x, x])
        while len(blocks) >= 2 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            wy2, w2, _lo2, hi2 = blocks.pop()
            wy1, w1, lo1, _hi1 = blocks.pop()
            blocks.append([wy1 + wy2, w1 + w2, lo1, hi2])
    output: list[tuple[float, float, float]] = []
    for x, _y, weight in ordered:
        for weighted_y, total_weight, lo, hi in blocks:
            if lo <= x <= hi:
                output.append((x, round(weighted_y / total_weight, 6), round(max(weight, 1e-9), 6)))
                break
    return output


def apply_calibration_map(predicted: float, calibration: list[tuple[float, float]]) -> float:
    if not calibration:
        return max(0.0, min(1.0, predicted))
    points = sorted(calibration, key=lambda point: point[0])
    if predicted <= points[0][0]:
        return max(0.0, min(1.0, points[0][1]))
    if predicted >= points[-1][0]:
        return max(0.0, min(1.0, points[-1][1]))
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if x0 <= predicted <= x1:
            if x1 == x0:
                return max(0.0, min(1.0, y1))
            fraction = (predicted - x0) / (x1 - x0)
            return max(0.0, min(1.0, y0 + fraction * (y1 - y0)))
    return max(0.0, min(1.0, predicted))


def brier_score(pairs: list[tuple[float, float]]) -> float | None:
    clean = [(prediction, outcome) for prediction, outcome in pairs if prediction is not None and outcome is not None]
    if not clean:
        return None
    return round(sum((prediction - outcome) ** 2 for prediction, outcome in clean) / len(clean), 6)
