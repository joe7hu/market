"""Hypothetical outcomes, walk-forward folds and significance for the strategy lab."""

from __future__ import annotations

from typing import Any

from investment_panel.analysis.stats import (two_proportion_significant, wilson_interval)
from investment_panel.core.db import (query_rows)
from investment_panel.core.options_radar.candidates import (build_candidate_event)
from investment_panel.core.options_radar.coerce import (_average, _elapsed_days, _elapsed_hours, _integer, _iso, _json_or_list, _median, _number)
from investment_panel.core.options_radar.constants import (MIN_FORWARD_TEST_DAYS)
from investment_panel.core.options_radar.regime import (_qqq_above_200d)
from investment_panel.core.options_radar.strategy_common import (_latest_attribution_labels)

def _historical_candidate_rows(con: Any) -> list[dict[str, Any]]:
    return query_rows(
        con,
        """
        SELECT
            s.*,
            f.required_2x_price,
            f.required_5x_price,
            f.required_10x_price,
            f.required_move_10x_pct,
            f.breakeven,
            f.iv_percentile,
            f.iv_rank,
            f.liquidity_score,
            f.convexity_score,
            sf.price,
            sf.ma_50,
            sf.rs_vs_qqq_20d,
            sf.base_length_days,
            sf.breakout_level
        FROM option_snapshot s
        JOIN option_features f ON f.contract_id = s.contract_id AND f.snapshot_time = s.snapshot_time
        LEFT JOIN stock_features sf ON sf.ticker = s.ticker AND sf.snapshot_time = s.snapshot_time
        ORDER BY s.snapshot_time, s.ticker, s.expiration, s.strike, s.option_type
        """,
    )


def _strategy_outcome_records(con: Any, rows: list[dict[str, Any]], strategy_version: str, strategy: dict[str, Any]) -> list[dict[str, Any]]:
    outcomes = []
    seen: set[tuple[str, str]] = set()
    qqq_cache: dict[str, bool | None] = {}
    attribution_labels = _latest_attribution_labels(con)
    for row in rows:
        event = build_candidate_event(row, strategy_version, strategy)
        if not event or event["state"] != "FIRE":
            continue
        key = (event["contract_id"], event["snapshot_time"])
        if key in seen:
            continue
        seen.add(key)
        outcome = _hypothetical_outcome(con, event)
        if not outcome:
            continue
        snapshot_time = _iso(event["snapshot_time"])
        qqq_above_200d = _qqq_above_200d(con, snapshot_time, qqq_cache)
        outcome.update(
            {
                "ticker": event["ticker"],
                "strategy_version": strategy_version,
                "setup_type": _setup_type(row, event),
                "cohorts": _cohort_labels(row, event, qqq_above_200d),
                "qqq_above_200d": qqq_above_200d,
                "latest_attribution_label": attribution_labels.get(event["event_id"]),
            }
        )
        outcomes.append(outcome)
    return outcomes


def _strategy_outcomes(con: Any, rows: list[dict[str, Any]], strategy_version: str, strategy: dict[str, Any]) -> dict[str, Any]:
    return _outcome_metrics(_strategy_outcome_records(con, rows, strategy_version, strategy))


def _hypothetical_outcome(con: Any, event: dict[str, Any]) -> dict[str, Any] | None:
    entry = _number(event.get("premium_fill_assumption"))
    if entry is None or entry <= 0:
        return None
    rows = query_rows(
        con,
        """
        SELECT snapshot_time, mid
        FROM option_snapshot
        WHERE contract_id = ? AND snapshot_time >= TRY_CAST(? AS TIMESTAMP)
        ORDER BY snapshot_time
        """,
        [event["contract_id"], event["snapshot_time"]],
    )
    marks = [(row["snapshot_time"], _number(row.get("mid"))) for row in rows if _number(row.get("mid")) is not None]
    if not marks:
        return None
    returns = [(snapshot_time, (mid or 0) / entry - 1) for snapshot_time, mid in marks]
    max_time, max_return = max(returns, key=lambda item: item[1])
    _min_time, max_drawdown = min(returns, key=lambda item: item[1])
    last_observation_time = marks[-1][0]
    observation_hours = _elapsed_hours(event["snapshot_time"], last_observation_time)
    time_to_2x = _first_hit_days(event["snapshot_time"], returns, 1.0)
    time_to_5x = _first_hit_days(event["snapshot_time"], returns, 4.0)
    time_to_10x = _first_hit_days(event["snapshot_time"], returns, 9.0)
    drawdown_before_2x = _drawdown_before_threshold(returns, 1.0)
    return {
        "event_id": event["event_id"],
        "contract_id": event["contract_id"],
        "entry_time": event["snapshot_time"],
        "entry_price": entry,
        "max_return_seen": max_return,
        "max_drawdown_seen": max_drawdown,
        "time_to_2x": time_to_2x,
        "time_to_5x": time_to_5x,
        "time_to_10x": time_to_10x,
        "drawdown_before_2x": drawdown_before_2x,
        "timing_label": _timing_label(time_to_2x, max_drawdown, drawdown_before_2x),
        "max_return_time": max_time,
        "last_observation_time": last_observation_time,
        "observation_hours": observation_hours,
        "observation_days": None if observation_hours is None else observation_hours / 24,
    }


def _outcome_metrics(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(outcomes)
    if not count:
        return {
            "candidate_count": 0,
            "hit_rate_2x": 0.0,
            "hit_rate_5x": 0.0,
            "hit_rate_10x": 0.0,
            "false_positive_rate": 0.0,
            "median_max_return": None,
            "median_max_drawdown": None,
            "average_time_to_2x": None,
            "outcomes": [],
        }
    hit_2x = [row for row in outcomes if row["max_return_seen"] >= 1.0]
    hit_5x = [row for row in outcomes if row["max_return_seen"] >= 4.0]
    hit_10x = [row for row in outcomes if row["max_return_seen"] >= 9.0]
    time_to_2x = [row["time_to_2x"] for row in hit_2x if row.get("time_to_2x") is not None]
    return {
        "candidate_count": count,
        "hit_rate_2x": len(hit_2x) / count,
        "hit_rate_5x": len(hit_5x) / count,
        "hit_rate_10x": len(hit_10x) / count,
        "false_positive_rate": 1 - (len(hit_2x) / count),
        "median_max_return": _median([row["max_return_seen"] for row in outcomes]),
        "median_max_drawdown": _median([row["max_drawdown_seen"] for row in outcomes]),
        "average_time_to_2x": _average([float(value) for value in time_to_2x]) if time_to_2x else None,
        "outcomes": outcomes[:50],
    }


def _hit_success_count(outcomes: dict[str, Any], key: str) -> int:
    n = int(outcomes.get("candidate_count") or 0)
    return round(float(outcomes.get(f"hit_rate_{key}") or 0) * n)


def _strategy_arm_significance(baseline: dict[str, Any], proposed: dict[str, Any], *, key: str = "2x", min_per_arm: int = 20) -> dict[str, Any]:
    """Two-proportion significance + Wilson intervals for proposed vs baseline hit rate."""

    bn = int(baseline.get("candidate_count") or 0)
    pn = int(proposed.get("candidate_count") or 0)
    bs = _hit_success_count(baseline, key)
    ps = _hit_success_count(proposed, key)
    blo, bhi = wilson_interval(bs, bn)
    plo, phi = wilson_interval(ps, pn)
    return {
        "key": key,
        "baseline_n": bn,
        "proposed_n": pn,
        "insufficient_sample": bn < min_per_arm or pn < min_per_arm,
        "significant": two_proportion_significant(ps, pn, bs, bn, min_per_arm=min_per_arm),
        "baseline_wilson_lo": round(blo, 4),
        "baseline_wilson_hi": round(bhi, 4),
        "proposed_wilson_lo": round(plo, 4),
        "proposed_wilson_hi": round(phi, 4),
    }


def _walk_forward_folds(
    ordered_rows: list[dict[str, Any]],
    baseline_fn,
    proposed_fn,
    *,
    folds: int = 3,
) -> dict[str, Any]:
    """Split rows into ``folds`` sequential time slices and require the proposed params
    to beat baseline out-of-sample-in-time in a majority of folds. ``baseline_fn`` /
    ``proposed_fn`` map a row subset to an outcomes dict (hit_rate_5x/10x)."""

    n = len(ordered_rows)
    if n < folds:
        return {"folds": [], "folds_improved": 0, "pass": False, "evaluable": False}
    size = n // folds
    results: list[dict[str, Any]] = []
    improved = 0
    for i in range(folds):
        lo = i * size
        hi = n if i == folds - 1 else (i + 1) * size
        slice_rows = ordered_rows[lo:hi]
        base = baseline_fn(slice_rows)
        prop = proposed_fn(slice_rows)
        beats = (float(prop.get("hit_rate_5x") or 0) > float(base.get("hit_rate_5x") or 0)) or (
            float(prop.get("hit_rate_10x") or 0) > float(base.get("hit_rate_10x") or 0)
        )
        improved += 1 if beats else 0
        results.append(
            {
                "fold": i,
                "n": len(slice_rows),
                "baseline_hit_rate_5x": base.get("hit_rate_5x"),
                "proposed_hit_rate_5x": prop.get("hit_rate_5x"),
                "beats": beats,
            }
        )
    return {"folds": results, "folds_improved": improved, "pass": improved >= 2, "evaluable": True}


def _backtest_verdict(
    baseline: dict[str, Any],
    proposed: dict[str, Any],
    *,
    significance: dict[str, Any] | None = None,
    walk_forward: dict[str, Any] | None = None,
) -> str:
    if int(proposed.get("candidate_count") or 0) == 0:
        return "fail"
    # Honest validation: not enough observations to claim anything -> block, don't pass.
    if significance is not None and significance.get("insufficient_sample"):
        return "insufficient_sample"
    improves_10x = float(proposed.get("hit_rate_10x") or 0) > float(baseline.get("hit_rate_10x") or 0)
    improves_5x = float(proposed.get("hit_rate_5x") or 0) > float(baseline.get("hit_rate_5x") or 0)
    baseline_false = float(baseline.get("false_positive_rate") or 0)
    proposed_false = float(proposed.get("false_positive_rate") or 0)
    allowed_false_positive = 1.0 if int(baseline.get("candidate_count") or 0) == 0 else min(1.0, baseline_false + 0.25)
    if not ((improves_10x or improves_5x) and proposed_false <= allowed_false_positive):
        return "fail"
    # The improvement must be statistically significant and hold out-of-sample-in-time.
    if significance is not None and not significance.get("significant"):
        return "fail"
    if walk_forward is not None and walk_forward.get("evaluable") and not walk_forward.get("pass"):
        return "fail"
    return "pass"


def _forward_test_verdict(baseline: dict[str, Any], proposed: dict[str, Any], days_observed: int) -> str:
    if days_observed < MIN_FORWARD_TEST_DAYS:
        return "collecting_data"
    if int(proposed.get("candidate_count") or 0) == 0:
        return "fail"
    if float(proposed.get("hit_rate_5x") or 0) >= float(baseline.get("hit_rate_5x") or 0):
        return "pass"
    return "fail"


def _first_hit_days(entry_time: Any, returns: list[tuple[Any, float]], threshold: float) -> int | None:
    for snapshot_time, value in returns:
        if value >= threshold:
            return _elapsed_days(entry_time, snapshot_time)
    return None


def _return_at_horizon(entry_time: Any, returns: list[tuple[Any, float]], horizon_days: int, mark_time: Any) -> float | None:
    elapsed_to_mark = _elapsed_days(entry_time, mark_time)
    if elapsed_to_mark is None or elapsed_to_mark < horizon_days:
        return None
    for snapshot_time, value in returns:
        elapsed = _elapsed_days(entry_time, snapshot_time)
        if elapsed is not None and elapsed >= horizon_days:
            return value
    return None


def _drawdown_before_threshold(returns: list[tuple[Any, float]], threshold: float) -> float | None:
    observed: list[float] = []
    for _snapshot_time, value in returns:
        observed.append(value)
        if value >= threshold:
            break
    return min(observed) if observed else None


def _timing_label(time_to_2x: int | None, max_drawdown: float | None, drawdown_before_2x: float | None) -> str:
    if time_to_2x is not None and (drawdown_before_2x or 0.0) <= -0.30:
        return "early_but_worked"
    if time_to_2x is None and (max_drawdown or 0.0) <= -0.40:
        return "false_positive_drawdown"
    if time_to_2x is not None and time_to_2x <= 5:
        return "fast_confirmation"
    if time_to_2x is not None:
        return "worked_after_wait"
    return "pending_or_failed"


def _setup_type(row: dict[str, Any], event: dict[str, Any]) -> str:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    blockers = [str(item) for item in raw.get("blockers", [])] if isinstance(raw.get("blockers"), list) else []
    positives = [str(item) for item in raw.get("positives", [])] if isinstance(raw.get("positives"), list) else []
    price = _number(row.get("price"))
    ma50 = _number(row.get("ma_50"))
    breakout = _number(row.get("breakout_level"))
    base_length = _integer(row.get("base_length_days")) or 0
    distance_from_high = _number(row.get("distance_from_52w_high"))
    rs20 = _number(row.get("rs_vs_qqq_20d"))
    if "stock_below_50d" in blockers or (price is not None and ma50 is not None and price < ma50):
        return "early_reversal"
    if distance_from_high is not None and distance_from_high <= -0.30:
        return "post_crash_recovery"
    if base_length >= 30 and price is not None and breakout is not None and price >= breakout * 0.98:
        return "post_base_breakout"
    if rs20 is not None and rs20 >= 0.05:
        return "relative_strength_leader"
    if "premium_inside_buy_under" in positives and "stock_above_50d" in positives:
        return "reclaiming_50d"
    return "standard_reversal"


def _cohort_labels(row: dict[str, Any], event: dict[str, Any], qqq_above_200d: bool | None) -> list[dict[str, str]]:
    labels = [{"type": "setup_type", "value": _setup_type(row, event)}]
    required_move = _number(row.get("required_move_10x_pct"))
    iv_percentile = _number(row.get("iv_percentile"))
    spread_pct = _number(row.get("spread_pct"))
    if required_move is not None:
        value = "under_200pct" if required_move <= 2.0 else "under_350pct" if required_move <= 3.5 else "over_350pct"
        labels.append({"type": "required_move_bucket", "value": value})
    if iv_percentile is not None:
        value = "low_iv" if iv_percentile < 50 else "normal_iv" if iv_percentile <= 70 else "high_iv"
        labels.append({"type": "iv_regime", "value": value})
    if spread_pct is not None:
        value = "tight_spread" if spread_pct <= 0.15 else "usable_spread" if spread_pct <= 0.25 else "wide_spread"
        labels.append({"type": "liquidity_regime", "value": value})
    value = "qqq_above_200d" if qqq_above_200d is True else "qqq_below_200d" if qqq_above_200d is False else "qqq_200d_unknown"
    labels.append({"type": "market_regime", "value": value})
    return labels


def _value_counts(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _cohort_definition(cohort_type: str, cohort_value: str) -> str:
    definitions = {
        ("setup_type", "early_reversal"): "Stock had not reclaimed the 50D context at candidate time.",
        ("setup_type", "post_crash_recovery"): "Stock was at least 30% below its 52-week high at candidate time.",
        ("setup_type", "post_base_breakout"): "Stock had a 30+ day base and was near or above the stored breakout level.",
        ("setup_type", "relative_strength_leader"): "20-day relative strength versus QQQ was at least +5%.",
        ("setup_type", "reclaiming_50d"): "Candidate passed premium and 50D reclaim gates.",
        ("setup_type", "standard_reversal"): "Candidate passed baseline gates without a stronger deterministic setup bucket.",
        ("market_regime", "qqq_above_200d"): "QQQ close was at or above its 200-day moving average.",
        ("market_regime", "qqq_below_200d"): "QQQ close was below its 200-day moving average.",
    }
    return definitions.get((cohort_type, cohort_value), f"{cohort_type}={cohort_value}")


def _proposed_strategy_parameters(base: dict[str, Any], value: Any) -> dict[str, Any]:
    changes = _json_or_list(value)
    if not isinstance(changes, dict):
        changes = {}
    return {**base, **{key: item for key, item in changes.items() if key != "candidate_note"}}
