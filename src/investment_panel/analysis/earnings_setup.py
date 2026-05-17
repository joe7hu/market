"""Deterministic earnings and analyst-estimate setup scoring."""

from __future__ import annotations

from datetime import date
from typing import Any

from investment_panel.core.db import json_dumps, query_rows


def store_earnings_setups(con: Any, symbols: list[str]) -> int:
    today = date.today().isoformat()
    count = 0
    for symbol in symbols:
        estimate_rows = query_rows(
            con,
            "SELECT as_of, estimates, source FROM analyst_estimates WHERE symbol = ? ORDER BY as_of DESC LIMIT 1",
            [symbol],
        )
        earnings_rows = query_rows(
            con,
            "SELECT event_date, metrics, source FROM earnings_events WHERE symbol = ? ORDER BY event_date DESC LIMIT 1",
            [symbol],
        )
        if not estimate_rows and not earnings_rows:
            continue
        estimate_payload = parse_json(estimate_rows[0].get("estimates")) if estimate_rows else {}
        earnings_payload = parse_json(earnings_rows[0].get("metrics")) if earnings_rows else {}
        setup = analyze_earnings_setup(estimate_payload, earnings_payload)
        con.execute(
            """
            INSERT OR REPLACE INTO earnings_setups
            (symbol, as_of, event_date, setup_type, score, revision_score, surprise_score,
             estimate_spread_score, sentiment_score, verdict, metrics, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                symbol,
                today,
                earnings_rows[0].get("event_date") if earnings_rows else None,
                "pre_post_earnings_setup",
                setup["score"],
                setup["revision_score"],
                setup["surprise_score"],
                setup["estimate_spread_score"],
                setup["sentiment_score"],
                setup["verdict"],
                json_dumps(setup["metrics"]),
                "deterministic_yfinance",
            ],
        )
        count += 1
    return count


def analyze_earnings_setup(estimates: dict[str, Any], earnings: dict[str, Any]) -> dict[str, Any]:
    revision_score, revision_metrics = revision_momentum(estimates)
    surprise_score, surprise_metrics = surprise_quality(earnings)
    spread_score, spread_metrics = estimate_spread(estimates)
    sentiment_score, sentiment_metrics = analyst_sentiment(estimates)
    score = round(revision_score * 0.35 + surprise_score * 0.25 + spread_score * 0.20 + sentiment_score * 0.20, 2)
    verdict = (
        "positive_revision_setup"
        if score >= 75 and revision_score >= 60
        else "constructive"
        if score >= 60
        else "uncertain"
        if score >= 40
        else "risk"
    )
    return {
        "score": score,
        "revision_score": revision_score,
        "surprise_score": surprise_score,
        "estimate_spread_score": spread_score,
        "sentiment_score": sentiment_score,
        "verdict": verdict,
        "metrics": {
            "revision": revision_metrics,
            "surprise": surprise_metrics,
            "estimate_spread": spread_metrics,
            "sentiment": sentiment_metrics,
            "note": "Deterministic setup score from stored yfinance estimate/earnings rows; not financial advice.",
        },
    }


def revision_momentum(estimates: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    trend_rows = records(estimates.get("eps_trend"))
    revision_rows = records(estimates.get("eps_revisions"))
    trend_changes = []
    for row in trend_rows:
        current = first_number(row, ["current", "Current Estimate", "Current"])
        prior_30 = first_number(row, ["30daysAgo", "30 Days Ago", "30DaysAgo"])
        prior_90 = first_number(row, ["90daysAgo", "90 Days Ago", "90DaysAgo"])
        baseline = prior_30 if prior_30 not in (None, 0) else prior_90
        if current is not None and baseline not in (None, 0):
            trend_changes.append((current - baseline) / abs(baseline))
    revision_ratios = []
    for row in revision_rows:
        up = first_number(row, ["upLast30days", "Up Last 30 Days", "upLast7days", "Up Last 7 Days"])
        down = first_number(row, ["downLast30days", "Down Last 30 Days", "downLast7days", "Down Last 7 Days"])
        if up is not None and down is not None and up + down > 0:
            revision_ratios.append(up / (up + down))
    trend_component = bounded_score(50 + (average(trend_changes) or 0) * 500)
    breadth_component = bounded_score((average(revision_ratios) or 0.5) * 100)
    score = round(trend_component * 0.6 + breadth_component * 0.4, 2)
    return score, {
        "average_eps_change_pct": round((average(trend_changes) or 0) * 100, 4),
        "average_revision_up_ratio": round(average(revision_ratios) or 0.5, 4),
        "trend_rows": len(trend_rows),
        "revision_rows": len(revision_rows),
    }


def surprise_quality(earnings: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    history_rows = records(earnings.get("earnings_history"))
    surprises = []
    beats = 0
    for row in history_rows[:8]:
        surprise = first_number(row, ["surprisePercent", "Surprise(%)", "surprise_percent"])
        estimate = first_number(row, ["epsEstimate", "EPS Estimate", "estimate"])
        actual = first_number(row, ["epsActual", "Reported EPS", "actual"])
        if surprise is not None:
            surprise_value = surprise / 100 if abs(surprise) > 1 else surprise
            surprises.append(surprise_value)
            if surprise_value > 0:
                beats += 1
        elif estimate not in (None, 0) and actual is not None:
            surprise_value = (actual - estimate) / abs(estimate)
            surprises.append(surprise_value)
            if surprise_value > 0:
                beats += 1
    beat_rate = beats / len(surprises) if surprises else 0.5
    avg_surprise = average(surprises) or 0
    score = bounded_score(50 + avg_surprise * 250 + (beat_rate - 0.5) * 60)
    return round(score, 2), {
        "beat_rate": round(beat_rate, 4),
        "average_surprise_pct": round(avg_surprise * 100, 4),
        "history_rows": len(history_rows),
    }


def estimate_spread(estimates: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    estimate_rows = records(estimates.get("earnings_estimate"))
    row = estimate_rows[0] if estimate_rows else {}
    avg_value = first_number(row, ["avg", "Avg. Estimate", "average"])
    low = first_number(row, ["low", "Low Estimate"])
    high = first_number(row, ["high", "High Estimate"])
    analysts = first_number(row, ["numberOfAnalysts", "No. of Analysts", "analysts"])
    spread_pct = ((high - low) / abs(avg_value)) if avg_value not in (None, 0) and low is not None and high is not None else None
    spread_component = 70 if spread_pct is None else bounded_score(100 - spread_pct * 300)
    coverage_component = bounded_score((analysts or 5) / 20 * 100)
    score = round(spread_component * 0.7 + coverage_component * 0.3, 2)
    return score, {
        "spread_pct": round(spread_pct * 100, 4) if spread_pct is not None else None,
        "analyst_count": analysts,
        "estimate_rows": len(estimate_rows),
    }


def analyst_sentiment(estimates: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    targets = estimates.get("analyst_price_targets")
    target_dict = targets if isinstance(targets, dict) else {}
    current = first_number(target_dict, ["current", "currentPrice"])
    mean = first_number(target_dict, ["mean", "meanTarget", "targetMeanPrice"])
    median = first_number(target_dict, ["median", "targetMedianPrice"])
    reference = mean if mean is not None else median
    implied_upside = ((reference - current) / current) if current not in (None, 0) and reference is not None else None
    score = 50 if implied_upside is None else bounded_score(50 + implied_upside * 150)
    return round(score, 2), {
        "current": current,
        "mean_target": mean,
        "median_target": median,
        "implied_upside_pct": round(implied_upside * 100, 4) if implied_upside is not None else None,
    }


def records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def parse_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    import json

    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def first_number(row: dict[str, Any], keys: list[str]) -> float | None:
    for key in keys:
        value = row.get(key)
        parsed = as_float(value)
        if parsed is not None:
            return parsed
    lowered = {str(key).lower().replace(" ", "").replace("_", ""): value for key, value in row.items()}
    for key in keys:
        value = lowered.get(key.lower().replace(" ", "").replace("_", ""))
        parsed = as_float(value)
        if parsed is not None:
            return parsed
    return None


def as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def bounded_score(value: float) -> float:
    return max(0.0, min(100.0, value))
