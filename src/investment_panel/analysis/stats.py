"""Pure statistics helpers for honest validation and calibration (Phase 2).

No scipy — Wilson score intervals, a two-proportion significance test, and
pool-adjacent-violators isotonic regression, all in plain Python so the radar's
learning loop carries no heavy numeric dependency.
"""

from __future__ import annotations

import math

DEFAULT_Z = 1.96  # ~95% two-sided


def wilson_interval(successes: int, n: int, *, z: float = DEFAULT_Z) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion.

    More honest than the normal approximation at small n / extreme p (it never
    leaves [0,1]). Returns ``(lo, hi)``; an empty sample is the maximally
    uncertain ``(0.0, 1.0)``.
    """

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
    """Whether two success rates differ significantly.

    Requires ``min_per_arm`` observations in each arm, then applies a pooled
    two-proportion z-test. Below the sample floor it returns ``False`` (treat as
    insufficient evidence — the caller should gate on this).
    """

    if n1 < min_per_arm or n2 < min_per_arm:
        return False
    p1, p2 = s1 / n1, s2 / n2
    pooled = (s1 + s2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1.0 / n1 + 1.0 / n2))
    if se <= 0:
        return False
    return abs(p1 - p2) / se >= z


def wilson_intervals_overlap(s1: int, n1: int, s2: int, n2: int, *, z: float = DEFAULT_Z) -> bool:
    """True when the two Wilson intervals overlap (i.e. not clearly separated)."""

    lo1, hi1 = wilson_interval(s1, n1, z=z)
    lo2, hi2 = wilson_interval(s2, n2, z=z)
    return not (hi1 < lo2 or hi2 < lo1)


def isotonic_increasing(points: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    """Pool-adjacent-violators isotonic regression producing a non-decreasing map.

    ``points`` are ``(x, y, weight)`` sorted (or sortable) by ``x``; returns the same
    x's with monotone-non-decreasing fitted ``y`` and pooled weight. Used to map
    predicted probabilities to realized hit rates without them ever going backwards.
    """

    if not points:
        return []
    ordered = sorted(points, key=lambda p: p[0])
    # Each block: [sum(weight*y), sum(weight), x_lo, x_hi]. Pool while the previous
    # block's mean exceeds the next (a monotonicity violation).
    blocks: list[list[float]] = []
    for x, y, w in ordered:
        weight = max(w, 1e-9)
        blocks.append([weight * y, weight, x, x])
        while len(blocks) >= 2 and (blocks[-2][0] / blocks[-2][1]) > (blocks[-1][0] / blocks[-1][1]):
            wy2, w2, _lo2, hi2 = blocks.pop()
            wy1, w1, lo1, _hi1 = blocks.pop()
            blocks.append([wy1 + wy2, w1 + w2, lo1, hi2])
    out: list[tuple[float, float, float]] = []
    for x, _y, w in ordered:
        for wy, weight, lo, hi in blocks:
            if lo <= x <= hi:
                out.append((x, round(wy / weight, 6), round(max(w, 1e-9), 6)))
                break
    return out


def apply_calibration_map(predicted: float, calibration: list[tuple[float, float]]) -> float:
    """Map a predicted probability through a calibration table via linear
    interpolation between the nearest ``(predicted, realized)`` anchors. An empty
    table is the identity map (uncalibrated)."""

    if not calibration:
        return max(0.0, min(1.0, predicted))
    pts = sorted(calibration, key=lambda p: p[0])
    if predicted <= pts[0][0]:
        return max(0.0, min(1.0, pts[0][1]))
    if predicted >= pts[-1][0]:
        return max(0.0, min(1.0, pts[-1][1]))
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= predicted <= x1:
            if x1 == x0:
                return max(0.0, min(1.0, y1))
            frac = (predicted - x0) / (x1 - x0)
            return max(0.0, min(1.0, y0 + frac * (y1 - y0)))
    return max(0.0, min(1.0, predicted))


def brier_score(pairs: list[tuple[float, float]]) -> float | None:
    """Mean squared error of predicted probabilities vs binary outcomes (0/1)."""

    clean = [(p, o) for p, o in pairs if p is not None and o is not None]
    if not clean:
        return None
    return round(sum((p - o) ** 2 for p, o in clean) / len(clean), 6)
