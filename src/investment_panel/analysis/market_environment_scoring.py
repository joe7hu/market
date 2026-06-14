"""Pure scoring, summary, and aggregate helpers for the market environment model.

These functions operate on plain row lists/scalars (no DuckDB connection, no DB
access). The DB-backed read accessors in ``core/panel/market_environment.py``
fetch rows and feed them into these computations.
"""

from __future__ import annotations

from typing import Any

from investment_panel.core.panel.coerce import _average, _format_metric, _median, _number_from_any, _optional_number, _share


def _compact_empty_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}


def _preferred_valuation_by_symbol(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    priority = {"blended_dcf_relative": 0, "dcf_base_case": 1, "relative_revenue_multiple": 2, "fundamental_proxy": 3}
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        current = output.get(symbol)
        if current is None or priority.get(str(row.get("method") or ""), 99) < priority.get(str(current.get("method") or ""), 99):
            output[symbol] = row
    return output


def _valuation_chart_points(history: list[dict[str, Any]], latest_price: float | None, fair_value: float | None) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for point in history[-180:]:
        close = _number_from_any(point.get("close"))
        if not close:
            continue
        row = {"date": str(point.get("date") or ""), "price": close}
        if latest_price and fair_value:
            row["fair_value"] = fair_value
            row["discount_pct"] = ((fair_value - close) / close) * 100
        points.append(row)
    return points


def _market_valuation_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    components = [row for row in rows if row.get("history") or row.get("forward_pe") or row.get("upside_pct")]
    if not components:
        return {}
    aggregate_history = _aggregate_normalized_history([row.get("history") for row in components if isinstance(row.get("history"), list)])
    median_upside = _median([_optional_number(row.get("upside_pct")) for row in components])
    median_forward_pe = _median([_optional_number(row.get("forward_pe")) for row in components])
    median_ps = _median([_optional_number(row.get("ps_ratio")) for row in components])
    score = _valuation_score(median_upside, median_forward_pe, median_ps)
    return _compact_empty_fields(
        {
            "symbol": "MARKET",
            "name": "Watchlist market",
            "scope": "whole_market",
            "component_count": len(components),
            "forward_pe": median_forward_pe,
            "ps_ratio": median_ps,
            "upside_pct": median_upside,
            "valuation_posture": _valuation_posture(median_upside, median_forward_pe, median_ps),
            "valuation_score": score,
            "history": aggregate_history,
            "coverage": f"{len(components)} covered names",
            "next_action": _valuation_next_action("MARKET", median_upside, median_forward_pe, median_ps),
        }
    )


def _aggregate_normalized_history(histories: list[Any]) -> list[dict[str, Any]]:
    by_date: dict[str, list[float]] = {}
    for history in histories:
        if not isinstance(history, list):
            continue
        valid = [point for point in history if isinstance(point, dict) and _number_from_any(point.get("price"))]
        if not valid:
            continue
        base = _number_from_any(valid[0].get("price"))
        if not base:
            continue
        for point in valid:
            date = str(point.get("date") or "")
            price = _number_from_any(point.get("price"))
            if date and price:
                by_date.setdefault(date, []).append((price / base) * 100)
    return [{"date": date, "price": round(sum(values) / len(values), 2)} for date, values in sorted(by_date.items())[-180:]]


def _market_symbol_scope(symbol: str, quote: dict[str, Any], config_watchlist: list[dict[str, Any]] | None = None) -> str:
    if symbol in {"SPY", "QQQ", "IWM"}:
        return "benchmark"
    configured = {str(item.get("symbol") or "").upper() for item in config_watchlist or []}
    if symbol in configured:
        return "watchlist"
    if quote:
        return "watchlist"
    return "coverage_gap"


def _valuation_coverage(latest_price: float | None, fair_value: float | None, forward_pe: float | None, ps_ratio: float | None, history: list[dict[str, Any]]) -> str:
    missing = []
    if not latest_price:
        missing.append("price")
    if not fair_value and not forward_pe and not ps_ratio:
        missing.append("valuation")
    if not history:
        missing.append("history")
    return "complete" if not missing else f"missing {', '.join(missing)}"


def _valuation_posture(upside_pct: float | None, forward_pe: float | None, ps_ratio: float | None) -> str:
    if upside_pct is None and not forward_pe and not ps_ratio:
        return "missing"
    upside = _number_from_any(upside_pct)
    pe = _number_from_any(forward_pe)
    ps = _number_from_any(ps_ratio)
    if upside >= 20:
        return "discounted"
    if upside <= -20 or pe >= 45 or ps >= 18:
        return "stretched"
    if upside >= 5 or (pe and pe <= 22) or (ps and ps <= 6):
        return "fair-to-attractive"
    return "fair"


def _valuation_score(upside_pct: float | None, forward_pe: float | None, ps_ratio: float | None) -> float | None:
    values = []
    upside = _number_from_any(upside_pct)
    pe = _number_from_any(forward_pe)
    ps = _number_from_any(ps_ratio)
    if upside:
        values.append(max(0, min(100, 50 + upside)))
    if pe:
        values.append(max(0, min(100, 85 - pe)))
    if ps:
        values.append(max(0, min(100, 80 - (ps * 3))))
    return round(sum(values) / len(values), 2) if values else None


def _valuation_next_action(symbol: str, upside_pct: float | None, forward_pe: float | None, ps_ratio: float | None) -> str:
    posture = _valuation_posture(upside_pct, forward_pe, ps_ratio)
    if symbol == "MARKET":
        if posture == "stretched":
            return "Require stronger thesis evidence before increasing market exposure."
        if posture == "discounted":
            return "Review watchlist names where thesis quality and valuation now align."
        return "Keep sizing normal and let source-backed names outrank broad exposure."
    if posture == "stretched":
        return "Demand catalyst or source-confirmed thesis before adding."
    if posture == "discounted":
        return "Check thesis and invalidation before promoting to active research."
    return "Keep on watch unless evidence or price changes."


def _market_valuation_reference_summary(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    parts = []
    for row in rows[:4]:
        value = _optional_number(row.get("latest_value"))
        percentile = _optional_number(row.get("percentile"))
        suffix = str(row.get("suffix") or "")
        if value is None or percentile is None:
            continue
        formatted = f"{value:.2f}{suffix}" if suffix else f"{value:.2f}"
        parts.append(f"{row.get('label')}: {formatted}, {percentile:.0f}th percentile")
    return "; ".join(parts)


def _market_valuation_reference_source(rows: list[dict[str, Any]]) -> str:
    sources = {str(row.get("source") or "").lower() for row in rows}
    if any("munger" in source for source in sources):
        return "Munger Mode market metrics"
    if "multpl" in sources:
        return "Multpl valuation tables"
    return "Broad-market valuation tables"


def _price_overlay_by_month(points: list[dict[str, Any]]) -> dict[str, float]:
    by_month: dict[str, float] = {}
    for point in points:
        as_of = str(point.get("as_of") or "")
        price = _optional_number(point.get("value"))
        if as_of and price is not None:
            by_month[as_of[:7]] = price
    return by_month


def _sample_market_metric_history(points: list[dict[str, Any]], price_by_month: dict[str, float], max_points: int = 520) -> list[dict[str, Any]]:
    if len(points) > max_points:
        stride = max(1, (len(points) + max_points - 1) // max_points)
        selected = [point for index, point in enumerate(points) if index % stride == 0 or index == len(points) - 1]
    else:
        selected = points
    return [
        {
            "date": str(point.get("as_of")),
            "value": point.get("value"),
            "index_price": price_by_month.get(str(point.get("as_of"))[:7]),
        }
        for point in selected
    ]


def _asset_trend_score(rows: list[dict[str, Any]]) -> float | None:
    candidates = [row for row in rows if row.get("group_name") in {"Market", "Sectors"}]
    checks = []
    for row in candidates:
        for key in ("sma_10_up", "sma_20_up", "sma_50_up", "sma_200_up"):
            if row.get(key) is not None:
                checks.append(bool(row.get(key)))
    return round(_share(checks) * 100, 2) if checks else None


def _asset_trend_summary(rows: list[dict[str, Any]]) -> str:
    market = [row for row in rows if row.get("group_name") == "Market"]
    if not market:
        return ""
    above_200 = _share([bool(row.get("sma_200_up")) for row in market if row.get("sma_200_up") is not None]) * 100
    avg_1m = _average([_optional_number(row.get("return_1m")) for row in market])
    avg_1y = _average([_optional_number(row.get("return_1y")) for row in market])
    return f"{_format_metric(above_200, '%')} of broad market rows above 200-day SMA; {_format_metric(avg_1m, '%')} 1-month average; {_format_metric(avg_1y, '%')} 1-year average."


def _asset_breadth_score(rows: list[dict[str, Any]]) -> float | None:
    candidates = [row for row in rows if row.get("group_name") in {"Market", "Sectors", "Industries", "Managed ETFs"}]
    if not candidates:
        return None
    ma_checks = [bool(row.get("sma_20_gt_50")) for row in candidates if row.get("sma_20_gt_50") is not None]
    ma_breadth = _share(ma_checks) * 100 if ma_checks else None
    range_scores = [_optional_number(row.get("range_ratio_52w")) for row in candidates]
    range_score = _average(range_scores)
    return _average([ma_breadth, range_score])


def _asset_breadth_summary(rows: list[dict[str, Any]]) -> str:
    candidates = [row for row in rows if row.get("group_name") in {"Market", "Sectors", "Industries", "Managed ETFs"}]
    if not candidates:
        return ""
    short_checks = [bool(row.get("sma_20_gt_50")) for row in candidates if row.get("sma_20_gt_50") is not None]
    long_checks = [bool(row.get("sma_50_gt_200")) for row in candidates if row.get("sma_50_gt_200") is not None]
    short_cross = _share(short_checks) * 100 if short_checks else None
    long_cross = _share(long_checks) * 100 if long_checks else None
    near_high = _share([(_optional_number(row.get("pct_from_52w_high")) or 100) <= 5 for row in candidates]) * 100
    return f"{_format_metric(short_cross, '%')} have 20-day SMA above 50-day; {_format_metric(long_cross, '%')} have 50-day above 200-day; {_format_metric(near_high, '%')} are within 5% of 52-week highs."


def _risk_appetite_score(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    by_symbol = {str(row.get("symbol") or "").upper(): row for row in rows}
    values = []
    vix = _optional_number(by_symbol.get("VIX", {}).get("price"))
    if vix is not None:
        values.append(max(0, min(100, 100 - ((vix - 12) * 4))))
    dollar_return = _optional_number(by_symbol.get("NYICDX", {}).get("return_1m"))
    if dollar_return is not None:
        values.append(max(0, min(100, 55 - dollar_return * 4)))
    tlt_trend = by_symbol.get("TLT", {}).get("sma_50_gt_200")
    if tlt_trend is not None:
        values.append(65 if tlt_trend else 35)
    ibit_return = _optional_number(by_symbol.get("IBIT", {}).get("return_1m"))
    if ibit_return is not None:
        values.append(max(0, min(100, 50 + ibit_return)))
    return _average(values)


def _risk_appetite_summary(rows: list[dict[str, Any]]) -> str:
    by_symbol = {str(row.get("symbol") or "").upper(): row for row in rows}
    vix = _optional_number(by_symbol.get("VIX", {}).get("price"))
    dollar = _optional_number(by_symbol.get("NYICDX", {}).get("return_1m"))
    tlt = _optional_number(by_symbol.get("TLT", {}).get("return_1m"))
    if vix is None and dollar is None and tlt is None:
        return "VIX, dollar, and bond inputs are not loaded."
    return f"VIX {_format_metric(vix, '')}; dollar 1M {_format_metric(dollar, '%')}; TLT 1M {_format_metric(tlt, '%')}."


def _leadership_score(rows: list[dict[str, Any]]) -> float | None:
    candidates = [row for row in rows if row.get("group_name") in {"Sectors", "Industries", "Managed ETFs", "Countries"}]
    if not candidates:
        return None
    positives = _share([(_optional_number(row.get("return_1m")) or 0) > 0 for row in candidates]) * 100
    near_high = _share([(_optional_number(row.get("pct_from_52w_high")) or 100) <= 10 for row in candidates]) * 100
    return _average([positives, near_high])


def _leadership_summary(rows: list[dict[str, Any]]) -> str:
    candidates = [row for row in rows if row.get("group_name") in {"Sectors", "Industries", "Managed ETFs", "Countries"}]
    if not candidates:
        return "Sector, theme, and country leadership rows are not loaded."
    leaders = sorted(candidates, key=lambda row: _optional_number(row.get("return_1m")) or -999, reverse=True)[:3]
    laggards = sorted(candidates, key=lambda row: _optional_number(row.get("return_1m")) or 999)[:2]
    leader_text = ", ".join(f"{row.get('symbol')} {_format_metric(_optional_number(row.get('return_1m')), '%')}" for row in leaders)
    laggard_text = ", ".join(f"{row.get('symbol')} {_format_metric(_optional_number(row.get('return_1m')), '%')}" for row in laggards)
    return f"1-month leaders: {leader_text}; laggards: {laggard_text}."


def _weighted_environment_score(rows: list[dict[str, Any]]) -> float | None:
    weighted = []
    total_weight = 0.0
    for row in rows:
        score = _optional_number(row.get("score"))
        weight = _optional_number(row.get("weight")) or 0.0
        if score is None or weight <= 0:
            continue
        weighted.append(score * weight)
        total_weight += weight
    if not weighted or total_weight <= 0:
        return _average([_optional_number(row.get("score")) for row in rows])
    return round(sum(weighted) / total_weight, 2)


def _environment_bucket(category: str, score: float | None, evidence: str, portfolio_effect: str, next_action: str, weight: float | None = None, source: str | None = None) -> dict[str, Any]:
    normalized = round(max(0, min(100, score)), 2) if score is not None else None
    return {
        "category": category,
        "score": normalized,
        "posture": _environment_posture(normalized),
        "evidence": evidence,
        "portfolio_effect": portfolio_effect,
        "next_action": next_action,
        "weight": weight,
        "source": source,
    }


def _environment_posture(score: float | None) -> str:
    if score is None:
        return "not enough data"
    if score >= 70:
        return "constructive"
    if score >= 45:
        return "mixed"
    return "defensive"


def _liquidity_score(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    grade_scores = {"A": 90, "B": 75, "C": 55, "D": 35, "F": 15}
    values = []
    for row in rows:
        grade = str(row.get("grade") or "").upper()[:1]
        if grade in grade_scores:
            values.append(grade_scores[grade])
        elif _number_from_any(row.get("avg_dollar_volume")):
            values.append(min(90, max(30, _number_from_any(row.get("avg_dollar_volume")) / 1_000_000)))
    return _average(values)


def _portfolio_environment_score(risk_rows: list[dict[str, Any]], correlation_rows: list[dict[str, Any]]) -> float | None:
    if not risk_rows and not correlation_rows:
        return None
    severity_penalty = 0
    for row in risk_rows:
        severity = str(row.get("severity") or row.get("level") or "").lower()
        severity_penalty += 22 if severity in {"critical", "high"} else 12 if severity in {"medium", "warn", "warning"} else 5
    correlation_penalty = min(25, len(correlation_rows) * 4)
    return max(0, 85 - severity_penalty - correlation_penalty)


def _overall_environment_summary(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No environment inputs are populated yet."
    constructive = [row["category"] for row in rows if row.get("posture") == "constructive"]
    defensive = [row["category"] for row in rows if row.get("posture") == "defensive"]
    if defensive:
        return f"Defensive pressure from {', '.join(defensive[:2])}; constructive support from {', '.join(constructive[:2]) or 'none'}."
    return f"Constructive support from {', '.join(constructive[:3]) or 'mixed inputs'}."


def _overall_portfolio_effect(score: float | None) -> str:
    if score is None:
        return "Do not change sizing until more market inputs are loaded."
    if score >= 70:
        return "Environment allows normal-to-full research sizing when ticker evidence is strong."
    if score >= 45:
        return "Environment supports staged sizing and tighter invalidation checks."
    return "Environment argues for defensive sizing and higher evidence thresholds."


def _overall_next_action(score: float | None) -> str:
    if score is None:
        return "Refresh free market sources and decision models."
    if score >= 70:
        return "Prioritize discounted watchlist names with constructive trend and source support."
    if score >= 45:
        return "Separate cheap-but-weak names from expensive leaders before adding exposure."
    return "Review risk cards and wait for breadth or valuation improvement before adding exposure."
