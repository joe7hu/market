"""Source-freshness scoring and labeling."""

from __future__ import annotations
from typing import Any

from investment_panel.core.decision.constants import FRESHNESS_ORDER, STATIC_SOURCES, SYMBOL_RE



def eligibility_detail(status: str) -> str:
    if status == "eligible":
        return "eligible for top-250 decision universe"
    if status == "source_thin":
        return "retained in discovered universe but excluded from the decision universe until a live or derived source supports it"
    return "unsupported or invalid symbol"




def stale_after_label(source_type: str) -> str:
    return {
        "intraday_quote": "4 market hours",
        "closing_quote": "previous close while market is closed",
        "crypto_quote": "36 hours",
        "options": "4 market hours",
        "news": "4 market hours",
        "daily": "1 trading day",
        "arco_thesis": "7 days",
        "filing": "filing cadence",
        "fundamental": "filing cadence",
        "documentation": "not applicable",
    }.get(source_type, "provider contract")




def symbol_freshness_detail(rows: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        key = str(row.get("source_key") or "")
        symbol = key.split(":")[-1].upper()
        if not SYMBOL_RE.match(symbol):
            continue
        detail = result.setdefault(symbol, default_freshness_detail())
        status = str(row.get("freshness_status") or "unknown")
        source_type = str(row.get("source_type") or "")
        if source_type in {"intraday_quote", "closing_quote", "crypto_quote"}:
            detail["quote_freshness"] = best_freshness(detail["quote_freshness"], status)
        elif source_type == "daily":
            detail["daily_analysis_freshness"] = worst_freshness(detail["daily_analysis_freshness"], status)
        elif source_type == "filing":
            detail["filing_freshness"] = worst_freshness(detail["filing_freshness"], status)
        elif source_type == "arco_thesis":
            detail["thesis_freshness"] = worst_freshness(detail["thesis_freshness"], status)
    for detail in result.values():
        detail["overall_decision_freshness"] = overall_decision_freshness(detail)
    return result




def default_freshness_detail() -> dict[str, str]:
    return {
        "quote_freshness": "missing",
        "daily_analysis_freshness": "missing",
        "filing_freshness": "not_applicable",
        "thesis_freshness": "not_applicable",
        "overall_decision_freshness": "missing",
    }




def worst_freshness(current: str, incoming: str) -> str:
    if current in {"missing", "not_applicable"}:
        return incoming
    return current if FRESHNESS_ORDER.get(current, 2) <= FRESHNESS_ORDER.get(incoming, 2) else incoming




def best_freshness(current: str, incoming: str) -> str:
    if current in {"missing", "not_applicable"}:
        return incoming
    return current if FRESHNESS_ORDER.get(current, 2) >= FRESHNESS_ORDER.get(incoming, 2) else incoming




def overall_decision_freshness(detail: dict[str, str]) -> str:
    core_statuses = [detail.get("quote_freshness", "missing"), detail.get("daily_analysis_freshness", "missing")]
    if any(status in {"failed"} for status in core_statuses):
        return "failed"
    if any(status in {"stale", "missing", "unknown"} for status in core_statuses):
        return "stale"
    thesis_status = detail.get("thesis_freshness")
    if thesis_status in {"failed", "stale"}:
        return "stale"
    return "fresh"




def top_source_cluster(counts: dict[str, Any]) -> str:
    ranked_sources = {
        "arco_thesis": 90,
        "public_disclosure_transaction": 85,
        "13f_holding": 80,
        "13f": 80,
        "news": 75,
        "analyst_estimate": 70,
        "earnings_setup": 68,
        "earnings": 65,
        "technical": 55,
        "sepa": 54,
        "liquidity": 53,
        "correlation": 52,
        "valuation": 51,
        "etf_premium": 51,
        "crypto_fundamental": 51,
        "options_payoff": 50,
        "tradingview_alert": 48,
        "tradingview_watchlist": 47,
        "tradingview_chart_state": 46,
        "tradingview": 45,
        "tradingview_search": 44,
        "yfinance": 40,
        "portfolio": 35,
        "broker_position:ibkr": 38,
        "broker_position:moomoo": 30,
    }
    eligible = [(key, int(value or 0)) for key, value in counts.items() if key not in STATIC_SOURCES and int(value or 0) > 0]
    if not eligible:
        return "-"
    return max(eligible, key=lambda item: (ranked_sources.get(item[0], 10), item[1], item[0]))[0]
