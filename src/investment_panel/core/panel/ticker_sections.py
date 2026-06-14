"""Structured ticker-dossier composition.

Turns the flat per-ticker read-model table map (produced by
``ticker_payload_tables``) into one section-organized ``dossier`` model: quote,
fundamentals, estimates, technicals, options, ownership, sources, thesis,
portfolio, plus the decision brief synthesized by the decision engine.

Each ``build_<section>`` function is pure — ``(symbol, tables) -> dict`` — so a
single section can be reused on other surfaces without recomputing the whole
dossier. Every section carries a ``coverage`` block (status/rows/sources) so
callers can degrade gracefully instead of guessing from missing keys. Raw
numbers are emitted as-is; presentation formatting lives in the UI.
"""

from __future__ import annotations

from typing import Any

from investment_panel.core.coercion import (
    iso_or_none,
    number_from_any,
    optional_number,
    parse_dt_utc,
    parse_json_dict,
    parse_json_list,
    string_list,
)
from investment_panel.core.panel.coerce import _normalize_symbol_token

# Single source of truth for which read-model tables feed each dossier section.
# Used to compute per-section coverage instead of two divergent family maps.
SECTION_TABLE_SOURCES: dict[str, tuple[str, ...]] = {
    "decision": ("symbol_decision_snapshot", "decision_queue", "opportunities_ranked", "candidates", "discovered_universe"),
    "quote": ("quotes",),
    "fundamentals": ("fundamentals", "universe_screen"),
    "estimates": ("analyst_estimates", "earnings", "earnings_setups"),
    "technicals": ("technicals", "sepa", "liquidity"),
    "options": ("options_ticker_signals", "options_expiry_signals", "options_payoff_scenarios", "options_chain", "options_expiries"),
    "ownership": ("ownership_consensus", "disclosures"),
    "sources": ("source_consensus", "ticker_source_signals", "feed_signals", "news", "opportunity_sources"),
    "thesis": ("thesis_monitor", "theses", "research_packets", "memos"),
    "portfolio": ("portfolio", "portfolio_risk_cards", "exposure_clusters", "correlation_edges", "correlations", "review_actions"),
}

# Per-row recency keys (used to pick the latest row within a table).
_TS_KEYS = ("observed_at", "as_of", "date", "created_at", "updated_at", "event_date", "filed_date", "latest_filed", "period_end", "filing_date")
# Observation-only keys for the dossier-level freshness stamp — excludes
# forward-looking event/period dates so ``as_of`` reflects when data was seen.
_OBSERVED_KEYS = ("observed_at", "as_of", "created_at", "updated_at", "filed_date", "filing_date", "date")


def build_ticker_dossier(symbol: str, tables: dict[str, list[dict[str, Any]]], decision_brief: dict[str, Any]) -> dict[str, Any]:
    """Compose the full section-organized dossier for ``symbol``."""

    sections = {
        "identity": build_identity(symbol, tables),
        "quote": build_quote(symbol, tables, decision_brief),
        "decision": decision_brief or {},
        "fundamentals": build_fundamentals(symbol, tables),
        "estimates": build_estimates(symbol, tables),
        "technicals": build_technicals(symbol, tables, decision_brief),
        "options": build_options(symbol, tables, decision_brief),
        "ownership": build_ownership(symbol, tables),
        "sources": build_sources(symbol, tables),
        "thesis": build_thesis(symbol, tables),
        "portfolio": build_portfolio(symbol, tables, decision_brief),
    }
    sections["coverage"] = _coverage_overview(tables, sections)
    return sections


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #


def _section_coverage(tables: dict[str, list[dict[str, Any]]], section: str) -> dict[str, Any]:
    source_tables = SECTION_TABLE_SOURCES.get(section, ())
    present = {name: tables.get(name) or [] for name in source_tables if tables.get(name)}
    rows = sum(len(value) for value in present.values())
    status = "live" if rows else "missing"
    if section == "options" and rows:
        payoff = tables.get("options_payoff_scenarios") or []
        if payoff and all(_is_expired(row) for row in payoff):
            status = "expired"
    return {"status": status, "rows": rows, "sources": list(present)}


def _coverage_overview(tables: dict[str, list[dict[str, Any]]], sections: dict[str, dict[str, Any]]) -> dict[str, Any]:
    families = {name: _section_coverage(tables, name) for name in SECTION_TABLE_SOURCES}
    live = [name for name, cov in families.items() if cov["status"] == "live"]
    missing = [name for name, cov in families.items() if cov["status"] == "missing"]
    return {
        "families": families,
        "live": live,
        "missing": missing,
        "loaded_families": len(live),
        "total_families": len(families),
        "as_of": _latest_timestamp(tables),
    }


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


def build_identity(symbol: str, tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    universe = _first(tables, "universe_screen", "discovered_universe")
    candidate = _first(tables, "candidates", "signals")
    fundamentals = _first(tables, "fundamentals")
    return {
        "symbol": symbol,
        "name": _text(universe.get("name") or candidate.get("name")) or symbol,
        "sector": _text(universe.get("sector") or fundamentals.get("sector") or candidate.get("category")),
        "asset_class": _text(universe.get("asset_class") or candidate.get("asset_class")) or "equity",
        "exchange": _text(universe.get("exchange") or fundamentals.get("exchange")),
        "watch_state": _text(universe.get("watch_state")),
        "tradingview_symbol": _tradingview_symbol(symbol, tables),
        "coverage": {"status": "live" if (universe or candidate) else "missing", "rows": 0, "sources": []},
    }


def build_quote(symbol: str, tables: dict[str, list[dict[str, Any]]], brief: dict[str, Any]) -> dict[str, Any]:
    canonical = parse_json_dict((brief or {}).get("canonical_quote"))
    if canonical:
        return {**canonical, "coverage": _section_coverage(tables, "quote")}
    quote = _latest(tables.get("quotes") or [])
    return {
        "symbol": symbol,
        "price": optional_number(quote.get("price") or quote.get("close") or quote.get("last")),
        "change_pct": optional_number(quote.get("change_pct") or quote.get("percent_change")),
        "observed_at": iso_or_none(quote.get("observed_at") or quote.get("as_of") or quote.get("date")),
        "source": _text(quote.get("source")) or "quote",
        "label": "Market quote",
        "coverage": _section_coverage(tables, "quote"),
    }


def build_fundamentals(symbol: str, tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    sec_rows = [row for row in (tables.get("fundamentals") or []) if _text(row.get("source")) == "sec_companyfacts"]
    latest = max(sec_rows, key=lambda row: _row_timestamp(row), default={})
    metrics = parse_json_dict(latest.get("metrics"))
    market = _first(tables, "universe_screen")
    return {
        "sec": {
            "form_type": _text(latest.get("form_type")),
            "filing_date": iso_or_none(latest.get("filing_date") or latest.get("period_end")),
            "period_end": iso_or_none(latest.get("period_end")),
            "source_url": _text(latest.get("source_url")) or None,
            "revenue": optional_number(metrics.get("revenue")),
            "revenue_growth": optional_number(metrics.get("revenue_growth")),
            "net_income": optional_number(metrics.get("net_income")),
            "net_margin": optional_number(metrics.get("net_margin")),
            "free_cash_flow": optional_number(metrics.get("free_cash_flow")),
            "fcf_margin": optional_number(metrics.get("fcf_margin")),
            "assets": optional_number(metrics.get("assets")),
            "liabilities": optional_number(metrics.get("liabilities")),
            "cash": optional_number(metrics.get("cash")),
            "debt_to_assets": optional_number(metrics.get("debt_to_assets")),
        },
        "market": {
            "market_cap": optional_number(market.get("market_cap")),
            "ps_ratio": optional_number(market.get("ps_ratio")),
            "pe_ratio": optional_number(market.get("pe_ratio")),
            "forward_pe": optional_number(market.get("forward_pe")),
            "forward_pe_source": _text(market.get("forward_pe_source")),
            "fcf_yield": optional_number(market.get("fcf_yield")),
            "roic": optional_number(market.get("roic")),
            "roic_source": _text(market.get("roic_source")),
            "quality_score": optional_number(market.get("quality_score")),
            "value_signal": _text(market.get("value_signal")),
        },
        "coverage": _section_coverage(tables, "fundamentals"),
    }


def build_estimates(symbol: str, tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    estimate_row = _latest(tables.get("analyst_estimates") or [])
    estimates = parse_json_dict(estimate_row.get("estimates"))
    targets = parse_json_dict(estimates.get("analyst_price_targets"))
    event = _latest(tables.get("earnings") or [])
    setup = _latest(tables.get("earnings_setups") or [])
    return {
        "analyst": {
            "as_of": iso_or_none(estimate_row.get("as_of")),
            "earnings_estimate": parse_json_list(estimates.get("earnings_estimate")),
            "revenue_estimate": parse_json_list(estimates.get("revenue_estimate")),
            "price_targets": {
                "mean": optional_number(targets.get("mean")),
                "low": optional_number(targets.get("low")),
                "high": optional_number(targets.get("high")),
                "current": optional_number(targets.get("current")),
            },
        },
        "earnings_event": {
            "event_date": iso_or_none(event.get("event_date")),
            "event_type": _text(event.get("event_type")),
        },
        "earnings_setup": {
            "setup_type": _text(setup.get("setup_type")),
            "verdict": _text(setup.get("verdict")),
            "score": optional_number(setup.get("score")),
            "revision_score": optional_number(setup.get("revision_score")),
            "surprise_score": optional_number(setup.get("surprise_score")),
            "sentiment_score": optional_number(setup.get("sentiment_score")),
            "estimate_spread_score": optional_number(setup.get("estimate_spread_score")),
            "event_date": iso_or_none(setup.get("event_date")),
        },
        "coverage": _section_coverage(tables, "estimates"),
    }


def build_technicals(symbol: str, tables: dict[str, list[dict[str, Any]]], brief: dict[str, Any]) -> dict[str, Any]:
    tech = _latest(tables.get("technicals") or [])
    features = parse_json_dict(tech.get("features"))
    sepa = _latest(tables.get("sepa") or [])
    liquidity = _latest(tables.get("liquidity") or [])

    def feat(key: str) -> float | None:
        return optional_number(tech.get(key) if tech.get(key) is not None else features.get(key))

    return {
        "trend": {
            "close": feat("close"),
            "ma20": feat("ma20"),
            "ma50": feat("ma50"),
            "ma200": feat("ma200"),
            "drawdown_from_high": feat("drawdown_from_high"),
            "range_recovery": feat("range_recovery"),
        },
        "momentum": {
            "technical_score": feat("technical_score"),
            "return_20d": feat("return_20d"),
            "return_60d": feat("return_60d"),
            "return_3m": feat("return_3m"),
            "return_ytd": feat("return_ytd"),
            "return_1y": feat("return_1y"),
            "rel_volume_1m": feat("rel_volume_1m"),
            "atr_pct_1m": feat("atr_pct_1m"),
            "as_of": iso_or_none(tech.get("date") or tech.get("as_of")),
        },
        "sepa": {
            "stage": _text(sepa.get("stage")),
            "verdict": _text(sepa.get("verdict")),
            "score": optional_number(sepa.get("score")),
            "checklist": parse_json_dict(sepa.get("checklist")),
            "as_of": iso_or_none(sepa.get("as_of")),
        },
        "liquidity": {
            "grade": _text(liquidity.get("grade")),
            "avg_daily_volume": optional_number(liquidity.get("avg_daily_volume")),
            "avg_dollar_volume": optional_number(liquidity.get("avg_dollar_volume")),
            "impact_1pct_adv_bps": optional_number(liquidity.get("impact_1pct_adv_bps")),
            "amihud_illiquidity": optional_number(liquidity.get("amihud_illiquidity")),
        },
        "chart_context": parse_json_dict((brief or {}).get("chart_context")),
        "coverage": _section_coverage(tables, "technicals"),
    }


def build_options(symbol: str, tables: dict[str, list[dict[str, Any]]], brief: dict[str, Any]) -> dict[str, Any]:
    signal = _latest(tables.get("options_ticker_signals") or [])
    expiries = sorted(
        (_option_expiry_row(row) for row in (tables.get("options_expiry_signals") or [])),
        key=lambda row: row.get("expiry") or "",
    )
    capabilities = [
        {
            "provider": _text(row.get("provider")),
            "status": _text(row.get("status")),
            "supports_open_interest": bool(row.get("supports_open_interest")),
            "supports_volume": bool(row.get("supports_volume")),
        }
        for row in (tables.get("options_provider_capabilities") or [])
    ]
    return {
        "signal": {
            "status": _text(signal.get("status")),
            "source": _text(signal.get("source")),
            "as_of": iso_or_none(signal.get("as_of")),
            "nearest_expiry": iso_or_none(signal.get("nearest_expiry")),
            "nearest_dte": optional_number(signal.get("nearest_dte")),
            "atm_iv": optional_number(signal.get("atm_iv")),
            "iv_regime": _text(signal.get("iv_regime")),
            "expected_move": optional_number(signal.get("expected_move")),
            "expected_move_pct": optional_number(signal.get("expected_move_pct")),
            "skew_signal": _text(signal.get("skew_signal")),
            "put_call_iv_skew": optional_number(signal.get("put_call_iv_skew")),
            "spread_quality": _text(signal.get("spread_quality")),
            "hedge_summary": _text(signal.get("hedge_summary")),
            "income_summary": _text(signal.get("income_summary")),
        },
        "unavailable_signals": [row for row in (signal.get("unavailable_signals") or []) if isinstance(row, dict)],
        "expiries": expiries,
        "capabilities": capabilities,
        "context": parse_json_dict((brief or {}).get("options_context")),
        "coverage": _section_coverage(tables, "options"),
    }


def build_ownership(symbol: str, tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    consensus = _first(tables, "ownership_consensus")
    filings = [
        {
            "filer_name": _text(row.get("filer_name") or row.get("trader_name")),
            "trader_name": _text(row.get("trader_name")),
            "action": _text(row.get("action")),
            "amount": _text(row.get("amount")),
            "event_date": iso_or_none(row.get("event_date")),
            "filed_date": iso_or_none(row.get("filed_date")),
            "source_type": _text(row.get("source_type")),
            "source_url": _text(row.get("source_url")) or None,
            "source_caveat": _text(row.get("source_caveat")),
        }
        for row in sorted(tables.get("disclosures") or [], key=lambda row: _text(row.get("filed_date")), reverse=True)
    ]
    return {
        "institutional": {
            "holders": optional_number(consensus.get("holders")),
            "investors": string_list(consensus.get("investors")),
            "holder_names": string_list(consensus.get("holder_names")),
            "net_buys": optional_number(consensus.get("net_buys")),
            "net_sells": optional_number(consensus.get("net_sells")),
            "net_activity": optional_number(consensus.get("net_activity")),
            "total_value": optional_number(consensus.get("total_value")),
            "latest_filed": iso_or_none(consensus.get("latest_filed")),
        },
        "filings": filings,
        "coverage": _section_coverage(tables, "ownership"),
    }


def build_sources(symbol: str, tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    consensus = [
        {
            "source_name": _text(row.get("source_name")),
            "content_type": _text(row.get("content_type") or row.get("source_family")),
            "net_consensus": _text(row.get("net_consensus") or row.get("recommendation")),
            "items_count": optional_number(row.get("items_count")),
            "latest_at": iso_or_none(row.get("latest_at") or row.get("observed_at")),
        }
        for row in (tables.get("source_consensus") or [])
    ]
    signals = [
        {
            "source_name": _text(row.get("source_name") or row.get("source_id")),
            "signal_type": _text(row.get("signal_type") or row.get("source_family")),
            "sentiment": _text(row.get("sentiment") or row.get("direction")),
            "confidence": optional_number(row.get("confidence")),
            "observed_at": iso_or_none(row.get("observed_at") or row.get("as_of")),
            "title": _text(row.get("title")),
        }
        for row in (tables.get("ticker_source_signals") or [])
    ]
    evidence = _evidence_items(tables)
    return {
        "consensus": consensus,
        "signals": signals,
        "evidence": evidence,
        "signal_count": len(consensus) + len(signals),
        "coverage": _section_coverage(tables, "sources"),
    }


def build_thesis(symbol: str, tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    state = _first(tables, "thesis_monitor", "theses")
    packet = _latest(tables.get("research_packets") or [])
    return {
        "state": {
            "thesis": _text(state.get("thesis") or state.get("why_owned_watched")),
            "status": _text(state.get("status")),
            "needs_review": bool(state.get("needs_review")),
            "review_reason": _text(state.get("review_reason")),
            "invalidation": _text(state.get("invalidation")),
            "last_reviewed": iso_or_none(state.get("last_reviewed") or state.get("as_of") or state.get("updated_at")),
        },
        "research_packet": {
            "decision": _text(packet.get("decision")),
            "conviction": _text(packet.get("conviction")),
            "bull_case": string_list(packet.get("bull_case")),
            "bear_case": string_list(packet.get("bear_case")),
            "why_now": string_list(packet.get("why_now")),
            "invalidation": string_list(packet.get("invalidation")),
            "entry_plan": parse_json_dict(packet.get("entry_plan")),
            "position_sizing": parse_json_dict(packet.get("position_sizing")),
            "has_position": bool(packet.get("has_position")),
            "evidence_count": optional_number(packet.get("evidence_count")),
            "created_at": iso_or_none(packet.get("created_at")),
        },
        "coverage": _section_coverage(tables, "thesis"),
    }


def build_portfolio(symbol: str, tables: dict[str, list[dict[str, Any]]], brief: dict[str, Any]) -> dict[str, Any]:
    position = _first(tables, "portfolio")
    fit = parse_json_dict((brief or {}).get("portfolio_fit"))
    return {
        "owned": bool(position) or bool(fit.get("owned")),
        "position": {
            "market_value": optional_number(position.get("market_value") or position.get("value")),
            "weight": optional_number(position.get("weight") or position.get("portfolio_weight")),
            "quantity": optional_number(position.get("quantity") or position.get("shares")),
            "cost_basis": optional_number(position.get("cost_basis") or position.get("avg_price")),
            "unrealized_pnl_pct": optional_number(position.get("unrealized_pnl_pct") or position.get("gain_pct")),
        },
        "fit": fit,
        "risk_cards": list(tables.get("portfolio_risk_cards") or []),
        "exposure_clusters": list(tables.get("exposure_clusters") or []),
        "correlations": [
            {
                "peer_symbol": _text(row.get("peer_symbol") or row.get("related_symbol") or row.get("benchmark")),
                "correlation": optional_number(row.get("correlation") or row.get("value")),
            }
            for row in (tables.get("correlation_edges") or tables.get("correlations") or [])
        ],
        "review_actions": list(tables.get("review_actions") or []),
        "coverage": _section_coverage(tables, "portfolio"),
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _evidence_items(tables: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for table in ("feed_signals", "news", "opportunity_sources"):
        for row in tables.get(table) or []:
            items.append(
                {
                    "source": _text(row.get("source_name") or row.get("source") or row.get("source_key")),
                    "title": _text(row.get("title") or row.get("event") or row.get("summary") or row.get("thesis")),
                    "signal": _text(row.get("sentiment") or row.get("decision") or row.get("action")),
                    "date": iso_or_none(row.get("published_at") or row.get("observed_at") or row.get("event_date") or row.get("date")),
                    "family": table,
                }
            )
    return items


def _option_expiry_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "expiry": iso_or_none(row.get("expiry") or row.get("expiration")),
        "dte": optional_number(row.get("dte")),
        "atm_strike": optional_number(row.get("atm_strike")),
        "atm_iv": optional_number(row.get("atm_iv")),
        "expected_move": optional_number(row.get("expected_move")),
        "expected_move_pct": optional_number(row.get("expected_move_pct")),
        "put_call_iv_skew": optional_number(row.get("put_call_iv_skew")),
        "spread_quality": _text(row.get("spread_quality")),
    }


def _tradingview_symbol(symbol: str, tables: dict[str, list[dict[str, Any]]]) -> str:
    normalized = symbol.upper()
    for row in tables.get("tradingview_chart_state") or []:
        explicit = _text(row.get("symbol"))
        if ":" in explicit:
            return explicit.upper()
    for row in tables.get("tradingview_symbol_search") or []:
        exchange = _text(row.get("exchange"))
        row_symbol = _text(row.get("symbol") or row.get("ticker"))
        if exchange and row_symbol and ":" not in row_symbol:
            return f"{exchange}:{row_symbol}".upper()
        if ":" in row_symbol:
            return row_symbol.upper()
    for row in tables.get("quotes") or []:
        raw_symbol = _text(parse_json_dict(row.get("raw")).get("symbol"))
        if ":" in raw_symbol:
            return raw_symbol.upper()
    if normalized.endswith("-USD"):
        return f"COINBASE:{normalized.replace('-USD', 'USD')}"
    if normalized in {"SPY", "QQQ"}:
        return f"AMEX:{normalized}"
    return f"NASDAQ:{normalized}"


def _is_expired(row: dict[str, Any]) -> bool:
    from datetime import date

    expiry = row.get("expiry") or row.get("expiration")
    parsed = parse_dt_utc(expiry)
    if parsed is not None:
        return parsed.date() < date.today()
    return number_from_any(row.get("dte") or row.get("days_to_expiry")) < 0


def _first(tables: dict[str, list[dict[str, Any]]], *names: str) -> dict[str, Any]:
    for name in names:
        rows = tables.get(name) or []
        if rows:
            return rows[0]
    return {}


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    return max(rows, key=_row_timestamp)


def _row_timestamp(row: dict[str, Any]) -> float:
    best = 0.0
    for key in _TS_KEYS:
        parsed = parse_dt_utc(row.get(key))
        if parsed is not None:
            best = max(best, parsed.timestamp())
    return best


def _latest_timestamp(tables: dict[str, list[dict[str, Any]]]) -> str | None:
    # Freshness stamp: the most recent observation that is not in the future
    # (some source rows carry forward-looking event dates in observed fields).
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).timestamp()
    best: float = 0.0
    best_iso: str | None = None
    for rows in tables.values():
        for row in rows or []:
            for key in _OBSERVED_KEYS:
                parsed = parse_dt_utc(row.get(key))
                if parsed is None:
                    continue
                stamp = parsed.timestamp()
                if best < stamp <= now:
                    best = stamp
                    best_iso = iso_or_none(row.get(key))
    return best_iso


def _text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    return str(value)
