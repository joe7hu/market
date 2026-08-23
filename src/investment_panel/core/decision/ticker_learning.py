"""Deterministic learning and paper-policy promotion gates for ticker decisions."""

from __future__ import annotations

from datetime import date, datetime
from math import sqrt
from statistics import mean, pstdev
from typing import Any, Iterable, Mapping


TICKER_LEARNING_VERSION = "ticker-learning-v1"
MIN_INDEPENDENT_EPISODES = 30
MIN_TRADING_DAYS = 20
MAX_TICKER_CONTRIBUTION = 0.20
MAX_BRIER_SCORE = 0.25


def evaluate_ticker_policy(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Evaluate resolved ticker episodes without changing the active policy.

    Each input row must already represent one unique ticker-decision-horizon
    episode.  The function does not manufacture transaction costs, a trend
    baseline, or a forward/canary split. Missing evidence becomes a promotion
    blocker while the current paper recommendation remains available.
    """

    episodes = [dict(row) for row in rows]
    episodes = [row for row in episodes if str(row.get("state") or "") == "resolved"]
    episodes = _one_episode_per_horizon(episodes)
    episode_keys = {
        (str(row.get("ticker_decision_id") or ""), str(row.get("horizon") or ""))
        for row in episodes
        if row.get("ticker_decision_id") and row.get("horizon")
    }
    trading_days = {
        str(row.get("as_of") or "")[:10]
        for row in episodes
        if row.get("as_of")
    }
    selected_gross = [_number(row.get("selected_return")) for row in episodes]
    selected_gross = [value for value in selected_gross if value is not None]
    stock_gross = [_number(row.get("stock_counterfactual_return")) for row in episodes]
    stock_gross = [value for value in stock_gross if value is not None]
    selected = _metadata_series(episodes, "cost_adjusted_selected_return")
    stock = _metadata_series(episodes, "cost_adjusted_stock_counterfactual_return")
    cash = _metadata_series(episodes, "cost_adjusted_cash_return")
    ticker_positive: dict[str, float] = {}
    for row in episodes:
        value = _number((row.get("metadata") or {}).get("cost_adjusted_selected_return"))
        if value is not None and value > 0:
            ticker = str(row.get("ticker") or "unknown")
            ticker_positive[ticker] = ticker_positive.get(ticker, 0.0) + value
    total_positive = sum(ticker_positive.values())
    max_contribution = max(ticker_positive.values(), default=0.0) / total_positive if total_positive else None
    brier_values = [_brier(row) for row in episodes]
    brier_values = [value for value in brier_values if value is not None]
    paired_counterfactuals = sum(
        1
        for row in episodes
        if _number((row.get("metadata") or {}).get("cost_adjusted_stock_counterfactual_return")) is not None
        and _number((row.get("metadata") or {}).get("cost_adjusted_cash_return")) is not None
    )

    metrics: dict[str, Any] = {
        "version": TICKER_LEARNING_VERSION,
        "independent_episode_count": len(episode_keys),
        "resolved_outcome_rows": len(episodes),
        "trading_day_count": len(trading_days),
        "selected_gross_expectancy": mean(selected_gross) if selected_gross else None,
        "stock_gross_expectancy": mean(stock_gross) if stock_gross else None,
        "selected_net_expectancy": mean(selected) if selected else None,
        "selected_lower_95_net_expectancy": _lower_95(selected),
        "stock_net_expectancy": mean(stock) if stock else None,
        "cash_net_expectancy": mean(cash) if cash else None,
        "trend_net_expectancy": (
            mean(_metadata_series(episodes, "trend_counterfactual_return"))
            if _metadata_series(episodes, "trend_counterfactual_return")
            else None
        ),
        "max_ticker_gain_contribution": max_contribution,
        "brier_score": mean(brier_values) if brier_values else None,
        "calibration_rows": len(brier_values),
        "cost_adjusted_rows": len(selected),
        "cost_adjusted_counterfactual_rows": paired_counterfactuals,
        "cost_adjusted_cash_rows": len(cash),
        "trend_baseline_rows": sum(
            1 for row in episodes if _number((row.get("metadata") or {}).get("trend_counterfactual_return")) is not None
        ),
        "forward_rows": sum(1 for row in episodes if (row.get("metadata") or {}).get("sample") == "forward"),
        "canary_rows": sum(1 for row in episodes if (row.get("metadata") or {}).get("sample") == "canary"),
        "historical_rows": sum(1 for row in episodes if (row.get("metadata") or {}).get("sample") == "historical"),
        "sample_overlap_defects": _sample_overlap_defects(episodes),
        "sample_interval_rows": sum(1 for row in episodes if _sample_interval(row) is not None),
        "purge_embargo_rows": sum(1 for row in episodes if (row.get("metadata") or {}).get("purge_embargo_verified")),
        "delisting_handled_rows": sum(1 for row in episodes if (row.get("metadata") or {}).get("delistings_handled")),
        "sector_slice_rows": sum(
            1 for row in episodes if _valid_slice((row.get("metadata") or {}).get("sector_slice"))
        ),
        "regime_slice_rows": sum(
            1 for row in episodes if _valid_slice((row.get("metadata") or {}).get("regime_slice"))
        ),
        "multiple_trial_correction_rows": sum(1 for row in episodes if (row.get("metadata") or {}).get("multiple_trial_correction")),
        "point_in_time_defects": sum(1 for row in episodes if (row.get("metadata") or {}).get("point_in_time_defect")),
    }
    blockers: list[str] = []
    if metrics["independent_episode_count"] < MIN_INDEPENDENT_EPISODES:
        blockers.append("independent_episode_sample_below_30")
    if metrics["trading_day_count"] < MIN_TRADING_DAYS:
        blockers.append("independent_trading_day_span_below_20")
    if metrics["selected_lower_95_net_expectancy"] is None:
        blockers.append("cost_adjusted_lower_95_expectancy_missing")
    elif metrics["selected_lower_95_net_expectancy"] <= 0:
        blockers.append("cost_adjusted_lower_95_expectancy_not_positive")
    if metrics["cost_adjusted_rows"] < len(episodes):
        blockers.append("cost_adjusted_selected_returns_missing")
    if metrics["cost_adjusted_counterfactual_rows"] < len(episodes):
        blockers.append("cost_adjusted_counterfactuals_missing")
    if (
        metrics["selected_net_expectancy"] is None
        or metrics["cash_net_expectancy"] is None
        or metrics["selected_net_expectancy"] <= metrics["cash_net_expectancy"]
    ):
        blockers.append("not_better_than_cash")
    if (
        metrics["selected_net_expectancy"] is None
        or metrics["stock_net_expectancy"] is None
        or metrics["selected_net_expectancy"] <= metrics["stock_net_expectancy"]
    ):
        blockers.append("not_better_than_stock")
    if metrics["trend_baseline_rows"] < metrics["independent_episode_count"]:
        blockers.append("simple_trend_baseline_missing")
    elif (
        metrics["selected_net_expectancy"] is None
        or metrics["trend_net_expectancy"] is None
        or metrics["selected_net_expectancy"] <= metrics["trend_net_expectancy"]
    ):
        blockers.append("not_better_than_simple_trend")
    if metrics["calibration_rows"] < metrics["independent_episode_count"]:
        blockers.append("probability_calibration_coverage_missing")
    elif metrics["brier_score"] is None or metrics["brier_score"] > MAX_BRIER_SCORE:
        blockers.append("probability_calibration_missing_or_above_0_25")
    if metrics["max_ticker_gain_contribution"] is not None and metrics["max_ticker_gain_contribution"] > MAX_TICKER_CONTRIBUTION:
        blockers.append("ticker_gain_contribution_above_20_percent")
    if metrics["forward_rows"] == 0:
        blockers.append("disjoint_forward_sample_missing")
    if metrics["canary_rows"] == 0:
        blockers.append("disjoint_canary_sample_missing")
    if metrics["historical_rows"] == 0:
        blockers.append("disjoint_historical_sample_missing")
    if metrics["sample_overlap_defects"]:
        blockers.append("historical_forward_canary_overlap")
    if metrics["sample_interval_rows"] < metrics["independent_episode_count"]:
        blockers.append("sample_interval_evidence_missing")
    if metrics["purge_embargo_rows"] < len(episodes):
        blockers.append("purge_and_embargo_evidence_missing")
    if metrics["delisting_handled_rows"] < len(episodes):
        blockers.append("delisting_handling_evidence_missing")
    if metrics["sector_slice_rows"] < len(episodes):
        blockers.append("sector_slice_evidence_missing")
    if metrics["regime_slice_rows"] < len(episodes):
        blockers.append("regime_slice_evidence_missing")
    if metrics["multiple_trial_correction_rows"] < len(episodes):
        blockers.append("repeated_trial_correction_missing")
    if metrics["point_in_time_defects"]:
        blockers.append("point_in_time_defects_unresolved")
    return {
        "status": "eligible" if not blockers else "collecting",
        "paper_only": True,
        "automatic_promotion": not blockers,
        "active_policy_change": "paper_signal_policy_only",
        "blockers": blockers,
        "metrics": metrics,
    }


def _brier(row: Mapping[str, Any]) -> float | None:
    actual_return = _number(row.get("stock_counterfactual_return"))
    if actual_return is None:
        return None
    scenarios = row.get("scenarios") or []
    bull_probability = None
    for scenario in scenarios if isinstance(scenarios, list) else []:
        if str((scenario or {}).get("name") or "").lower() == "bull":
            bull_probability = _number((scenario or {}).get("probability"))
            break
    if bull_probability is None:
        return None
    return (bull_probability - (1.0 if actual_return > 0 else 0.0)) ** 2


def _lower_95(values: list[float]) -> float | None:
    if not values:
        return None
    deviation = pstdev(values) if len(values) > 1 else 0.0
    return mean(values) - 1.645 * deviation / sqrt(len(values))


def _metadata_series(episodes: list[dict[str, Any]], key: str) -> list[float]:
    """Return a complete cost-adjusted series; incomplete series cannot promote."""

    values = [_number((row.get("metadata") or {}).get(key)) for row in episodes]
    return [value for value in values if value is not None]


def _one_episode_per_horizon(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the longest resolved maturity for each decision and horizon."""

    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for row in episodes:
        key = (str(row.get("ticker_decision_id") or ""), str(row.get("horizon") or ""))
        if not key[0] or not key[1]:
            continue
        current = selected.get(key)
        if current is None or _sessions(row) >= _sessions(current):
            selected[key] = row
    return list(selected.values())


def _sample_overlap_defects(episodes: list[dict[str, Any]]) -> int:
    intervals: list[tuple[str, date, date, tuple[str, str]]] = []
    defects = 0
    for row in episodes:
        sample = str((row.get("metadata") or {}).get("sample") or "").strip()
        key = (str(row.get("ticker_decision_id") or ""), str(row.get("horizon") or ""))
        interval = _sample_interval(row)
        if not sample or not key[0] or not key[1] or interval is None:
            continue
        start, end = interval
        for prior_sample, prior_start, prior_end, prior_key in intervals:
            if prior_sample == sample or prior_key == key:
                continue
            if start <= prior_end and prior_start <= end:
                defects += 1
        intervals.append((sample, start, end, key))
    return defects


def _sample_interval(row: Mapping[str, Any]) -> tuple[date, date] | None:
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    start = _calendar_date(metadata.get("sample_start") or row.get("as_of"))
    end = _calendar_date(
        metadata.get("sample_end")
        or row.get("measured_through")
        or row.get("as_of")
    )
    if start is None or end is None:
        return None
    return (min(start, end), max(start, end))


def _calendar_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value in (None, ""):
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed.date()
    except (TypeError, ValueError):
        try:
            return date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            return None


def _sessions(row: Mapping[str, Any]) -> int:
    try:
        return int(row.get("horizon_sessions") or 0)
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _valid_slice(value: Any) -> bool:
    """Reject placeholders as evidence for a required evaluation slice."""

    return str(value or "").strip().lower() not in {"", "unknown", "unclassified", "unavailable", "none"}


__all__ = ["TICKER_LEARNING_VERSION", "evaluate_ticker_policy"]
