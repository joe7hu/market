"""Decision-grade universe, freshness, and queue read models."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from investment_panel.core.db import json_dumps, query_rows
from investment_panel.core.instruments import infer_asset_class, normalize_symbol


SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,14}$")
INTRADAY_STALE_HOURS = 4
ARCO_STALE_DAYS = 7
DAILY_STALE_DAYS = 1
FILING_STALE_DAYS = 120
MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
STATIC_SOURCES = {"config_watchlist", "config", "instrument", "instruments", "candidate"}
PRIMARY_EVIDENCE_SOURCES = {
    "arco_thesis",
    "news",
    "public_disclosure_transaction",
    "13f_holding",
    "13f",
    "analyst_estimate",
    "earnings",
    "earnings_setup",
    "tradingview_alert",
}
DAILY_ANALYSIS_SOURCES = {"technical", "sepa", "liquidity", "correlation", "valuation", "earnings_setup", "options_payoff"}
FRESHNESS_ORDER = {"failed": 0, "stale": 1, "missing": 1, "unknown": 2, "documentation": 3, "not_applicable": 3, "fresh": 4}


def refresh_decision_read_models(con: Any, config: Any | None = None) -> dict[str, Any]:
    """Build and persist the decision read models from current source tables."""

    watchlist = watchlist_from_config(config)
    source_freshness = build_source_freshness(con)
    universe = build_discovered_universe(con, watchlist)
    queue = build_decision_queue(con, universe, source_freshness)
    snapshots = build_symbol_decision_snapshots(queue, universe)

    persist_source_freshness(con, source_freshness)
    persist_discovered_universe(con, universe)
    persist_decision_queue(con, queue)
    persist_symbol_decision_snapshots(con, snapshots)
    return {
        "status": "decision_models_refreshed",
        "discovered_universe": len(universe),
        "source_freshness": len(source_freshness),
        "decision_queue": len(queue),
        "symbol_decision_snapshots": len(snapshots),
        "decision_universe_members": sum(1 for row in universe if row.get("decision_universe_member")),
        "stale_queue_rows": sum(1 for row in queue if row.get("action_grade") == "Stale"),
    }


def discovered_universe_rows(con: Any) -> list[dict[str, Any]]:
    return [decode(row) for row in query_rows(con, "SELECT * FROM discovered_universe ORDER BY universe_rank NULLS LAST, symbol LIMIT 1000")]


def decision_queue_rows(con: Any) -> list[dict[str, Any]]:
    return [decode(row) for row in query_rows(con, "SELECT * FROM decision_queue ORDER BY rank NULLS LAST, score DESC NULLS LAST LIMIT 250")]


def source_freshness_rows(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(con, "SELECT * FROM source_freshness ORDER BY docs_only ASC, checked_at DESC NULLS LAST, source_key")
    return [decode(row) for row in rows]


def symbol_decision_snapshot_rows(con: Any) -> list[dict[str, Any]]:
    return [decode(row) for row in query_rows(con, "SELECT * FROM symbol_decision_snapshots ORDER BY as_of DESC NULLS LAST, symbol LIMIT 250")]


def decision_readiness_rows(con: Any) -> list[dict[str, Any]]:
    """Decision-readiness contract for the app and API.

    This intentionally derives from the persisted decision queue so the UI can
    show both the underlying decision score and the action score after gates.
    """

    queue = [decode(row) for row in query_rows(con, "SELECT * FROM decision_queue ORDER BY rank ASC, action_score DESC NULLS LAST LIMIT 250")]
    portfolio_count = int(query_rows(con, "SELECT count(*) AS count FROM portfolio_positions")[0].get("count") or 0)
    output: list[dict[str, Any]] = []
    for row in queue:
        basis = row.get("decision_basis") if isinstance(row.get("decision_basis"), dict) else {}
        source_counts = basis.get("source_counts") if isinstance(basis.get("source_counts"), dict) else {}
        blockers = readiness_blockers(row, source_counts, portfolio_count)
        missing_inputs = readiness_missing_inputs(row, source_counts, portfolio_count)
        status = readiness_status(row, blockers, missing_inputs)
        output.append(
            {
                "symbol": row.get("symbol"),
                "status": status,
                "decision_score": row.get("decision_score"),
                "action_score": row.get("action_score"),
                "freshness_status": row.get("freshness_status"),
                "blockers": blockers,
                "missing_inputs": missing_inputs,
                "next_action": readiness_next_action(status, blockers, missing_inputs),
                "source_counts": source_counts,
                "portfolio_fit": readiness_portfolio_fit(row, portfolio_count),
                "as_of": row.get("as_of"),
            }
        )
    return output


def symbol_decision_snapshot(con: Any, symbol: str) -> dict[str, Any] | None:
    rows = query_rows(con, "SELECT * FROM symbol_decision_snapshots WHERE symbol = ? LIMIT 1", [symbol.upper()])
    return decode(rows[0]) if rows else None


def build_discovered_universe(con: Any, config_watchlist: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}

    def touch(
        symbol: Any,
        source: str,
        reason: str,
        observed_at: Any = None,
        name: str | None = None,
        asset_class: str | None = None,
        strength: float = 1.0,
        event_at: Any = None,
    ) -> None:
        normalized = normalize_symbol(str(symbol or ""))
        if not normalized or not SYMBOL_RE.match(normalized):
            return
        row = items.setdefault(
            normalized,
            {
                "symbol": normalized,
                "name": name or normalized,
                "asset_class": asset_class or infer_asset_class(normalized),
                "reasons": set(),
                "source_counts": defaultdict(int),
                "latest_source_timestamp": None,
                "latest_observed_at": None,
                "next_event_at": None,
                "evidence_score": 0.0,
                "liquidity_score": 0.0,
            },
        )
        if name and row["name"] == normalized:
            row["name"] = name
        if asset_class and not row.get("asset_class"):
            row["asset_class"] = asset_class
        row["reasons"].add(reason)
        row["source_counts"][source] += 1
        row["evidence_score"] += strength
        observed = parse_dt(observed_at)
        if observed and (row["latest_observed_at"] is None or observed > row["latest_observed_at"]):
            row["latest_observed_at"] = observed
            row["latest_source_timestamp"] = observed
        event = parse_dt(event_at)
        if event and event >= datetime.now(UTC) and (row["next_event_at"] is None or event < row["next_event_at"]):
            row["next_event_at"] = event

    for item in config_watchlist:
        touch(item.get("symbol"), "config_watchlist", "configured watchlist", None, item.get("name"), item.get("asset_class"), 0.5)

    for row in query_rows(con, "SELECT symbol, name, asset_class, source FROM instruments"):
        touch(row.get("symbol"), "instrument", "instrument master", None, row.get("name"), row.get("asset_class"), 0.5)

    for row in query_rows(con, "SELECT symbol, created_at, thesis_summary FROM birdclaw_theses WHERE symbol IS NOT NULL"):
        touch(row.get("symbol"), "arco_thesis", "Arco/Birdclaw thesis evidence", row.get("created_at"), None, None, 3.0)

    for row in query_rows(con, "SELECT symbol, filed_date, event_date, source_type, trader_name, raw FROM disclosures"):
        observed = row.get("filed_date") or row.get("event_date")
        label = "13F holding" if row.get("source_type") == "13f" else "trader disclosure"
        touch(row.get("symbol"), row.get("source_type") or "disclosure", label, observed, None, None, 2.0)
        raw = parse_json(row.get("raw"))
        for holding in raw.get("holdings", []) if isinstance(raw.get("holdings"), list) else []:
            touch(holding.get("symbol"), "13f_holding", f"13F holding from {row.get('trader_name') or 'tracker'}", observed, holding.get("name"), "equity", 1.5)

    for row in query_rows(con, "SELECT symbol, observed_at, name, metrics, source FROM market_screener_rows"):
        touch(row.get("symbol"), row.get("source") or "market_screener", "TradingView screener row", row.get("observed_at"), row.get("name"), None, 1.5)

    for row in query_rows(con, "SELECT published_at, provider, related_symbols, title FROM news_items"):
        for symbol in related_symbols(row.get("related_symbols")):
            touch(symbol, row.get("provider") or "news", "TradingView/news catalyst mention", row.get("published_at"), None, None, 1.5)

    for row in query_rows(con, "SELECT query, symbol, observed_at, description, exchange FROM tradingview_symbol_search"):
        touch(row.get("symbol"), "tradingview_search", f"TradingView search result for {row.get('query')}", row.get("observed_at"), row.get("description"), None, 0.5)

    for row in query_rows(con, "SELECT id, observed_at, name, symbols FROM tradingview_watchlists"):
        for symbol in related_symbols(row.get("symbols")):
            touch(symbol, "tradingview_watchlist", f"TradingView watchlist: {row.get('name') or row.get('id')}", row.get("observed_at"), None, None, 1.0)

    for row in query_rows(con, "SELECT symbol, observed_at, status FROM tradingview_alerts WHERE symbol IS NOT NULL"):
        touch(row.get("symbol"), "tradingview_alert", f"TradingView alert: {row.get('status') or 'loaded'}", row.get("observed_at"), None, None, 1.25)

    for row in query_rows(con, "SELECT symbol, observed_at, interval FROM tradingview_chart_state WHERE symbol IS NOT NULL"):
        touch(row.get("symbol"), "tradingview_chart_state", f"TradingView active chart {row.get('interval') or ''}".strip(), row.get("observed_at"), None, None, 0.75)

    for row in query_rows(con, "SELECT symbol, event_date, source FROM earnings_events"):
        touch(row.get("symbol"), row.get("source") or "earnings", "earnings calendar", None, None, None, 1.25, event_at=row.get("event_date"))

    for row in query_rows(con, "SELECT symbol, as_of, source FROM analyst_estimates"):
        touch(row.get("symbol"), "analyst_estimate", "analyst estimate snapshot", row.get("as_of"), None, None, 1.25)

    for row in query_rows(con, "SELECT symbol, as_of, event_date, verdict FROM earnings_setups"):
        touch(row.get("symbol"), "earnings_setup", f"earnings setup: {row.get('verdict') or 'scored'}", row.get("as_of"), None, None, 1.25, event_at=row.get("event_date"))

    for row in query_rows(con, "SELECT symbol, purchase_date FROM portfolio_positions"):
        touch(row.get("symbol"), "portfolio", "owned portfolio row", row.get("purchase_date"), None, None, 2.0)

    for row in query_rows(con, "SELECT symbol, run_date, evidence FROM candidates"):
        evidence = parse_json(row.get("evidence"))
        touch(row.get("symbol"), "candidate", "prior candidate screen", None, None, None, 0.25 + min(len(evidence) if isinstance(evidence, list) else 0, 2))

    for row in query_rows(con, "SELECT symbol, observed_at, source FROM quotes_intraday"):
        touch(row.get("symbol"), row.get("source") or "quote", "latest intraday quote", row.get("observed_at"), None, None, 0.75)

    for row in query_rows(con, "SELECT symbol, date FROM technical_features"):
        touch(row.get("symbol"), "technical", "technical feature row", row.get("date"), None, None, 1.0)
    for row in query_rows(con, "SELECT symbol, as_of FROM sepa_analyses"):
        touch(row.get("symbol"), "sepa", "SEPA setup analysis", row.get("as_of"), None, None, 1.0)
    for row in query_rows(con, "SELECT symbol, as_of, avg_dollar_volume FROM liquidity_metrics"):
        touch(row.get("symbol"), "liquidity", "liquidity analysis", row.get("as_of"), None, None, 1.0)
    for row in query_rows(con, "SELECT target_symbol AS symbol, as_of FROM correlation_runs"):
        touch(row.get("symbol"), "correlation", "correlation analysis", row.get("as_of"), None, None, 0.5)
    for row in query_rows(con, "SELECT symbol, as_of FROM valuation_models"):
        touch(row.get("symbol"), "valuation", "valuation model", row.get("as_of"), None, None, 0.75)
    for row in query_rows(con, "SELECT symbol, as_of, strategy_type FROM options_payoff_scenarios"):
        touch(row.get("symbol"), "options_payoff", f"options payoff: {row.get('strategy_type')}", row.get("as_of"), None, None, 0.75)

    liquidity = latest_by_symbol(query_rows(con, "SELECT symbol, as_of, grade, avg_dollar_volume FROM liquidity_metrics ORDER BY as_of DESC"), "symbol")
    now = datetime.now(UTC)
    output = []
    for symbol, row in items.items():
        counts = dict(row["source_counts"])
        latest = row["latest_observed_at"]
        next_event = row["next_event_at"]
        liq = liquidity.get(symbol, {})
        dollar_volume = float(liq.get("avg_dollar_volume") or 0)
        liquidity_score = min(100.0, dollar_volume / 1_000_000)
        total_source_count = sum(counts.values())
        source_count = sum(value for key, value in counts.items() if key not in STATIC_SOURCES)
        recency_score = recency_points(latest) if latest else 0.0
        tradable_asset = row.get("asset_class") in {"equity", "etf", "crypto"}
        eligibility_status = "eligible" if tradable_asset and source_count > 0 else "source_thin" if tradable_asset else "ineligible"
        evidence_score = float(row["evidence_score"]) + min(source_count, 10)
        discovery_score = evidence_score + liquidity_score * 0.2 + recency_score * 0.25
        output.append(
            {
                "symbol": symbol,
                "name": row["name"],
                "asset_class": row["asset_class"],
                "inclusion_reasons": sorted(row["reasons"]),
                "source_counts": counts,
                "source_count": source_count,
                "total_source_count": total_source_count,
                "latest_source_timestamp": latest,
                "latest_source_at": latest,
                "latest_observed_at": latest,
                "next_event_at": next_event,
                "eligibility_status": eligibility_status,
                "eligibility_detail": eligibility_detail(eligibility_status),
                "evidence_score": round(evidence_score, 2),
                "discovery_score": round(discovery_score, 2),
                "liquidity_score": round(liquidity_score, 2),
                "recency_score": round(recency_score, 2),
            }
        )
    output.sort(key=lambda item: (item["eligibility_status"] == "eligible", item["recency_score"], item["source_count"], item["evidence_score"], item["liquidity_score"]), reverse=True)
    eligible_rank = 0
    for index, row in enumerate(output, start=1):
        if row["eligibility_status"] == "eligible":
            eligible_rank += 1
            row["universe_rank"] = eligible_rank
            row["decision_universe_member"] = eligible_rank <= 250
        else:
            row["universe_rank"] = index
            row["decision_universe_member"] = False
        row["updated_at"] = now
    return output


def build_source_freshness(con: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(key: str, source_type: str, provider: str, observed_at: Any, status: str = "ok", detail: str = "", docs_only: bool = False) -> None:
        observed = parse_dt(observed_at)
        freshness = classify_freshness(source_type, observed, status, docs_only)
        rows.append(
            {
                "source_key": key,
                "source": key,
                "source_type": source_type,
                "source_kind": "documentation" if docs_only else source_type,
                "provider": provider,
                "last_observed_at": observed,
                "freshness_status": freshness,
                "provider_status": "failed" if str(status).lower() in {"error", "failed"} else str(status or "ok"),
                "stale_after": stale_after_label(source_type),
                "status": freshness,
                "detail": detail,
                "docs_only": docs_only,
                "checked_at": datetime.now(UTC),
            }
        )

    for row in query_rows(con, "SELECT symbol, observed_at, source FROM quotes_intraday QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY observed_at DESC) = 1"):
        add(f"{row.get('source') or 'quote'}:{row.get('symbol')}", "intraday_quote", row.get("source") or "quote", row.get("observed_at"))
    for row in query_rows(con, "SELECT symbol, date, source FROM prices_daily QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY date DESC) = 1"):
        add(f"previous_close:{row.get('symbol')}", "closing_quote", row.get("source") or "daily_price", row.get("date"))
    for row in query_rows(con, "SELECT symbol, expiry, observed_at, source FROM options_expiries"):
        add(f"{row.get('source') or 'options'}:options:{row.get('expiry')}:{row.get('symbol')}", "options", row.get("source") or "options", row.get("observed_at"))
    for row in query_rows(con, "SELECT symbol, expiry, as_of, source FROM options_payoff_scenarios"):
        add(f"{row.get('source') or 'options_payoff'}:options-payoff:{row.get('expiry')}:{row.get('symbol')}", "options", row.get("source") or "options_payoff", row.get("as_of"))
    for row in query_rows(con, "SELECT id, published_at, provider, source FROM news_items"):
        add(f"{row.get('source') or row.get('provider') or 'news'}:{row.get('id')}", "news", row.get("provider") or "news", row.get("published_at"))
    for row in query_rows(con, "SELECT id, observed_at, symbol, source FROM tradingview_symbol_search"):
        add(f"{row.get('source') or 'tradingview'}:search:{row.get('id')}:{row.get('symbol')}", "provider_run", row.get("source") or "tradingview", row.get("observed_at"))
    for row in query_rows(con, "SELECT id, observed_at, name, source FROM tradingview_watchlists"):
        add(f"{row.get('source') or 'tradingview'}:watchlist:{row.get('id')}", "provider_run", row.get("source") or "tradingview", row.get("observed_at"), detail=str(row.get("name") or ""))
    for row in query_rows(con, "SELECT id, observed_at, symbol, source FROM tradingview_alerts"):
        add(f"{row.get('source') or 'tradingview'}:alert:{row.get('id')}:{row.get('symbol')}", "provider_run", row.get("source") or "tradingview", row.get("observed_at"))
    for row in query_rows(con, "SELECT id, observed_at, symbol, source FROM tradingview_chart_state"):
        add(f"{row.get('source') or 'tradingview'}:chart-state:{row.get('id')}:{row.get('symbol')}", "provider_run", row.get("source") or "tradingview", row.get("observed_at"))
    for row in query_rows(con, "SELECT symbol, date, features FROM technical_features QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY date DESC) = 1"):
        add(f"technicals:{row.get('symbol')}", "daily", "technicals", row.get("date"))
    for table, provider in [("sepa_analyses", "sepa"), ("liquidity_metrics", "liquidity"), ("correlation_runs", "correlation"), ("valuation_models", "valuation"), ("earnings_setups", "earnings_setup"), ("analyst_estimates", "estimates")]:
        symbol_col = "target_symbol" if table == "correlation_runs" else "symbol"
        date_col = "as_of"
        for row in query_rows(con, f"SELECT {symbol_col} AS symbol, {date_col} AS as_of FROM {table}"):
            add(f"{provider}:{row.get('symbol')}", "daily" if provider in {"sepa", "liquidity", "correlation", "earnings_setup"} else "fundamental", provider, row.get("as_of"))
    for row in query_rows(con, "SELECT symbol, filing_date FROM equity_fundamentals"):
        add(f"fundamentals:{row.get('symbol')}", "fundamental", "sec_companyfacts", row.get("filing_date"))
    for row in query_rows(con, "SELECT symbol, filed_date, event_date, source_type FROM disclosures"):
        add(f"{row.get('source_type') or 'disclosure'}:{row.get('symbol')}", "filing", row.get("source_type") or "disclosure", row.get("filed_date") or row.get("event_date"))
    for row in query_rows(con, "SELECT symbol, created_at FROM birdclaw_theses"):
        add(f"arco_thesis:{row.get('symbol')}", "arco_thesis", "arco", row.get("created_at"))
    for row in query_rows(con, "SELECT source, checked_at, status, detail FROM source_health"):
        source = str(row.get("source") or "")
        status = str(row.get("status") or "").lower()
        docs_only = status in {"verified_docs", "documentation", "docs_only"} or source.endswith(".md") or source.startswith("docs/")
        source_type = "documentation" if docs_only else "provider_health"
        source_key = source if docs_only or source.endswith(":provider-run") else f"source_health:{source}"
        add(source_key, source_type, source.split(":")[0] or source, row.get("checked_at"), row.get("status") or "ok", row.get("detail") or "", docs_only)
    for row in query_rows(con, "SELECT provider, capability, finished_at, status, detail FROM provider_runs"):
        add(f"{row.get('provider')}:{row.get('capability')}:provider-run", "provider_run", row.get("provider") or "provider", row.get("finished_at"), row.get("status") or "ok", row.get("detail") or "")
    return dedupe_freshness(rows)


def build_decision_queue(con: Any, universe: list[dict[str, Any]], freshness_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    universe_by_symbol = {row["symbol"]: row for row in universe}
    freshness_by_symbol = symbol_freshness_detail(freshness_rows)
    candidates = latest_by_symbol(query_rows(con, "SELECT * FROM candidates ORDER BY run_date DESC, score DESC"), "symbol")
    quotes = latest_by_symbol(query_rows(con, "SELECT symbol, observed_at, price, change_pct FROM quotes_intraday ORDER BY observed_at DESC"), "symbol")
    liquidity = latest_by_symbol(query_rows(con, "SELECT symbol, as_of, grade, avg_dollar_volume FROM liquidity_metrics ORDER BY as_of DESC"), "symbol")
    catalysts = latest_by_symbol(query_rows(con, "SELECT symbol, event_date, event FROM catalysts WHERE symbol IS NOT NULL ORDER BY event_date ASC NULLS LAST"), "symbol")
    earnings = latest_by_symbol(query_rows(con, "SELECT symbol, event_date, event_type AS event FROM earnings_events ORDER BY event_date ASC NULLS LAST"), "symbol")
    portfolio = {row["symbol"]: row for row in query_rows(con, "SELECT symbol, quantity, avg_cost FROM portfolio_positions")}
    now = datetime.now(UTC)

    rows = []
    for symbol, uni in universe_by_symbol.items():
        if not uni.get("decision_universe_member"):
            continue
        candidate = candidates.get(symbol, {})
        quote = quotes.get(symbol, {})
        liq = liquidity.get(symbol, {})
        event = catalysts.get(symbol) or earnings.get(symbol) or {}
        raw_score = float(candidate.get("score") or 0)
        discovery_score = float(uni.get("discovery_score") or uni.get("evidence_score") or 0)
        decision_score = raw_score or min(70.0, discovery_score * 0.6)
        evidence = parse_json(candidate.get("evidence"))
        candidate_evidence_count = len(evidence) if isinstance(evidence, list) else 0
        source_counts = uni.get("source_counts") if isinstance(uni.get("source_counts"), dict) else {}
        raw_source_rows = int(uni.get("total_source_count") or sum(int(value or 0) for value in source_counts.values()))
        independent_source_count = sum(1 for key, value in source_counts.items() if key not in STATIC_SOURCES and int(value or 0) > 0)
        primary_evidence_count = sum(1 for key, value in source_counts.items() if key in PRIMARY_EVIDENCE_SOURCES and int(value or 0) > 0)
        evidence_items_count = candidate_evidence_count + primary_evidence_count
        evidence_count = evidence_items_count
        source_count = int(uni.get("source_count") or 0)
        freshness = freshness_by_symbol.get(symbol, default_freshness_detail())
        freshness_status = freshness["overall_decision_freshness"]
        blocking_gates = gate_reasons(candidate, freshness, evidence_count, independent_source_count, primary_evidence_count, liq)
        if not quote and uni.get("asset_class") in {"equity", "etf", "crypto"} and freshness.get("quote_freshness") in {"missing", "unknown"}:
            blocking_gates.append("missing_intraday_quote")
        if not liq and uni.get("asset_class") in {"equity", "etf"}:
            blocking_gates.append("liquidity_unknown")
        blocking_gates = sorted(set(blocking_gates))
        action_score = apply_blocking_penalties(decision_score, blocking_gates)
        action_grade = action_grade_for(action_score, freshness_status, evidence_count, independent_source_count, blocking_gates)
        basis = decision_basis(
            symbol,
            decision_score,
            action_score,
            discovery_score,
            uni,
            quote,
            liq,
            event,
            evidence_count,
            raw_source_rows,
            independent_source_count,
            evidence_items_count,
            primary_evidence_count,
            freshness,
        )
        rows.append(
            {
                "symbol": symbol,
                "as_of": now,
                "rank": 0,
                "action_grade": action_grade,
                "decision_bucket": action_grade,
                "score": round(action_score, 2),
                "discovery_score": round(discovery_score, 2),
                "decision_score": round(decision_score, 2),
                "action_score": round(action_score, 2),
                "freshness_status": freshness_status,
                "quote_freshness": freshness["quote_freshness"],
                "daily_analysis_freshness": freshness["daily_analysis_freshness"],
                "filing_freshness": freshness["filing_freshness"],
                "thesis_freshness": freshness["thesis_freshness"],
                "overall_decision_freshness": freshness["overall_decision_freshness"],
                "source_cluster": top_source_cluster(uni.get("source_counts") or {}),
                "evidence_count": evidence_count,
                "source_count": source_count,
                "raw_source_rows": raw_source_rows,
                "independent_source_count": independent_source_count,
                "evidence_items_count": evidence_items_count,
                "primary_evidence_count": primary_evidence_count,
                "inclusion_reasons": uni.get("inclusion_reasons") or [],
                "blocking_gates": blocking_gates,
                "decision_basis": basis,
                "latest_quote": quote.get("price"),
                "latest_quote_at": quote.get("observed_at"),
                "latest_observed_at": uni.get("latest_observed_at") or uni.get("latest_source_at"),
                "next_event_at": uni.get("next_event_at"),
                "catalyst_window": catalyst_window(event),
                "liquidity_grade": liq.get("grade"),
                "portfolio_impact": portfolio_impact(portfolio.get(symbol)),
                "owned": symbol in portfolio,
                "invalidation": invalidation_for(action_grade, blocking_gates),
                "name": uni.get("name"),
                "asset_class": uni.get("asset_class"),
            }
        )

    bucket_order = {"Act": 5, "Research": 4, "Watch": 3, "Reject": 2, "Stale": 1}
    rows.sort(key=lambda row: (bucket_order.get(row["action_grade"], 0), row["score"], row["evidence_count"], row["source_count"]), reverse=True)
    for index, row in enumerate(rows[:250], start=1):
        row["rank"] = index
    return rows[:250]


def build_symbol_decision_snapshots(queue: list[dict[str, Any]], universe: list[dict[str, Any]]) -> list[dict[str, Any]]:
    universe_by_symbol = {row["symbol"]: row for row in universe}
    snapshots = []
    for row in queue:
        uni = universe_by_symbol.get(row["symbol"], {})
        snapshot = {
            "symbol": row["symbol"],
            "score": row["score"],
            "discovery_score": row.get("discovery_score"),
            "decision_score": row.get("decision_score"),
            "action_score": row.get("action_score"),
            "rank": row["rank"],
            "latest_quote": row.get("latest_quote"),
            "latest_quote_at": row.get("latest_quote_at"),
            "latest_observed_at": row.get("latest_observed_at"),
            "next_event_at": row.get("next_event_at"),
            "catalyst_window": row.get("catalyst_window"),
            "liquidity_grade": row.get("liquidity_grade"),
            "portfolio_impact": row.get("portfolio_impact"),
            "invalidation": row.get("invalidation"),
            "raw_source_rows": row.get("raw_source_rows"),
            "independent_source_count": row.get("independent_source_count"),
            "evidence_items_count": row.get("evidence_items_count"),
            "primary_evidence_count": row.get("primary_evidence_count"),
            "universe": uni,
        }
        snapshots.append(
            {
                "symbol": row["symbol"],
                "as_of": row["as_of"],
                "action_grade": row["action_grade"],
                "freshness_status": row["freshness_status"],
                "quote_freshness": row.get("quote_freshness"),
                "daily_analysis_freshness": row.get("daily_analysis_freshness"),
                "filing_freshness": row.get("filing_freshness"),
                "thesis_freshness": row.get("thesis_freshness"),
                "source_cluster": row["source_cluster"],
                "inclusion_reasons": row["inclusion_reasons"],
                "blocking_gates": row["blocking_gates"],
                "decision_basis": row["decision_basis"],
                "invalidation": row["invalidation"],
                "snapshot": snapshot,
            }
        )
    return snapshots


def persist_discovered_universe(con: Any, rows: list[dict[str, Any]]) -> None:
    con.execute("DELETE FROM discovered_universe")
    for row in rows:
        con.execute(
            """
            INSERT OR REPLACE INTO discovered_universe
            (symbol, name, asset_class, inclusion_reasons, source_counts, latest_source_timestamp,
             latest_observed_at, next_event_at, eligibility_status, eligibility_detail, evidence_score,
             discovery_score, liquidity_score, recency_score,
             universe_rank, decision_universe_member, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row["symbol"], row.get("name"), row.get("asset_class"), json_dumps(row.get("inclusion_reasons") or []),
                json_dumps(row.get("source_counts") or {}), row.get("latest_source_timestamp"),
                row.get("latest_observed_at"), row.get("next_event_at"),
                row.get("eligibility_status"), row.get("eligibility_detail"), row.get("evidence_score"),
                row.get("discovery_score"), row.get("liquidity_score"), row.get("recency_score"), row.get("universe_rank"),
                row.get("decision_universe_member"), row.get("updated_at"),
            ],
        )


def persist_decision_queue(con: Any, rows: list[dict[str, Any]]) -> None:
    con.execute("DELETE FROM decision_queue")
    for row in rows:
        con.execute(
            """
            INSERT OR REPLACE INTO decision_queue
            (symbol, as_of, rank, action_grade, decision_bucket, score, discovery_score, decision_score,
             action_score, freshness_status, quote_freshness, daily_analysis_freshness, filing_freshness,
             thesis_freshness, overall_decision_freshness, source_cluster, evidence_count, raw_source_rows,
             independent_source_count, evidence_items_count, primary_evidence_count, inclusion_reasons,
             blocking_gates, decision_basis, latest_quote, latest_quote_at, latest_observed_at, next_event_at,
             catalyst_window, liquidity_grade, portfolio_impact, invalidation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row["symbol"], row.get("as_of"), row.get("rank"), row.get("action_grade"), row.get("decision_bucket"),
                row.get("score"), row.get("discovery_score"), row.get("decision_score"), row.get("action_score"),
                row.get("freshness_status"), row.get("quote_freshness"), row.get("daily_analysis_freshness"),
                row.get("filing_freshness"), row.get("thesis_freshness"), row.get("overall_decision_freshness"),
                row.get("source_cluster"), row.get("evidence_count"), row.get("raw_source_rows"),
                row.get("independent_source_count"), row.get("evidence_items_count"), row.get("primary_evidence_count"),
                json_dumps(row.get("inclusion_reasons") or []), json_dumps(row.get("blocking_gates") or []),
                json_dumps(row.get("decision_basis") or {}), row.get("latest_quote"), row.get("latest_quote_at"),
                row.get("latest_observed_at"), row.get("next_event_at"), row.get("catalyst_window"), row.get("liquidity_grade"), json_dumps(row.get("portfolio_impact") or {}),
                row.get("invalidation"),
            ],
        )


def persist_source_freshness(con: Any, rows: list[dict[str, Any]]) -> None:
    con.execute("DELETE FROM source_freshness")
    for row in rows:
        con.execute(
            """
            INSERT OR REPLACE INTO source_freshness
            (source_key, source_type, provider, last_observed_at, freshness_status, stale_after,
             status, detail, docs_only, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row["source_key"], row.get("source_type"), row.get("provider"), row.get("last_observed_at"),
                row.get("freshness_status"), row.get("stale_after"), row.get("provider_status") or row.get("status"),
                row.get("detail"), row.get("docs_only"), row.get("checked_at"),
            ],
        )


def persist_symbol_decision_snapshots(con: Any, rows: list[dict[str, Any]]) -> None:
    con.execute("DELETE FROM symbol_decision_snapshots")
    for row in rows:
        con.execute(
            """
            INSERT OR REPLACE INTO symbol_decision_snapshots
            (symbol, as_of, action_grade, freshness_status, quote_freshness, daily_analysis_freshness,
             filing_freshness, thesis_freshness, source_cluster, inclusion_reasons,
             blocking_gates, decision_basis, snapshot)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row["symbol"], row.get("as_of"), row.get("action_grade"), row.get("freshness_status"),
                row.get("quote_freshness"), row.get("daily_analysis_freshness"), row.get("filing_freshness"),
                row.get("thesis_freshness"), row.get("source_cluster"), json_dumps(row.get("inclusion_reasons") or []),
                json_dumps(row.get("blocking_gates") or []), json_dumps(row.get("decision_basis") or {}),
                json_dumps(row.get("snapshot") or {}),
            ],
        )


def classify_freshness(source_type: str, observed: datetime | None, status: str, docs_only: bool, now: datetime | None = None) -> str:
    normalized_status = str(status or "").lower()
    if docs_only or source_type == "documentation":
        return "documentation"
    if normalized_status in {"error", "failed", "missing_dependency"}:
        return "failed"
    if observed is None:
        return "unknown"
    checked_at = normalized_utc(now or datetime.now(UTC))
    age = checked_at - observed
    if source_type in {"intraday_quote", "options", "news"}:
        market_age = market_session_elapsed(observed, checked_at)
        return "fresh" if market_age <= timedelta(hours=INTRADAY_STALE_HOURS) else "stale"
    if source_type == "closing_quote":
        if is_market_open(checked_at):
            return "stale"
        return "fresh" if trading_day_lag(observed.date(), checked_at) <= DAILY_STALE_DAYS else "stale"
    if source_type in {"daily"}:
        return "fresh" if trading_day_lag(observed.date(), checked_at) <= DAILY_STALE_DAYS else "stale"
    if source_type == "arco_thesis":
        return "fresh" if age <= timedelta(days=ARCO_STALE_DAYS) else "stale"
    if source_type in {"filing", "fundamental"}:
        return "fresh" if age <= timedelta(days=FILING_STALE_DAYS) else "stale"
    if source_type in {"provider_run", "provider_health"}:
        return "fresh" if age <= timedelta(days=1) else "stale"
    return "fresh"


def normalized_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def market_session_elapsed(start: datetime, end: datetime) -> timedelta:
    """Elapsed regular US equity market time between two timestamps."""

    start_utc = normalized_utc(start)
    end_utc = normalized_utc(end)
    if end_utc <= start_utc:
        return timedelta()

    start_local = start_utc.astimezone(MARKET_TZ)
    end_local = end_utc.astimezone(MARKET_TZ)
    current = start_local.date()
    total = timedelta()
    while current <= end_local.date():
        if is_us_market_day(current):
            open_at, close_at = market_session_bounds(current)
            window_start = max(start_local, open_at)
            window_end = min(end_local, close_at)
            if window_end > window_start:
                total += window_end - window_start
        current += timedelta(days=1)
    return total


def trading_day_lag(observed_date: date, now: datetime) -> int:
    latest_expected = latest_completed_market_day(now)
    if observed_date >= latest_expected:
        return 0
    lag = 0
    current = observed_date + timedelta(days=1)
    while current <= latest_expected:
        if is_us_market_day(current):
            lag += 1
        current += timedelta(days=1)
    return lag


def latest_completed_market_day(now: datetime) -> date:
    local_now = normalized_utc(now).astimezone(MARKET_TZ)
    current = local_now.date()
    if is_us_market_day(current) and local_now.time() >= MARKET_CLOSE:
        return current
    current -= timedelta(days=1)
    while not is_us_market_day(current):
        current -= timedelta(days=1)
    return current


def is_market_open(now: datetime) -> bool:
    local_now = normalized_utc(now).astimezone(MARKET_TZ)
    return is_us_market_day(local_now.date()) and MARKET_OPEN <= local_now.time() < MARKET_CLOSE


def market_session_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, MARKET_OPEN, tzinfo=MARKET_TZ),
        datetime.combine(day, MARKET_CLOSE, tzinfo=MARKET_TZ),
    )


def is_us_market_day(day: date) -> bool:
    return day.weekday() < 5 and day not in us_market_holidays(day.year)


@lru_cache(maxsize=None)
def us_market_holidays(year: int) -> frozenset[date]:
    return frozenset(
        day
        for day in {
            observed_fixed_holiday(year, 1, 1),
            nth_weekday(year, 1, 0, 3),
            nth_weekday(year, 2, 0, 3),
            easter_date(year) - timedelta(days=2),
            last_weekday(year, 5, 0),
            observed_fixed_holiday(year, 6, 19),
            observed_fixed_holiday(year, 7, 4),
            nth_weekday(year, 9, 0, 1),
            nth_weekday(year, 11, 3, 4),
            observed_fixed_holiday(year, 12, 25),
        }
        if day.year == year
    )


def observed_fixed_holiday(year: int, month: int, day: int) -> date:
    holiday = date(year, month, day)
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> date:
    current = date(year, month, 1)
    offset = (weekday - current.weekday()) % 7
    return current + timedelta(days=offset + (ordinal - 1) * 7)


def last_weekday(year: int, month: int, weekday: int) -> date:
    current = date(year + int(month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def easter_date(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def watchlist_from_config(config: Any | None) -> list[dict[str, Any]]:
    if config is None:
        return []
    if isinstance(config, list):
        return config
    if isinstance(config, dict):
        return list(config.get("watchlist") or [])
    return list(getattr(config, "watchlist", []) or [])


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
        "options": "4 market hours",
        "news": "4 market hours",
        "daily": "1 trading day",
        "arco_thesis": "7 days",
        "filing": "filing cadence",
        "fundamental": "filing cadence",
        "documentation": "not applicable",
    }.get(source_type, "provider contract")


def gate_reasons(
    candidate: dict[str, Any],
    freshness: dict[str, str],
    evidence_count: int,
    independent_source_count: int,
    primary_evidence_count: int,
    liquidity: dict[str, Any],
) -> list[str]:
    gates = []
    breakdown = parse_json(candidate.get("score_breakdown"))
    for gate in breakdown.get("gates", []) if isinstance(breakdown.get("gates"), list) else []:
        gates.append(str(gate))
    quote_status = freshness.get("quote_freshness", "missing")
    daily_status = freshness.get("daily_analysis_freshness", "missing")
    if quote_status in {"missing", "unknown"}:
        gates.append("missing_intraday_quote")
    elif quote_status in {"stale", "failed"}:
        gates.append("stale_intraday_quote")
    if daily_status in {"missing", "unknown"}:
        gates.append("missing_daily_analysis")
    elif daily_status in {"stale", "failed"}:
        gates.append("stale_daily_analysis")
    if freshness.get("overall_decision_freshness") in {"stale", "failed", "missing"}:
        gates.append("stale_data")
    if evidence_count < 2 or independent_source_count < 2 or primary_evidence_count < 1:
        gates.append("evidence_thin")
    grade = str(liquidity.get("grade") or "").upper()
    dollar_volume = float(liquidity.get("avg_dollar_volume") or 0)
    if grade in {"F", "D"} or (dollar_volume and dollar_volume < 1_000_000):
        gates.append("liquidity_bad")
    return sorted(set(gates))


def action_grade_for(score: float, freshness: str, evidence_count: int, source_count: int, gates: list[str]) -> str:
    hard_freshness_gates = {
        "stale_data",
        "stale_intraday_quote",
        "missing_intraday_quote",
        "stale_daily_analysis",
        "missing_daily_analysis",
    }
    if freshness in {"stale", "failed", "degraded", "missing"} or hard_freshness_gates.intersection(gates):
        return "Stale"
    if "liquidity_bad" in gates:
        return "Reject"
    if "missing_intraday_quote" in gates or "liquidity_unknown" in gates:
        return "Watch" if score >= 60 else "Reject"
    if "evidence_thin" in gates:
        return "Watch" if score >= 60 else "Reject"
    if evidence_count < 2 or source_count < 2:
        return "Watch" if score >= 60 else "Reject"
    if score >= 90:
        return "Act"
    if score >= 75:
        return "Research"
    if score >= 55:
        return "Watch"
    return "Reject"


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
        if source_type in {"intraday_quote", "closing_quote"}:
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
        "options_payoff": 50,
        "tradingview_alert": 48,
        "tradingview_watchlist": 47,
        "tradingview_chart_state": 46,
        "tradingview": 45,
        "tradingview_search": 44,
        "yfinance": 40,
        "portfolio": 35,
    }
    eligible = [(key, int(value or 0)) for key, value in counts.items() if key not in STATIC_SOURCES and int(value or 0) > 0]
    if not eligible:
        return "-"
    return max(eligible, key=lambda item: (ranked_sources.get(item[0], 10), item[1], item[0]))[0]


def apply_blocking_penalties(score: float, gates: list[str]) -> float:
    penalties = {
        "stale_data": 25,
        "stale_intraday_quote": 18,
        "missing_intraday_quote": 18,
        "stale_daily_analysis": 15,
        "missing_daily_analysis": 15,
        "liquidity_unknown": 12,
        "liquidity_bad": 30,
        "evidence_thin": 10,
    }
    return max(0.0, score - sum(penalties.get(gate, 0) for gate in gates))


def decision_basis(
    symbol: str,
    decision_score: float,
    action_score: float,
    discovery_score: float,
    universe: dict[str, Any],
    quote: dict[str, Any],
    liquidity: dict[str, Any],
    event: dict[str, Any],
    evidence_count: int,
    raw_source_rows: int,
    independent_source_count: int,
    evidence_items_count: int,
    primary_evidence_count: int,
    freshness: dict[str, str],
) -> dict[str, Any]:
    return {
        "summary": f"{symbol} action score {round(action_score, 2)} from decision score {round(decision_score, 2)}.",
        "discovery_score": round(discovery_score, 2),
        "decision_score": round(decision_score, 2),
        "action_score": round(action_score, 2),
        "inclusion_reasons": universe.get("inclusion_reasons") or [],
        "source_counts": universe.get("source_counts") or {},
        "source_count": universe.get("source_count") or 0,
        "raw_source_rows": raw_source_rows,
        "independent_source_count": independent_source_count,
        "evidence_count": evidence_count,
        "evidence_items_count": evidence_items_count,
        "primary_evidence_count": primary_evidence_count,
        "eligibility_status": universe.get("eligibility_status"),
        "freshness": freshness,
        "latest_quote": quote.get("price"),
        "liquidity_grade": liquidity.get("grade"),
        "catalyst": event.get("event"),
    }


def invalidation_for(action_grade: str, gates: list[str]) -> str:
    if any("stale" in gate for gate in gates):
        return "Refresh source data before making an investment decision."
    if any("liquidity" in gate for gate in gates):
        return "Do not act unless liquidity improves enough to size safely."
    if action_grade in {"Act", "Research"}:
        return "Thesis weakens if source evidence is contradicted or trend/liquidity support breaks."
    return "Needs stronger evidence, catalyst confirmation, or improved liquidity before action."


def catalyst_window(row: dict[str, Any]) -> str:
    event_date = row.get("event_date")
    if not event_date:
        return "-"
    return f"{event_date}: {row.get('event') or 'event'}"


def portfolio_impact(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {"owned": False}
    return {"owned": True, "quantity": row.get("quantity"), "avg_cost": row.get("avg_cost")}


def readiness_blockers(row: dict[str, Any], source_counts: dict[str, Any], portfolio_count: int) -> list[str]:
    gates = [str(gate) for gate in row.get("blocking_gates") or []]
    blockers: list[str] = []
    if row.get("quote_freshness") in {"stale", "failed"} or "stale_intraday_quote" in gates:
        blockers.append("stale quote age")
    if row.get("quote_freshness") in {"missing", "unknown"} or "missing_intraday_quote" in gates:
        blockers.append("missing intraday quote")
    if row.get("daily_analysis_freshness") in {"stale", "failed"} or "stale_daily_analysis" in gates:
        blockers.append("stale daily analysis")
    if row.get("daily_analysis_freshness") in {"missing", "unknown"} or "missing_daily_analysis" in gates:
        blockers.append("missing daily analysis")
    if "liquidity_unknown" in gates:
        blockers.append("missing liquidity")
    if "liquidity_bad" in gates:
        blockers.append("liquidity below sizing threshold")
    if portfolio_count == 0:
        blockers.append("missing portfolio context")
    if not int(source_counts.get("valuation") or 0):
        blockers.append("missing valuation")
    if not int(source_counts.get("arco_thesis") or source_counts.get("thesis") or 0):
        blockers.append("missing thesis")
    if "evidence_thin" in gates:
        blockers.append("thin primary evidence")
    return sorted(set(blockers))


def readiness_missing_inputs(row: dict[str, Any], source_counts: dict[str, Any], portfolio_count: int) -> list[str]:
    missing: list[str] = []
    if row.get("quote_freshness") in {"missing", "unknown"}:
        missing.append("quote")
    if row.get("daily_analysis_freshness") in {"missing", "unknown"}:
        missing.append("daily_analysis")
    if "liquidity_unknown" in (row.get("blocking_gates") or []):
        missing.append("liquidity")
    if not int(source_counts.get("valuation") or 0):
        missing.append("valuation")
    if not int(source_counts.get("arco_thesis") or source_counts.get("thesis") or 0):
        missing.append("thesis")
    if portfolio_count == 0:
        missing.append("portfolio")
    return sorted(set(missing))


def readiness_status(row: dict[str, Any], blockers: list[str], missing_inputs: list[str]) -> str:
    refresh_terms = ("stale quote", "stale daily", "missing intraday quote", "missing daily")
    if any(any(term in blocker for term in refresh_terms) for blocker in blockers) or row.get("freshness_status") in {"stale", "failed"}:
        return "blocked_refresh"
    context_terms = {"portfolio", "liquidity", "valuation"}
    if context_terms.intersection(missing_inputs):
        return "blocked_missing_context"
    if {"thesis"}.intersection(missing_inputs) or "thin primary evidence" in blockers:
        return "needs_research"
    if row.get("action_grade") in {"Act", "Research"} and not blockers:
        return "ready"
    return "monitor"


def readiness_next_action(status: str, blockers: list[str], missing_inputs: list[str]) -> str:
    if status == "blocked_refresh":
        return "Run full_market_refresh or the specific stale source refresh before acting."
    if status == "blocked_missing_context":
        if "portfolio" in missing_inputs:
            return "Import or enter portfolio positions so sizing and duplicate-risk checks are available."
        if "liquidity" in missing_inputs:
            return "Refresh liquidity metrics before sizing a trade."
        return "Add the missing valuation/context row before making a buy decision."
    if status == "needs_research":
        return "Create or refresh the ticker thesis, valuation, catalyst, and primary-evidence packet."
    if status == "ready":
        return "Review ticker dossier and sizing constraints before placing any order."
    return "Monitor until a stronger catalyst, thesis, or source update appears."


def readiness_portfolio_fit(row: dict[str, Any], portfolio_count: int) -> dict[str, Any]:
    impact = row.get("portfolio_impact") if isinstance(row.get("portfolio_impact"), dict) else {}
    return {
        "has_portfolio_context": portfolio_count > 0,
        "current_exposure": impact if impact else {"owned": False},
        "overlap_correlation": "unknown",
        "concentration_impact": "unknown" if portfolio_count == 0 else "review_required",
        "existing_thesis_status": row.get("thesis_freshness") or "unknown",
        "duplicates_risk": bool(impact.get("owned")),
    }


def latest_by_symbol(rows: list[dict[str, Any]], symbol_key: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get(symbol_key) or "").upper()
        if symbol and symbol not in result:
            result[symbol] = row
    return result


def dedupe_freshness(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["source_key"]
        existing = deduped.get(key)
        if not existing or (parse_dt(row.get("last_observed_at")) or datetime.min.replace(tzinfo=UTC)) >= (parse_dt(existing.get("last_observed_at")) or datetime.min.replace(tzinfo=UTC)):
            deduped[key] = row
    return list(deduped.values())


def related_symbols(value: Any) -> list[str]:
    parsed = parse_json(value)
    if isinstance(parsed, list):
        return [str(item).split(":")[-1].upper() for item in parsed]
    if isinstance(value, str):
        return [item.strip().split(":")[-1].upper() for item in value.replace(";", ",").split(",") if item.strip()]
    return []


def parse_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC)
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def recency_points(observed: datetime) -> float:
    age_days = max(0.0, (datetime.now(UTC) - observed).total_seconds() / 86400)
    return max(0.0, 100.0 - age_days * 10)


def decode(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for field in ("inclusion_reasons", "source_counts", "blocking_gates", "decision_basis", "portfolio_impact", "snapshot"):
        if field in decoded:
            decoded[field] = parse_json(decoded[field])
    if "latest_source_timestamp" in decoded:
        decoded["latest_source_at"] = decoded["latest_source_timestamp"]
    if "source_counts" in decoded and "source_count" not in decoded:
        counts = decoded.get("source_counts") or {}
        decoded["source_count"] = sum(int(value or 0) for value in counts.values()) if isinstance(counts, dict) else 0
    if "source_key" in decoded:
        decoded["source"] = decoded["source_key"]
        decoded["source_kind"] = "documentation" if decoded.get("docs_only") else decoded.get("source_type")
        decoded["provider_status"] = decoded.get("status")
    snapshot = decoded.get("snapshot")
    if isinstance(snapshot, dict) and "invalidation" in snapshot:
        decoded["invalidation"] = snapshot.get("invalidation")
    return decoded
