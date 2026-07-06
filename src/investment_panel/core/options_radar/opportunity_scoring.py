"""Conviction / asymmetry / evidence scoring for opportunities."""

from __future__ import annotations

from typing import Any

from investment_panel.analysis.option_ev import (ev_score)
from investment_panel.core.db import (query_rows)
from investment_panel.core.options_radar.calibration import (calibrated_p2x)
from investment_panel.core.options_radar.coerce import (_integer, _json, _number)
from investment_panel.core.options_radar.constants import (DEFAULT_STRATEGY_VERSION)
from investment_panel.core.options_radar.scoring import (_theme_watch_matches, _theme_watch_score)
from investment_panel.core.options_radar.strategy_outcomes import (_cohort_labels)


def load_cohort_priors(con: Any, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> dict[tuple[str, str], dict[str, Any]]:
    """Load *significant* realized cohort edges so scoring can use what the learning loop
    measured instead of treating ``learning_score`` as a constant. Keyed by
    ``(cohort_type, cohort_value)``; only cohorts that cleared the diagnostic significance
    gate (n>=20 and Wilson-lower-bound>=10% on the realizable 2x hit rate) are kept."""

    rows = query_rows(
        con,
        "SELECT cohort_type, cohort_value, candidate_count, hit_rate_2x, hit_rate_5x, raw "
        "FROM strategy_cohort_result WHERE strategy_version = ?",
        [strategy_version],
    )
    priors: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        significance = _json(row.get("raw")).get("significance") or {}
        if not significance.get("significant"):
            continue
        key = (str(row.get("cohort_type") or ""), str(row.get("cohort_value") or ""))
        priors[key] = {
            "hit_rate_2x": _number(row.get("hit_rate_2x")) or 0.0,
            "hit_rate_5x": _number(row.get("hit_rate_5x")) or 0.0,
            "n": _integer(row.get("candidate_count")) or 0,
        }
    return priors

def _opportunity_scores(
    row: dict[str, Any],
    *,
    validation: dict[str, Any],
    source_context: dict[str, Any],
    qqq_above_200d: bool | None,
    calibration: dict[str, Any] | None = None,
    cohort_priors: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    required_move = _number(row.get("required_move_pct"))
    convexity = _number(row.get("convexity_score")) or 0.0
    liquidity = _number(row.get("liquidity_score")) or 0.0
    spread = _number(row.get("spread_pct"))
    buy_under = _number(row.get("buy_under"))
    fill = _number(row.get("premium_fill_assumption"))
    dte = _integer(row.get("dte"))
    quality_status = str(row.get("quality_status") or "ok").lower()

    move_score = 0.0 if required_move is None else max(0.0, min(100.0, 100.0 - required_move * 32.0))
    # EV asymmetry (probability/theta-aware) supersedes the linear convexity+move proxy
    # when the contract was priceable; otherwise fall back to the legacy proxy.
    ev = _json(row.get("raw")).get("ev") or {}
    ev_multiple = _number(ev.get("ev_multiple"))
    ev_p2x = _number(ev.get("p_2x"))
    ev_asymmetry = ev_score(ev_multiple, spread) if ev_multiple is not None else None
    asymmetry = ev_asymmetry if ev_asymmetry is not None else min(100.0, convexity * 0.55 + move_score * 0.45)

    spread_score = 45.0 if spread is None else max(0.0, min(100.0, 100.0 - spread * 360.0))
    cap_room_score = 45.0
    if buy_under is not None and fill is not None and buy_under > 0:
        cap_room_score = max(0.0, min(100.0, (buy_under - fill) / buy_under * 220.0 + 50.0))
    entry = min(100.0, liquidity * 0.45 + spread_score * 0.35 + cap_room_score * 0.20)

    thesis_score = max(_thesis_score(validation, row), _source_backed_thesis_score(source_context))
    evidence = min(100.0, thesis_score * 0.65 + float(source_context["score"]) * 0.35)
    catalyst = min(100.0, float(source_context["score"]) * 0.65 + _catalyst_validation_score(validation) * 0.35)
    regime = 85.0 if qqq_above_200d is True else 25.0 if qqq_above_200d is False else 45.0
    dte_score = 45.0 if dte is None else max(0.0, min(100.0, 100.0 - abs(dte - 540) / 540 * 70.0))
    quality_score = 100.0 if quality_status == "ok" else 55.0 if quality_status == "caution" else 10.0
    survivability = min(100.0, dte_score * 0.40 + liquidity * 0.30 + quality_score * 0.30)
    learning = _learning_score(row, cohort_priors=cohort_priors, qqq_above_200d=qqq_above_200d)
    theme_bonus = _theme_watch_score(_theme_watch_matches(row)) * 0.60
    base_conviction = (
        asymmetry * 0.24
        + entry * 0.20
        + evidence * 0.20
        + catalyst * 0.12
        + regime * 0.10
        + survivability * 0.10
        + learning * 0.04
        + theme_bonus
    )
    # Probability-grounded conviction: calibrated P(2x) scaled by EV headroom. The
    # multi-factor base score becomes context (evidence/regime/survivability) blended
    # behind the EV signal rather than the primary driver. Identity P(2x) until the
    # calibration map has >=30 mature observations.
    cal_p2x = calibrated_p2x(ev_p2x, calibration) if ev_p2x is not None else None
    ev_conviction = (
        100.0 * cal_p2x * min(1.0, (ev_multiple or 0.0) / 2.0)
        if cal_p2x is not None and ev_multiple is not None
        else None
    )
    conviction = (0.55 * ev_conviction + 0.45 * base_conviction) if ev_conviction is not None else base_conviction
    return {
        "conviction_score": round(max(0.0, min(100.0, conviction)), 2),
        "asymmetry_score": round(asymmetry, 2),
        "entry_quality_score": round(entry, 2),
        "evidence_score": round(evidence, 2),
        "catalyst_score": round(catalyst, 2),
        "regime_score": round(regime, 2),
        "survivability_score": round(survivability, 2),
        "learning_score": round(learning, 2),
        "calibrated_p2x": round(cal_p2x, 4) if cal_p2x is not None else None,
        "ev_conviction": round(ev_conviction, 2) if ev_conviction is not None else None,
        "ev_multiple": round(ev_multiple, 4) if ev_multiple is not None else None,
    }


def _thesis_score(validation: dict[str, Any], row: dict[str, Any]) -> float:
    state = validation.get("state")
    if state in {"validated", "strengthening"}:
        score = 85.0
    elif row.get("thesis_id"):
        score = 55.0
    elif validation.get("validation_id"):
        score = 45.0
    else:
        return 0.0
    if validation.get("proof_status") in {"supported", "source_backed", "clear"}:
        score += 7.5
    if validation.get("evidence_status") in {"source_backed", "source_confirmed", "supported"}:
        score += 7.5
    if validation.get("invalidation_status") == "breached":
        score = 0.0
    if validation.get("red_team_status") == "hard_risk_triggered":
        score = 0.0
    return max(0.0, min(100.0, score))


def _source_backed_thesis_score(source_context: dict[str, Any]) -> float:
    count = int(source_context.get("count") or 0)
    catalyst_count = int(source_context.get("catalyst_count") or 0)
    confidence = _number(source_context.get("average_confidence")) or 0.0
    source_score = float(source_context.get("score") or 0.0)
    if count >= 4 and catalyst_count >= 1 and source_score >= 70.0:
        return min(92.0, 74.0 + confidence * 18.0)
    if count >= 2 and source_score >= 45.0:
        return min(72.0, 52.0 + confidence * 12.0)
    return 0.0


def _catalyst_validation_score(validation: dict[str, Any]) -> float:
    status = str(validation.get("catalyst_status") or "")
    if status in {"scheduled", "source_confirmed", "supported"}:
        return 90.0
    if status in {"partial", "pending", "agent_cited"}:
        return 55.0
    return 20.0


def _learning_score(
    row: dict[str, Any],
    *,
    cohort_priors: dict[tuple[str, str], dict[str, Any]] | None = None,
    qqq_above_200d: bool | None = None,
) -> float:
    """Map the candidate's cohorts (setup type, IV/liquidity regime, market regime, …) to
    the realized hit rates the learning loop measured for those cohorts, so a setup that
    historically produced exitable winners ranks above one that did not. Falls back to the
    legacy neutral prior when no *significant* cohort covers this candidate — fresh setups
    are never hidden, only nudged by evidence."""

    raw = _json(row.get("raw"))
    positives = raw.get("positives") if isinstance(raw.get("positives"), list) else []
    neutral = 60.0 if ("10x_math_inside_cap" in positives and "premium_inside_buy_under" in positives) else 50.0
    if not cohort_priors:
        return neutral
    labels = _cohort_labels(row, {"raw": raw}, qqq_above_200d)
    edges = [
        prior["hit_rate_2x"]
        for label in labels
        if (prior := cohort_priors.get((str(label.get("type")), str(label.get("value"))))) is not None
    ]
    if not edges:
        return neutral
    # Significant cohorts only reach here; their realized 2x hit rate (0..1) maps straight
    # to a 0..100 learning score, averaged when several cohorts cover the candidate.
    return round(max(0.0, min(100.0, sum(edges) / len(edges) * 100.0)), 2)
