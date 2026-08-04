"""Cost-aware counterfactual and promotion metrics for recovery strategies."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import mean, stdev
from typing import Iterable

from investment_panel.core.options_recovery import (
    ExitFill,
    LifecycleResult,
    QuoteCapture,
    executable_exit_price,
    lifecycle_return,
)


@dataclass(frozen=True)
class CounterfactualMetrics:
    return_1_session: float | None
    return_3_session: float | None
    return_5_session: float | None
    return_10_session: float | None
    realized_return: float | None
    exit_efficiency: float | None


def counterfactual_metrics(
    result: LifecycleResult,
    captures: Iterable[QuoteCapture],
) -> CounterfactualMetrics:
    """Measure horizons from executable bid-side marks, never midpoint peaks."""

    if result.entry_fill_price is None or result.entry_fill_at is None:
        return CounterfactualMetrics(None, None, None, None, None, None)
    ordered = [capture for capture in captures if capture.observed_at >= result.entry_fill_at and capture.continuity_ok]
    horizons = {
        horizon: _mark_return(result, ordered, horizon)
        for horizon in (1, 3, 5, 10)
    }
    realized = lifecycle_return(
        entry_price=result.entry_fill_price,
        exits=result.exit_fills,
        quantity=1,
        leg_count=1,
    ) if result.exit_fills and result.filled_quantity == 1 else None
    efficiency = (
        realized / result.executable_peak_return
        if realized is not None and result.executable_peak_return is not None and result.executable_peak_return > 0
        else None
    )
    return CounterfactualMetrics(
        horizons[1], horizons[3], horizons[5], horizons[10], realized, efficiency,
    )


def lower_confidence_bound(values: Iterable[float]) -> float | None:
    sample = [float(value) for value in values]
    if not sample:
        return None
    if len(sample) == 1:
        return sample[0]
    return mean(sample) - 1.96 * stdev(sample) / sqrt(len(sample))


def recovery_promotion_passes(metrics: dict[str, float | int | bool | None]) -> bool:
    """The program's non-negotiable paper-ready promotion thresholds."""

    return bool(
        int(metrics.get("independent_events") or 0) >= 20
        and int(metrics.get("shadow_signals") or 0) >= 100
        and int(metrics.get("paper_fills") or 0) >= 30
        and float(metrics.get("net_expectancy") or 0) > 0
        and float(metrics.get("lower_95_expectancy") or 0) > 0
        and float(metrics.get("calibration_gap") or 1.0) < 0.10
        and float(metrics.get("max_ticker_gain_concentration") or 1.0) <= 0.20
        and not bool(metrics.get("unresolved_defects"))
    )


def _mark_return(result: LifecycleResult, captures: list[QuoteCapture], horizon: int) -> float | None:
    if result.entry_session_number is None:
        return None
    eligible = [
        capture for capture in captures
        if capture.session_number - result.entry_session_number >= horizon and capture.legs
    ]
    if not eligible:
        return None
    capture = eligible[0]
    try:
        exit_price = executable_exit_price(capture.legs)
    except ValueError:
        return None
    return lifecycle_return(
        entry_price=result.entry_fill_price or 0.0,
        exits=[ExitFill(capture.observed_at, 1, exit_price, f"mark_{horizon}", horizon)],
        quantity=1,
        leg_count=len(capture.legs),
    )
