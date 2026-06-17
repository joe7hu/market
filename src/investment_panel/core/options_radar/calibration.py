"""Isotonic calibration of predicted P(2x) conviction."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from investment_panel.analysis.stats import (apply_calibration_map, brier_score, isotonic_increasing, wilson_interval)
from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.options_radar.coerce import (_elapsed_days, _json, _number)
from investment_panel.core.options_radar.constants import (DEFAULT_STRATEGY_VERSION)

MIN_CALIBRATION_MATURE_DAYS = 60
# Activation floor for the calibration *flag*. Lowered from 30: per-bin Bayesian
# shrinkage (below) keeps a small-sample map honest, so it no longer takes a hard
# 30-observation cliff before the map does anything useful.
MIN_CALIBRATION_MATURE_OBS = 15
# Pseudo-count strength for shrinking each bin's realized rate toward its predicted
# mean. At n << this the bin map ≈ the model's own prediction (no spurious edge); as
# n grows the empirical rate takes over. This removes the identity-until-N cliff and
# lets calibration improve continuously instead of switching on all at once.
CALIBRATION_PRIOR_STRENGTH = 20.0
CALIBRATION_SUMMARY_BIN = -1


def _shrink(succ: float, n: int, prior_mean: float, *, strength: float = CALIBRATION_PRIOR_STRENGTH) -> float:
    """Beta-style shrinkage of a realized rate toward ``prior_mean`` (the bin's predicted
    probability) with ``strength`` pseudo-observations."""

    return (succ + strength * prior_mean) / (n + strength)


def build_conviction_calibration(
    samples: list[tuple[float, int, int]],
    *,
    bins: int = 10,
    min_mature: int = MIN_CALIBRATION_MATURE_OBS,
) -> tuple[list[dict[str, Any]], list[tuple[float, float]], bool]:
    """Bin predicted P(2x) against realized outcomes and fit a monotone calibration map.

    ``samples`` are ``(predicted_p2x, outcome_2x, outcome_5x)`` over mature events, where
    the outcomes are the *realizable* (trailing-stop) hits, not paper peaks. Returns
    ``(bin_rows, calibration_map, calibrated)``. The map is fit on shrunk per-bin rates so
    it is well-behaved at small n; ``calibrated`` flips True once ``min_mature``
    observations exist (until then the loader keeps the identity map).
    """

    clean = [(p, o2, o5) for p, o2, o5 in samples if p is not None and o2 is not None]
    mature_n = len(clean)
    calibrated = mature_n >= min_mature
    if not clean:
        return [], [], False
    buckets: dict[int, list[tuple[float, int, int]]] = defaultdict(list)
    for p, o2, o5 in clean:
        buckets[min(bins - 1, max(0, int(p * bins)))].append((p, o2, o5))
    bin_rows: list[dict[str, Any]] = []
    map_points: list[tuple[float, float, float]] = []
    for idx in range(bins):
        members = buckets.get(idx, [])
        if not members:
            continue
        n = len(members)
        predicted = sum(m[0] for m in members) / n
        succ2 = sum(m[1] for m in members)
        realized2 = succ2 / n
        realized5 = sum(m[2] for m in members) / n
        shrunk2 = _shrink(succ2, n, predicted)
        lo, hi = wilson_interval(succ2, n)
        bin_rows.append(
            {
                "bin_index": idx,
                "bin_lo": idx / bins,
                "bin_hi": (idx + 1) / bins,
                "n": n,
                "predicted_p2x": round(predicted, 6),
                "realized_p2x": round(realized2, 6),
                "shrunk_p2x": round(shrunk2, 6),
                "realized_p5x": round(realized5, 6),
                "wilson_lo": round(lo, 6),
                "wilson_hi": round(hi, 6),
                "brier": brier_score([(m[0], m[1]) for m in members]),
            }
        )
        # Fit the monotone map on the shrunk rate so a tiny, lucky bin can't claim a huge
        # calibrated lift; isotonic regression then enforces monotonicity across bins.
        map_points.append((predicted, shrunk2, n))
    calibration_map = [(x, y) for x, y, _w in isotonic_increasing(map_points)]
    return bin_rows, calibration_map, calibrated


def refresh_conviction_calibration(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    """Join predicted P(2x) (from candidate_event.raw.ev) to realized mark outcomes for
    mature events and persist calibration bins + the monotone map for ``strategy_version``."""

    rows = query_rows(
        con,
        """
        WITH latest_mark AS (
            SELECT *
            FROM candidate_event_mark
            QUALIFY row_number() OVER (PARTITION BY event_id ORDER BY mark_time DESC) = 1
        )
        SELECT ce.event_id, ce.snapshot_time, ce.raw AS event_raw,
               m.mark_time, m.time_to_2x, m.time_to_5x, m.max_return_since_alert,
               m.raw AS mark_raw
        FROM candidate_event ce
        JOIN latest_mark m ON m.event_id = ce.event_id
        WHERE ce.strategy_version = ?
        """,
        [strategy_version],
    )
    samples: list[tuple[float, int, int]] = []
    for row in rows:
        observed_days = _elapsed_days(row.get("snapshot_time"), row.get("mark_time"))
        if observed_days is None or observed_days < MIN_CALIBRATION_MATURE_DAYS:
            continue
        ev = _json(row.get("event_raw")).get("ev") or {}
        predicted = _number(ev.get("p_2x"))
        if predicted is None:
            continue
        # Calibrate against the realizable trailing-stop exit, falling back to the paper
        # peak only when an older mark predates the realized field. Predicted P(2x) thus
        # learns to mean "probability of an exitable double", not "ever printed 2x".
        realized = _number(_json(row.get("mark_raw")).get("realized_exit_return"))
        basis = realized if realized is not None else (_number(row.get("max_return_since_alert")) or 0.0)
        outcome_2x = 1 if basis >= 1.0 else 0
        outcome_5x = 1 if basis >= 4.0 else 0
        samples.append((predicted, outcome_2x, outcome_5x))

    bin_rows, calibration_map, calibrated = build_conviction_calibration(samples)
    as_of = datetime.now(timezone.utc).isoformat()
    con.execute("DELETE FROM conviction_calibration WHERE strategy_version = ?", [strategy_version])
    written = 0
    for bin_row in bin_rows:
        con.execute(
            """
            INSERT OR REPLACE INTO conviction_calibration
            (strategy_version, bin_index, bin_lo, bin_hi, n, predicted_p2x,
             realized_p2x, realized_p5x, wilson_lo, wilson_hi, brier, mature_n,
             calibrated, as_of, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                strategy_version,
                bin_row["bin_index"],
                bin_row["bin_lo"],
                bin_row["bin_hi"],
                bin_row["n"],
                bin_row["predicted_p2x"],
                bin_row["realized_p2x"],
                bin_row["realized_p5x"],
                bin_row["wilson_lo"],
                bin_row["wilson_hi"],
                bin_row["brier"],
                len(samples),
                calibrated,
                as_of,
                json_dumps({}),
            ],
        )
        written += 1
    # Summary row carries the monotone map and the calibrated flag for the loader.
    con.execute(
        """
        INSERT OR REPLACE INTO conviction_calibration
        (strategy_version, bin_index, mature_n, calibrated, as_of, raw)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            strategy_version,
            CALIBRATION_SUMMARY_BIN,
            len(samples),
            calibrated,
            as_of,
            json_dumps({"calibration_map": calibration_map, "calibrated": calibrated, "mature_n": len(samples)}),
        ],
    )
    return written


def load_conviction_calibration(con: Any, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> dict[str, Any]:
    """Load the stored calibration map + calibrated flag. Empty/identity until enough
    mature observations have accrued."""

    rows = query_rows(
        con,
        "SELECT raw, calibrated FROM conviction_calibration WHERE strategy_version = ? AND bin_index = ?",
        [strategy_version, CALIBRATION_SUMMARY_BIN],
    )
    if not rows:
        return {"calibration_map": [], "calibrated": False}
    raw = _json(rows[0].get("raw"))
    calibrated = bool(raw.get("calibrated"))
    mapping = raw.get("calibration_map") or []
    pairs = [(float(x), float(y)) for x, y in mapping] if calibrated else []
    return {"calibration_map": pairs, "calibrated": calibrated}


def calibrated_p2x(predicted: float | None, calibration: dict[str, Any] | None) -> float | None:
    """Apply a loaded calibration map to a predicted P(2x); identity when uncalibrated."""

    if predicted is None:
        return None
    if not calibration or not calibration.get("calibrated"):
        return max(0.0, min(1.0, predicted))
    return apply_calibration_map(predicted, calibration.get("calibration_map") or [])
