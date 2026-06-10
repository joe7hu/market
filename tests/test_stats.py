"""Pure stats helpers for Phase 2 validation/calibration."""

from __future__ import annotations

import pytest

from investment_panel.analysis.stats import (
    apply_calibration_map,
    brier_score,
    isotonic_increasing,
    two_proportion_significant,
    wilson_interval,
    wilson_intervals_overlap,
)


def test_wilson_interval_brackets_proportion_and_stays_in_unit():
    lo, hi = wilson_interval(8, 10)
    assert 0.0 <= lo < 0.8 < hi <= 1.0
    # Empty sample -> maximally uncertain.
    assert wilson_interval(0, 0) == (0.0, 1.0)
    # Extreme p never escapes [0,1].
    lo, hi = wilson_interval(10, 10)
    assert lo >= 0.0 and hi <= 1.0


def test_wilson_narrows_with_more_data():
    lo_small, hi_small = wilson_interval(7, 10)
    lo_big, hi_big = wilson_interval(700, 1000)
    assert (hi_big - lo_big) < (hi_small - hi_small + (hi_small - lo_small))  # big interval narrower
    assert (hi_big - lo_big) < (hi_small - lo_small)


def test_two_proportion_significance_respects_sample_floor():
    # Big, clearly separated arms -> significant.
    assert two_proportion_significant(80, 100, 30, 100) is True
    # Same rates -> not significant.
    assert two_proportion_significant(50, 100, 50, 100) is False
    # Below the per-arm floor -> insufficient evidence regardless of separation.
    assert two_proportion_significant(5, 5, 0, 5) is False


def test_wilson_overlap():
    assert wilson_intervals_overlap(50, 100, 52, 100) is True
    assert wilson_intervals_overlap(90, 100, 10, 100) is False


def test_isotonic_enforces_monotonic_nondecreasing():
    # A dip in the middle (0.5 predicts lower realized than 0.3) gets pooled away.
    raw = [(0.1, 0.05, 10), (0.3, 0.6, 10), (0.5, 0.4, 10), (0.7, 0.8, 10)]
    fitted = isotonic_increasing(raw)
    ys = [y for _x, y, _w in fitted]
    assert ys == sorted(ys)  # non-decreasing
    # The 0.3 and 0.5 violators pool to their shared mean (0.5).
    assert fitted[1][1] == fitted[2][1] == pytest.approx(0.5)


def test_apply_calibration_map_identity_and_interpolation():
    assert apply_calibration_map(0.4, []) == 0.4  # empty table = identity
    table = [(0.2, 0.1), (0.6, 0.5)]
    assert apply_calibration_map(0.4, table) == pytest.approx(0.3)  # midpoint interpolation
    assert apply_calibration_map(0.1, table) == pytest.approx(0.1)  # clamps to first anchor
    assert apply_calibration_map(0.9, table) == pytest.approx(0.5)  # clamps to last anchor


def test_brier_score():
    assert brier_score([]) is None
    assert brier_score([(1.0, 1.0), (0.0, 0.0)]) == 0.0
    assert brier_score([(0.5, 1.0), (0.5, 0.0)]) == pytest.approx(0.25)
