"""Fundamentals and quality-scoring helpers."""

from __future__ import annotations
from typing import Any
from investment_panel.core.db import db, init_db, query_rows

from investment_panel.core.panel.coerce import _dict_from_value, _meaningful_text, _number_from_any, _optional_number, _string_list



def _is_watch_universe(row: dict[str, Any]) -> bool:
    counts = _dict_from_value(row.get("source_counts"))
    reasons = " ".join(_string_list(row.get("inclusion_reasons"))).lower()
    return bool(counts.get("config_watchlist") or counts.get("watchlist") or "watchlist" in reasons)




def _metric_number(metrics: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = metrics.get(key)
        number = _number_from_any(value)
        if number:
            return number
    return None




def _metric_number_present(metrics: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in metrics:
            continue
        number = _optional_number(metrics.get(key))
        if number is not None:
            return number
    return None




def _pe_from_fundamentals(metrics: dict[str, Any], fundamental_metrics: dict[str, Any]) -> float | None:
    for key in ("trailing_pe", "trailingPE", "price_earnings", "pe_ratio"):
        value = _number_from_any(metrics.get(key))
        if value:
            return round(value, 2)
    market_cap = _metric_number(metrics, "market_cap", "marketCap", "market_cap_basic")
    net_income = _number_from_any(fundamental_metrics.get("net_income"))
    if not net_income:
        revenue = _number_from_any(metrics.get("total_revenue"))
        margin = _number_from_any(metrics.get("net_margin"))
        net_income = revenue * margin if revenue and margin else 0.0
    if market_cap and net_income and net_income > 0:
        return round(market_cap / net_income, 2)
    return None




def _ps_from_fundamentals(metrics: dict[str, Any], fundamental_metrics: dict[str, Any]) -> float | None:
    for key in ("price_to_sales", "priceToSalesTrailing12Months", "price_to_sales_ttm", "price_sales_ttm", "ps_ratio", "ev_sales", "enterprise_to_revenue"):
        value = _number_from_any(metrics.get(key))
        if value:
            return round(value, 2)
    market_cap = _metric_number(metrics, "market_cap", "marketCap", "market_cap_basic", "market_capitalization")
    revenue = _number_from_any(metrics.get("total_revenue")) or _number_from_any(fundamental_metrics.get("revenue"))
    if market_cap and revenue and revenue > 0:
        return round(market_cap / revenue, 2)
    return None




def _roic_from_fundamentals(fundamental_metrics: dict[str, Any], metrics: dict[str, Any] | None = None) -> float | None:
    metrics = metrics or {}
    net_income = _number_from_any(fundamental_metrics.get("net_income"))
    if not net_income:
        revenue = _number_from_any(metrics.get("total_revenue"))
        margin = _number_from_any(metrics.get("net_margin"))
        net_income = revenue * margin if revenue and margin else 0.0
    assets = _number_from_any(fundamental_metrics.get("assets"))
    liabilities = _number_from_any(fundamental_metrics.get("liabilities"))
    capital = assets - liabilities if assets and liabilities and assets > liabilities else assets
    if net_income and capital and capital > 0:
        return round((net_income / capital) * 100, 2)
    margin = _number_from_any(metrics.get("net_margin"))
    if margin:
        return round(margin * 100, 2)
    return None




def _free_cash_flow(metrics: dict[str, Any], fundamental_metrics: dict[str, Any]) -> float | None:
    direct = _metric_number_present(metrics, "free_cash_flow", "freeCashflow", "free_cashflow")
    if direct is None:
        direct = _optional_number(fundamental_metrics.get("free_cash_flow"))
    if direct is not None:
        return direct
    operating_cash_flow = _metric_number_present(metrics, "operating_cash_flow", "operatingCashflow", "totalCashFromOperatingActivities")
    if operating_cash_flow is None:
        operating_cash_flow = _optional_number(fundamental_metrics.get("operating_cash_flow"))
    capex = _metric_number_present(metrics, "capital_expenditures", "capitalExpenditures")
    if capex is None:
        capex = _optional_number(fundamental_metrics.get("capital_expenditures"))
    if operating_cash_flow is None:
        return _fcf_proxy(metrics, fundamental_metrics)
    if capex is None:
        return operating_cash_flow
    return operating_cash_flow + capex if capex < 0 else operating_cash_flow - capex




def _fcf_proxy(metrics: dict[str, Any], fundamental_metrics: dict[str, Any]) -> float | None:
    revenue = _metric_number(metrics, "total_revenue", "totalRevenue", "revenue") or _number_from_any(fundamental_metrics.get("revenue"))
    net_margin = _metric_number_present(metrics, "net_margin", "profitMargins", "profit_margin")
    if net_margin is None:
        net_margin = _optional_number(fundamental_metrics.get("net_margin"))
    if not revenue or net_margin is None:
        return None
    return revenue * net_margin * 0.75




def _rank_percentiles(rows: list[dict[str, Any]], metric: str) -> dict[str, float]:
    ranked = [
        (str(row.get("symbol") or "").upper(), _optional_number(row.get(metric)))
        for row in rows
        if str(row.get("symbol") or "").upper()
    ]
    ranked = [(symbol, value) for symbol, value in ranked if value is not None]
    ranked.sort(key=lambda item: item[1])
    if not ranked:
        return {}
    if len(ranked) == 1:
        return {ranked[0][0]: 100.0}
    return {
        symbol: round(1 + (index / (len(ranked) - 1)) * 98, 2)
        for index, (symbol, _value) in enumerate(ranked)
    }




def _valuation_percentiles_by_symbol(con: Any, symbols: list[str]) -> dict[str, float]:
    normalized = sorted({symbol for symbol in symbols if symbol})
    if not normalized:
        return {}
    placeholders = ", ".join(["?"] * len(normalized))
    rows = query_rows(
        con,
        f"""
        SELECT symbol, metrics
        FROM market_screener_rows
        WHERE symbol IN ({placeholders})
        ORDER BY symbol, observed_at
        """,
        normalized,
    )
    ps_by_symbol: dict[str, list[float]] = {}
    pe_by_symbol: dict[str, list[float]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        metrics = _dict_from_value(row.get("metrics"))
        ps = _ps_from_fundamentals(metrics, {})
        pe = _metric_number(metrics, "forward_pe", "forwardPE", "forward_pe_ratio", "pe_forward")
        if ps:
            ps_by_symbol.setdefault(symbol, []).append(ps)
        if pe:
            pe_by_symbol.setdefault(symbol, []).append(pe)

    output: dict[str, float] = {}
    for symbol in normalized:
        percentiles = []
        if values := ps_by_symbol.get(symbol):
            percentiles.append(_own_history_percentile(values))
        if values := pe_by_symbol.get(symbol):
            percentiles.append(_own_history_percentile(values))
        cleaned = [value for value in percentiles if value is not None]
        if cleaned:
            output[symbol] = round(sum(cleaned) / len(cleaned), 2)
    return output




def _own_history_percentile(values: list[float]) -> float | None:
    cleaned = sorted(value for value in values if value == value)
    if not cleaned:
        return None
    if len(cleaned) == 1:
        return 50.0
    current = values[-1]
    below = sum(1 for value in cleaned if value < current)
    return round((below / (len(cleaned) - 1)) * 100, 2)




def _quality_score(decision: dict[str, Any], metrics: dict[str, Any], valuation: dict[str, Any]) -> float:
    score = _number_from_any(decision.get("action_score") or decision.get("decision_score") or decision.get("score"))
    roic = _metric_number(metrics, "roic", "returnOnInvestedCapital", "return_on_invested_capital") or 0
    pe = _metric_number(metrics, "forward_pe", "forwardPE", "pe_forward", "trailing_pe", "trailingPE", "price_earnings", "pe_ratio") or 0
    upside = _number_from_any(valuation.get("upside_pct"))
    if not score:
        score = 45
    if roic:
        score += min(20, max(-10, roic / 2))
    if pe:
        score += 10 if pe < 20 else -8 if pe > 45 else 2
    if upside:
        score += max(-10, min(15, upside / 3))
    return max(0, min(100, score))




def _star_rating(score: float) -> str:
    stars = max(1, min(5, round(score / 20)))
    return f"{stars}/5"




def _value_signal(valuation: dict[str, Any], metrics: dict[str, Any]) -> str:
    upside = _number_from_any(valuation.get("upside_pct"))
    if upside:
        return f"{upside:+.1f}% fair-value gap"
    pe = _metric_number(metrics, "forward_pe", "forwardPE", "pe_forward")
    if pe:
        return f"{pe:.1f}x fwd P/E"
    pe = _metric_number(metrics, "trailing_pe", "trailingPE", "price_earnings", "pe_ratio")
    if pe:
        return f"{pe:.1f}x P/E"
    return "No valuation row"




def _universe_next_action(decision: dict[str, Any], watch_state: str) -> str:
    catalyst = _meaningful_text(decision.get("catalyst_window"))
    if catalyst:
        return catalyst
    if watch_state == "owned":
        return "Review sizing and thesis fit."
    if watch_state == "watched":
        return "Keep in review queue until evidence or price changes."
    return "Promote only if source consensus or valuation improves."




def _signal_next_action(*values: Any, fallback: str) -> str:
    for value in values:
        text = _meaningful_text(value)
        if text:
            return text
    return fallback




def _watch_sort(row: dict[str, Any]) -> int:
    return {"owned": 0, "watched": 1, "candidate": 2}.get(str(row.get("watch_state")), 3)
