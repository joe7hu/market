"""Options and tab-summary helpers for ticker decision briefs."""

from __future__ import annotations

from datetime import date
from typing import Any

from investment_panel.core.decision.brief_coerce import (
    _first_row,
    _fmt_money,
    _fmt_pct,
    _number,
    _object,
    _parse_date,
    _text,
    _text_join,
)


def _is_option_expired(row: dict[str, Any], today: date | None = None) -> bool:
    expiry = _parse_date(row.get("expiry") or row.get("expiration"))
    if expiry is not None:
        return expiry < (today or date.today())
    dte = _number(row.get("dte") or row.get("days_to_expiry"), 0.0)
    return dte < 0


def _best_option(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    finite_loss = [row for row in rows if _number(row.get("max_loss")) < 0]
    return min(finite_loss or rows, key=lambda row: abs(_number(row.get("max_loss"), 10**12)))


def _missing_families(tables: dict[str, list[dict[str, Any]]]) -> list[str]:
    checks = {
        "quote": ["quotes"],
        "thesis": ["theses", "memos"],
        "news": ["news"],
        "filings": ["disclosures"],
    }
    return [label for label, keys in checks.items() if not any(tables.get(key) for key in keys)]


def _max_loss(option: dict[str, Any]) -> str:
    loss = _number(option.get("max_loss"))
    if loss < 0:
        return _fmt_money(abs(loss))
    return "No bounded-loss option scenario selected."


def _options_context(option: dict[str, Any], option_rows: list[dict[str, Any]], setup: dict[str, Any]) -> dict[str, Any]:
    expired_count = sum(1 for row in option_rows if _is_option_expired(row))
    live_count = len(option_rows) - expired_count
    if not option:
        if expired_count:
            return {
                "status": "expired",
                "summary": "All options scenarios are expired; refresh the option chain before using options.",
                "scenario_count": len(option_rows),
                "live_scenario_count": 0,
                "expired_scenario_count": expired_count,
            }
        return {"status": "missing", "summary": "No options scenario row loaded.", "scenario_count": 0, "live_scenario_count": 0}
    spot = _number(option.get("spot"))
    breakevens = option.get("breakevens") if isinstance(option.get("breakevens"), list) else []
    first_breakeven = _number(breakevens[0]) if breakevens else 0.0
    move_to_breakeven = ((first_breakeven / spot) - 1) * 100 if spot and first_breakeven else 0.0
    return {
        "status": "live",
        "summary": f"{_text(option.get('strategy_type')).replace('_', ' ')} expires {option.get('expiry')}; breakeven move {_fmt_pct(move_to_breakeven)}.",
        "scenario_count": len(option_rows),
        "live_scenario_count": live_count,
        "expired_scenario_count": expired_count,
        "iv": _number(option.get("iv")),
        "dte": _number(option.get("dte")),
        "breakeven": first_breakeven,
        "max_loss": _max_loss(option),
        "event_fit": "Check expiry against catalyst window: " + _text(setup.get("catalyst")),
    }


def _ticker_tab_summaries(tables: dict[str, list[dict[str, Any]]], setup: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    fundamentals = _first_row(tables, "fundamentals")
    estimates = _first_row(tables, "analyst_estimates")
    financial_valuation = _first_row(tables, "valuations")
    earnings = _first_row(tables, "earnings_setups", "earnings")
    memo = _first_row(tables, "research_packets", "memos", "theses")
    option_rows = tables.get("options_payoff_scenarios") or []
    expired_options = sum(1 for row in option_rows if _is_option_expired(row))
    live_options = len(option_rows) - expired_options
    return {
        "Evidence Stack": [
            {"label": "For", "value": str(len(tables.get("opportunity_sources") or [])), "caption": "source rows supporting current setup"},
            {"label": "Against", "value": _text(setup.get("stance")), "caption": "stance from gates and valuation"},
            {"label": "Open Inputs", "value": str(len(_missing_families(tables))), "caption": "source families not loaded for this ticker"},
        ],
        "Fundamentals": [
            {"label": "Latest Filing", "value": _text(fundamentals.get("form_type")) or "Not loaded", "caption": _text(fundamentals.get("filing_date") or fundamentals.get("period_end")) or "No SEC company-facts row"},
            {"label": "Revenue", "value": _fmt_money(_number(_object(fundamentals.get("metrics")).get("revenue"))), "caption": "SEC company facts"},
            {"label": "Net Margin", "value": _fmt_pct(_number(_object(fundamentals.get("metrics")).get("net_margin")) * 100), "caption": "latest annual period"},
        ],
        "Estimates": [
            {"label": "Earnings Setup", "value": _text(earnings.get("verdict")) or "Not loaded", "caption": f"score {_number(earnings.get('score')):.0f}" if earnings else "No earnings setup row"},
            {"label": "Event", "value": _text(earnings.get("event_date")) or "Not loaded", "caption": "next earnings/event row"},
            {"label": "Estimate Snapshot", "value": "Loaded" if estimates else "Not loaded", "caption": _text(estimates.get("as_of")) or "No analyst estimate row"},
        ],
        "Financials": [
            {"label": "Best Fair Value", "value": _fmt_money(_number(financial_valuation.get("fair_value"))), "caption": _text(financial_valuation.get("method"))},
            {"label": "Modeled Upside", "value": _fmt_pct(_number(financial_valuation.get("upside_pct"))), "caption": "relative to model quote"},
            {"label": "Confidence", "value": _text(_object(financial_valuation.get("diagnostics")).get("confidence")) or "Not scored", "caption": _text(_object(financial_valuation.get("diagnostics")).get("note")) or "No valuation diagnostics row"},
        ],
        "Options": [
            {"label": "Live Scenarios", "value": str(live_options), "caption": "usable current option setups"},
            {"label": "Expired Scenarios", "value": str(expired_options), "caption": "hidden from live risk plan"},
            {"label": "Status", "value": "Expired" if option_rows and not live_options else "Loaded" if live_options else "Not loaded", "caption": "option chain usability"},
        ],
        "TradingView": [
            {"label": "Personal Context", "value": "Loaded" if any(tables.get(key) for key in ("tradingview_symbol_search", "tradingview_watchlists", "tradingview_alerts", "tradingview_chart_state")) else "Not loaded", "caption": "watchlists, alerts, search, chart state"},
            {"label": "Chart", "value": "Embedded", "caption": "daily technical chart available on overview"},
        ],
        "News": [
            {"label": "Ticker News", "value": str(len(tables.get("news") or [])), "caption": "ticker-specific news rows"},
            {"label": "Catalysts", "value": str(len(tables.get("catalysts") or [])), "caption": _text(setup.get("catalyst"))},
        ],
        "Filings": [
            {"label": "Disclosure Rows", "value": str(len(tables.get("disclosures") or [])), "caption": "tracked filings/disclosures"},
            {"label": "Portfolio Rows", "value": str(len(tables.get("portfolio") or [])), "caption": "current position context"},
        ],
        "Memos": [
            {"label": "Decision Memo", "value": _text(memo.get("decision") or memo.get("conviction")) or "Not loaded", "caption": _text_join(memo.get("why_now")) or "No ticker-specific memo row"},
            {"label": "Entry Plan", "value": _text(_object(memo.get("entry_plan")).get("initial_weight")) or "Review required", "caption": _text(_object(memo.get("entry_plan")).get("ideal_entry"))},
        ],
    }
