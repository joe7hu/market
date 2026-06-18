"""Discovered-universe, queue, freshness, and snapshot builders."""

from __future__ import annotations
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from investment_panel.core.db import json_dumps, query_rows, upsert_instrument
from investment_panel.core.instruments import normalize_symbol
from investment_panel.core.source_status import normalize_source_status

from investment_panel.core.decision.constants import PRIMARY_EVIDENCE_SOURCES, STATIC_SOURCES
from investment_panel.core.decision.coerce import dedupe_freshness, latest_by_symbol, parse_dt, parse_json, related_symbols
from investment_panel.core.decision.calendar import classify_freshness
from investment_panel.core.decision.freshness import default_freshness_detail, stale_after_label, symbol_freshness_detail, top_source_cluster
from investment_panel.core.decision.watchlist import manual_watchlist_rows
from investment_panel.core.decision.grading import action_grade_for, apply_blocking_penalties, catalyst_window, decision_basis, gate_reasons, invalidation_for, portfolio_impact
from investment_panel.core.decision.portfolio import effective_portfolio_by_symbol
from investment_panel.core.decision.quotes import canonical_quote_rows
from investment_panel.core.decision.universe import DiscoveredUniverseAccumulator



def build_discovered_universe(con: Any, config_watchlist: list[dict[str, Any]]) -> list[dict[str, Any]]:
    universe = DiscoveredUniverseAccumulator()
    touch = universe.add

    for item in config_watchlist:
        touch(item.get("symbol"), "config_watchlist", "configured watchlist", None, item.get("name"), item.get("asset_class"), 0.5)

    for item in manual_watchlist_rows(con):
        touch(item.get("symbol"), "manual_watchlist", "manual watchlist", item.get("updated_at"), item.get("name"), item.get("asset_class"), 1.25)

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
    for row in query_rows(con, "SELECT symbol, observed_at, signal_type, source_id, thesis FROM ticker_source_signals WHERE symbol IS NOT NULL"):
        touch(
            row.get("symbol"),
            row.get("source_id") or "source_signal",
            f"canonical source signal: {row.get('signal_type') or 'evidence'}",
            row.get("observed_at"),
            None,
            None,
            1.75,
        )

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
    for row in query_rows(con, "SELECT symbol, as_of, premium_pct FROM etf_premiums"):
        touch(row.get("symbol"), "etf_premium", "ETF premium/discount snapshot", row.get("as_of"), None, "etf", 0.75)
    for row in query_rows(con, "SELECT symbol, date, source FROM crypto_fundamentals"):
        touch(row.get("symbol"), "crypto_fundamental", "CoinGecko crypto market/fundamental snapshot", row.get("date"), None, "crypto", 0.75)

    for row in query_rows(con, "SELECT symbol, purchase_date FROM portfolio_positions"):
        touch(row.get("symbol"), "portfolio", "owned portfolio row", row.get("purchase_date"), None, None, 2.0)

    for row in query_rows(con, "SELECT symbol, updated_at, asset_class, provider FROM broker_positions"):
        touch(
            row.get("symbol"),
            f"broker_position:{row.get('provider') or 'broker'}",
            "broker-sourced portfolio row",
            row.get("updated_at"),
            None,
            row.get("asset_class"),
            2.5,
        )

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
    return universe.rows(liquidity)




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
                "provider_status": normalize_source_status(status),
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
        symbol = str(row.get("symbol") or "").upper()
        source_type = "crypto_quote" if symbol.endswith("-USD") else "closing_quote"
        add(f"previous_close:{symbol}", source_type, row.get("source") or "daily_price", row.get("date"))
    for row in query_rows(con, "SELECT source, symbol, max(observed_at) AS observed_at, count(*) AS row_count FROM options_expiries GROUP BY source, symbol"):
        provider = row.get("source") or "options"
        add(f"{provider}:options:{row.get('symbol')}", "options", provider, row.get("observed_at"), detail=f"{row.get('row_count') or 0} expiries")
    for row in query_rows(con, "SELECT source, symbol, max(as_of) AS as_of, count(*) AS row_count FROM options_payoff_scenarios GROUP BY source, symbol"):
        provider = row.get("source") or "options_payoff"
        add(f"{provider}:options-payoff:{row.get('symbol')}", "options", provider, row.get("as_of"), detail=f"{row.get('row_count') or 0} payoff rows")
    for row in query_rows(con, "SELECT COALESCE(source, provider, 'news') AS source, provider, max(published_at) AS published_at, count(*) AS row_count FROM news_items GROUP BY COALESCE(source, provider, 'news'), provider"):
        provider = row.get("provider") or row.get("source") or "news"
        add(f"{row.get('source') or provider}:news", "news", provider, row.get("published_at"), detail=f"{row.get('row_count') or 0} news items")
    for table, capability, time_col in [
        ("tradingview_symbol_search", "search", "observed_at"),
        ("tradingview_watchlists", "watchlist", "observed_at"),
        ("tradingview_alerts", "alert", "observed_at"),
        ("tradingview_chart_state", "chart-state", "observed_at"),
    ]:
        for row in query_rows(con, f"SELECT COALESCE(source, 'tradingview') AS source, max({time_col}) AS observed_at, count(*) AS row_count FROM {table} GROUP BY COALESCE(source, 'tradingview')"):
            provider = row.get("source") or "tradingview"
            add(f"{provider}:{capability}:provider-run", "provider_run", provider, row.get("observed_at"), detail=f"{row.get('row_count') or 0} rows")
    for row in query_rows(con, "SELECT symbol, date, features FROM technical_features QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY date DESC) = 1"):
        add(f"technicals:{row.get('symbol')}", "daily", "technicals", row.get("date"))
    for table, provider in [("sepa_analyses", "sepa"), ("liquidity_metrics", "liquidity"), ("correlation_runs", "correlation"), ("valuation_models", "valuation"), ("earnings_setups", "earnings_setup"), ("analyst_estimates", "estimates")]:
        symbol_col = "target_symbol" if table == "correlation_runs" else "symbol"
        date_col = "as_of"
        for row in query_rows(con, f"SELECT {symbol_col} AS symbol, {date_col} AS as_of FROM {table}"):
            add(f"{provider}:{row.get('symbol')}", "daily" if provider in {"sepa", "liquidity", "correlation", "earnings_setup"} else "fundamental", provider, row.get("as_of"))
    for row in query_rows(con, "SELECT symbol, filing_date FROM equity_fundamentals"):
        add(f"fundamentals:{row.get('symbol')}", "fundamental", "sec_companyfacts", row.get("filing_date"))
    for row in query_rows(con, "SELECT symbol, as_of FROM etf_premiums"):
        add(f"etf_premium:{row.get('symbol')}", "fundamental", "yfinance_etf_premium", row.get("as_of"))
    for row in query_rows(con, "SELECT symbol, date FROM crypto_fundamentals"):
        add(f"crypto_fundamental:{row.get('symbol')}", "fundamental", "coingecko", row.get("date"))
    for row in query_rows(con, "SELECT symbol, filed_date, event_date, source_type FROM disclosures"):
        add(f"{row.get('source_type') or 'disclosure'}:{row.get('symbol')}", "filing", row.get("source_type") or "disclosure", row.get("filed_date") or row.get("event_date"))
    for row in query_rows(con, "SELECT symbol, created_at FROM birdclaw_theses"):
        add(f"arco_thesis:{row.get('symbol')}", "arco_thesis", "arco", row.get("created_at"))
    for row in query_rows(con, "SELECT source, checked_at, status, detail FROM source_health"):
        source = str(row.get("source") or "")
        status = normalize_source_status(row.get("status"))
        docs_only = status == "documentation" or source.startswith("docs:") or source.endswith(".md") or source.startswith("docs/")
        source_type = "documentation" if docs_only else "provider_health"
        source_key = source if docs_only or source.endswith(":provider-run") else f"source_health:{source}"
        add(source_key, source_type, source.split(":")[0] or source, row.get("checked_at"), row.get("status") or "ok", row.get("detail") or "", docs_only)
    for row in query_rows(con, "SELECT provider, capability, finished_at, status, detail FROM provider_runs"):
        add(f"{row.get('provider')}:{row.get('capability')}:provider-run", "provider_run", row.get("provider") or "provider", row.get("finished_at"), row.get("status") or "ok", row.get("detail") or "")
    for row in query_rows(con, "SELECT provider, checked_at, status, detail, last_data_at FROM broker_provider_status"):
        add(f"broker:{row.get('provider')}", "provider_health", row.get("provider") or "broker", row.get("last_data_at") or row.get("checked_at"), row.get("status") or "missing", row.get("detail") or "")
    for row in query_rows(con, "SELECT source_id, run_id, capability, finished_at, status, failure_detail FROM source_runs"):
        status = normalize_source_status(row.get("status") or "unknown")
        source_type = "documentation" if status == "documentation" else "provider_run"
        add(
            f"{row.get('source_id')}:{row.get('capability') or 'source'}:source-run",
            source_type,
            row.get("source_id") or "source",
            row.get("finished_at"),
            row.get("status") or "unknown",
            row.get("failure_detail") or "",
            docs_only=source_type == "documentation",
        )
    for row in query_rows(con, "SELECT source_id, source_kind, max(observed_at) AS observed_at, count(*) AS row_count FROM source_items WHERE source_kind IN ('news', 'arco_thesis') GROUP BY source_id, source_kind"):
        source_kind = str(row.get("source_kind") or "item")
        add(
            f"{row.get('source_id')}:{source_kind}:source-items",
            "news" if source_kind == "news" else "arco_thesis",
            row.get("source_id") or "source_item",
            row.get("observed_at"),
            detail=f"{row.get('row_count') or 0} source items",
        )
    return dedupe_freshness(rows)




def build_decision_queue(con: Any, universe: list[dict[str, Any]], freshness_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    universe_by_symbol = {row["symbol"]: row for row in universe}
    freshness_by_symbol = symbol_freshness_detail(freshness_rows)
    candidates = latest_by_symbol(query_rows(con, "SELECT * FROM candidates ORDER BY run_date DESC, score DESC"), "symbol")
    quotes = latest_by_symbol(canonical_quote_rows(con), "symbol")
    liquidity = latest_by_symbol(query_rows(con, "SELECT symbol, as_of, grade, avg_dollar_volume FROM liquidity_metrics ORDER BY as_of DESC"), "symbol")
    catalysts = latest_by_symbol(query_rows(con, "SELECT symbol, event_date, event FROM catalysts WHERE symbol IS NOT NULL ORDER BY event_date ASC NULLS LAST"), "symbol")
    earnings = latest_by_symbol(query_rows(con, "SELECT symbol, event_date, event_type AS event FROM earnings_events ORDER BY event_date ASC NULLS LAST"), "symbol")
    portfolio = effective_portfolio_by_symbol(con)
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
