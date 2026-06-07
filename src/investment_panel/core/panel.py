"""Read models for the FastAPI app."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from threading import Lock
from typing import Any

from investment_panel.core.config import AppConfig, config_to_dict, load_config
from investment_panel.core import brokers
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.daily_brief import daily_brief
from investment_panel.core.decision import canonical_quote_rows, decision_readiness_rows, effective_watchlist, manual_watchlist_rows, refresh_decision_read_models
from investment_panel.core.portfolio_intelligence import correlation_edges, exposure_clusters, portfolio_risk_cards, review_actions
from investment_panel.core.research import build_research_packet, generate_deterministic_memo
from investment_panel.core.signals import signal_rows
from investment_panel.core.sources import ensure_canonical_sources, source_item_rows, source_registry_rows, source_run_rows, source_ticker_ranking_rows, ticker_source_signal_rows
from investment_panel.core.thesis_monitor import thesis_monitor_rows


DECISION_REFRESH_LOCK = Lock()
DECISION_READ_MODEL_TABLES = {
    "decision_queue",
    "decision_readiness",
    "discovered_universe",
    "feed_signals",
    "source_freshness",
    "symbol_decision_snapshot",
    "symbol_decision_snapshots",
    "thesis_monitor",
    "universe_screen",
}


def load_panel_data(
    config: dict[str, Any] | AppConfig | None = None,
    table_names: list[str] | set[str] | tuple[str, ...] | None = None,
    ensure_decision_models: bool | None = None,
    ensure_source_models: bool | None = None,
) -> dict[str, Any]:
    app_config = config if isinstance(config, AppConfig) else load_config()
    if isinstance(config, dict):
        # FastAPI compatibility path: app.data_access passes a plain dict.
        db_path = Path(config.get("database", {}).get("duckdb_path", "data/investment.duckdb"))
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        config_watchlist = list(config.get("watchlist", []))
    else:
        db_path = app_config.database.duckdb_path
        config_watchlist = app_config.watchlist
    init_db(db_path)
    # Keep the API read connection in the same mode as init/write jobs. DuckDB
    # rejects simultaneous connections to one file when read_only differs.
    requested_tables = set(table_names or [])
    should_ensure_decision = (not requested_tables) if ensure_decision_models is None else ensure_decision_models
    should_ensure_sources = should_ensure_decision or ((not requested_tables) if ensure_source_models is None else ensure_source_models)
    with db(db_path, read_only=False) as con:
        if should_ensure_sources:
            ensure_canonical_sources(con)
        active_watchlist = effective_watchlist(con, config_watchlist)
        decision_refresh = (
            ensure_decision_read_models(con, active_watchlist)
            if should_ensure_decision
            else decision_readiness_snapshot(con, requested_tables)
        )
        decision_snapshots = symbol_decision_snapshots(con) if not requested_tables or requested_tables & DECISION_READ_MODEL_TABLES else []
        table_loaders = {
            "signals": lambda: signal_rows(con),
            "opportunities_ranked": lambda: opportunities_ranked(con),
            "opportunity_sources": lambda: opportunity_sources(con),
            "discovered_universe": lambda: discovered_universe(con),
            "decision_queue": lambda: decision_queue(con),
            "decision_readiness": lambda: decision_readiness(con),
            "source_freshness": lambda: source_freshness(con),
            "symbol_decision_snapshot": lambda: decision_snapshots,
            "symbol_decision_snapshots": lambda: decision_snapshots,
            "candidates": lambda: candidates(con),
            "portfolio": lambda: portfolio(con),
            "theses": lambda: theses(con),
            "manual_watchlist": lambda: manual_watchlist_rows(con),
            "thesis_monitor": lambda: thesis_monitor_rows(con, active_watchlist),
            "catalysts": lambda: catalysts(con),
            "fundamentals": lambda: fundamentals(con),
            "disclosures": lambda: disclosures(con),
            "quotes": lambda: quotes(con),
            "screener": lambda: screener(con),
            "options_expiries": lambda: options_expiries(con),
            "options_chain": lambda: options_chain(con),
            "options_payoff_scenarios": lambda: options_payoff_scenarios(con),
            "options_provider_capabilities": lambda: options_provider_capabilities(con),
            "options_expiry_signals": lambda: options_expiry_signals(con),
            "options_ticker_signals": lambda: options_ticker_signals(con),
            "option_strategy_versions": lambda: option_strategy_versions(con),
            "option_radar_summary": lambda: option_radar_summary(con),
            "option_radar_opportunity": lambda: option_radar_opportunity(con),
            "option_snapshot": lambda: option_snapshot(con),
            "option_features": lambda: option_features(con),
            "stock_features": lambda: stock_features(con),
            "agent_thesis": lambda: agent_thesis(con),
            "agent_thesis_request": lambda: agent_thesis_request(con),
            "agent_thesis_validation": lambda: agent_thesis_validation(con),
            "agent_postmortem_request": lambda: agent_postmortem_request(con),
            "agent_postmortem": lambda: agent_postmortem(con),
            "candidate_event": lambda: candidate_event(con),
            "candidate_event_mark": lambda: candidate_event_mark(con),
            "candidate_event_attribution": lambda: candidate_event_attribution(con),
            "shadow_trade": lambda: shadow_trade(con),
            "shadow_trade_mark": lambda: shadow_trade_mark(con),
            "radar_state_transition": lambda: radar_state_transition(con),
            "option_attribution": lambda: option_attribution(con),
            "missed_winner_event": lambda: missed_winner_event(con),
            "strategy_mutation_proposal": lambda: strategy_mutation_proposal(con),
            "strategy_backtest_result": lambda: strategy_backtest_result(con),
            "strategy_forward_test_result": lambda: strategy_forward_test_result(con),
            "strategy_cohort_result": lambda: strategy_cohort_result(con),
            "news": lambda: news(con),
            "tradingview_symbol_search": lambda: tradingview_symbol_search(con),
            "tradingview_watchlists": lambda: tradingview_watchlists(con),
            "tradingview_alerts": lambda: tradingview_alerts(con),
            "tradingview_chart_state": lambda: tradingview_chart_state(con),
            "sepa": lambda: sepa(con),
            "liquidity": lambda: liquidity(con),
            "correlations": lambda: correlations(con),
            "etf_premiums": lambda: etf_premiums(con),
            "analyst_estimates": lambda: analyst_estimates(con),
            "earnings": lambda: earnings(con),
            "earnings_setups": lambda: earnings_setups(con),
            "valuations": lambda: valuations(con),
            "technicals": lambda: technicals(con),
            "research_packets": lambda: research_packets(con),
            "provider_runs": lambda: provider_runs(con),
            "broker_status": lambda: brokers.broker_status_rows(con),
            "broker_accounts": lambda: brokers.broker_accounts(con),
            "broker_positions": lambda: brokers.broker_positions(con),
            "broker_market_snapshots": lambda: brokers.broker_market_snapshots(con),
            "broker_scanner_signals": lambda: brokers.broker_scanner_signals(con),
            "agent_recommendations": lambda: brokers.agent_recommendations(con),
            "paper_orders": lambda: brokers.paper_orders(con),
            "daily_brief": lambda: daily_brief(con),
            "feed_signals": lambda: feed_signals(con, active_watchlist),
            "universe_screen": lambda: universe_screen(con, active_watchlist),
            "source_consensus": lambda: source_consensus(con),
            "ownership_consensus": lambda: ownership_consensus(con),
            "market_context": lambda: market_context(con),
            "market_valuation_reference_charts": lambda: market_valuation_reference_charts(con),
            "market_valuation_charts": lambda: market_valuation_charts(con, active_watchlist),
            "market_environment_assets": lambda: market_environment_assets(con),
            "market_environment_model": lambda: market_environment_model(con, active_watchlist),
            "exposure_clusters": lambda: exposure_clusters(con),
            "correlation_edges": lambda: correlation_edges(con),
            "portfolio_risk_cards": lambda: portfolio_risk_cards(con),
            "review_actions": lambda: review_actions(con),
            "ticker_memos": lambda: reports(con),
            "trader_twins": lambda: trader_profiles(app_config.trader_profile_dir),
            "source_health": lambda: source_health(con),
            "sources": lambda: source_registry_rows(con),
            "source_runs": lambda: source_run_rows(con),
            "source_ticker_rankings": lambda: source_ticker_ranking_rows(con),
            "source_items": lambda: source_item_rows(con),
            "ticker_source_signals": lambda: ticker_source_signal_rows(con),
        }
        selected_tables = requested_tables or set(table_loaders)
        tables = {name: table_loaders[name]() for name in table_loaders if name in selected_tables}
    ready = any(tables.values()) if requested_tables else any(tables.get(name) for name in ("signals", "candidates", "portfolio", "ticker_memos"))
    return {
        "ready": ready,
        "message": "Loaded investment panel data." if ready else "Database is initialized but contains no screened candidates yet.",
        "source": "duckdb",
        "metadata": {"config": config_to_dict(app_config), "decision_refresh": decision_refresh},
        "tables": tables,
    }


def load_ticker_dossier_data(
    config: dict[str, Any] | AppConfig | None,
    ticker: str,
    ensure_decision_models: bool = True,
) -> dict[str, Any]:
    app_config = config if isinstance(config, AppConfig) else load_config()
    if isinstance(config, dict):
        db_path = Path(config.get("database", {}).get("duckdb_path", "data/investment.duckdb"))
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        config_watchlist = list(config.get("watchlist", []))
    else:
        db_path = app_config.database.duckdb_path
        config_watchlist = app_config.watchlist
    symbol = str(ticker or "").upper().strip()
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        if ensure_decision_models:
            ensure_canonical_sources(con)
        active_watchlist = effective_watchlist(con, config_watchlist)
        readiness = (
            ensure_decision_read_models(con, active_watchlist)
            if ensure_decision_models
            else decision_readiness_snapshot(con, DECISION_READ_MODEL_TABLES)
        )
        tables = {
            "candidates": _rows_matching_symbol(candidates(con), symbol),
            "decision_queue": _rows_matching_symbol(decision_queue(con), symbol),
            "discovered_universe": _rows_matching_symbol(discovered_universe(con), symbol),
            "universe_screen": _rows_matching_symbol(universe_screen(con, active_watchlist), symbol),
            "symbol_decision_snapshot": _rows_matching_symbol(symbol_decision_snapshots(con), symbol),
            "symbol_decision_snapshots": _rows_matching_symbol(symbol_decision_snapshots(con), symbol),
            "opportunities_ranked": _rows_matching_symbol(opportunities_ranked(con), symbol),
            "opportunity_sources": _rows_matching_symbol(opportunity_sources(con), symbol),
            "feed_signals": _rows_matching_symbol(feed_signals(con, active_watchlist), symbol),
            "source_consensus": _rows_matching_symbol(source_consensus(con), symbol),
            "ticker_source_signals": ticker_source_signal_rows(con, symbol=symbol),
            "ownership_consensus": _rows_matching_symbol(ownership_consensus(con), symbol),
            "portfolio": _rows_matching_symbol(portfolio(con), symbol),
            "theses": _rows_matching_symbol(theses(con), symbol),
            "thesis_monitor": _rows_matching_symbol(thesis_monitor_rows(con, active_watchlist), symbol),
            "catalysts": _rows_matching_symbol(catalysts(con), symbol),
            "signals": _rows_matching_symbol(signal_rows(con), symbol),
            "fundamentals": _rows_matching_symbol(fundamentals(con), symbol),
            "disclosures": _rows_matching_symbol(disclosures(con), symbol),
            "quotes": _rows_matching_symbol(quotes(con), symbol),
            "options_expiries": _rows_matching_symbol(options_expiries(con), symbol),
            "options_chain": _rows_matching_symbol(options_chain(con), symbol),
            "options_payoff_scenarios": _rows_matching_symbol(options_payoff_scenarios(con), symbol),
            "options_provider_capabilities": options_provider_capabilities(con),
            "options_expiry_signals": _rows_matching_symbol(options_expiry_signals(con), symbol),
            "options_ticker_signals": _rows_matching_symbol(options_ticker_signals(con), symbol),
            "news": _rows_matching_symbol(news(con), symbol),
            "tradingview_symbol_search": _rows_matching_symbol(tradingview_symbol_search(con), symbol),
            "tradingview_watchlists": _rows_matching_symbol(tradingview_watchlists(con), symbol),
            "tradingview_alerts": _rows_matching_symbol(tradingview_alerts(con), symbol),
            "tradingview_chart_state": _rows_matching_symbol(tradingview_chart_state(con), symbol),
            "sepa": _rows_matching_symbol(sepa(con), symbol),
            "liquidity": _rows_matching_symbol(liquidity(con), symbol),
            "correlations": _rows_matching_symbol(correlations(con), symbol),
            "etf_premiums": _rows_matching_symbol(etf_premiums(con), symbol),
            "analyst_estimates": _rows_matching_symbol(analyst_estimates(con), symbol),
            "earnings": _rows_matching_symbol(earnings(con), symbol),
            "earnings_setups": _rows_matching_symbol(earnings_setups(con), symbol),
            "valuations": _rows_matching_symbol(valuations(con), symbol),
            "technicals": technicals(con, symbols=[symbol]),
            "research_packets": _rows_matching_symbol(research_packets(con), symbol),
            "exposure_clusters": _rows_matching_symbol(exposure_clusters(con), symbol),
            "correlation_edges": _rows_matching_symbol(correlation_edges(con), symbol),
            "portfolio_risk_cards": _rows_matching_symbol(portfolio_risk_cards(con), symbol),
            "review_actions": _rows_matching_symbol(review_actions(con), symbol),
            "ticker_memos": _rows_matching_symbol(reports(con), symbol),
        }
    ready = any(rows for rows in tables.values())
    return {
        "ready": ready,
        "message": f"Loaded {symbol} ticker dossier." if ready else f"No ticker dossier rows loaded for {symbol}.",
        "source": "duckdb",
        "metadata": {"config": config_to_dict(app_config), "decision_refresh": readiness},
        "tables": tables,
    }


def _rows_matching_symbol(rows: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    normalized = _normalize_symbol_token(symbol)
    return [row for row in rows if _row_matches_symbol(row, normalized)]


def _row_matches_symbol(row: dict[str, Any], symbol: str) -> bool:
    if not symbol:
        return False
    direct_values = (
        row.get("symbol"),
        row.get("ticker"),
        row.get("primary_symbol"),
        row.get("peer_symbol"),
        row.get("target_symbol"),
    )
    if any(_normalize_symbol_token(value) == symbol for value in direct_values):
        return True
    for key in ("symbols", "related_symbols", "tickers", "bullish_symbols", "bearish_symbols"):
        if symbol in _symbols_from_value(row.get(key)):
            return True
    return False


def ensure_decision_read_models(con: Any, config_watchlist: list[dict[str, Any]]) -> dict[str, int | str]:
    counts = query_rows(
        con,
        """
        SELECT
            (SELECT count(*) FROM discovered_universe) AS discovered_universe,
            (SELECT count(*) FROM decision_queue) AS decision_queue,
            (SELECT count(*) FROM source_freshness) AS source_freshness,
            (SELECT count(*) FROM symbol_decision_snapshots) AS symbol_decision_snapshots
        """,
    )[0]
    if all(int(counts.get(key) or 0) > 0 for key in counts):
        return {**counts, "status": "cached"}
    with DECISION_REFRESH_LOCK:
        counts = query_rows(
            con,
            """
            SELECT
                (SELECT count(*) FROM discovered_universe) AS discovered_universe,
                (SELECT count(*) FROM decision_queue) AS decision_queue,
                (SELECT count(*) FROM source_freshness) AS source_freshness,
                (SELECT count(*) FROM symbol_decision_snapshots) AS symbol_decision_snapshots
            """,
        )[0]
        if all(int(counts.get(key) or 0) > 0 for key in counts):
            return {**counts, "status": "cached"}
        result = refresh_decision_read_models(con, config_watchlist)
        return {**result, "status": "refreshed"}


def decision_readiness_snapshot(con: Any, requested_tables: set[str]) -> dict[str, int | str | list[str]]:
    counts = query_rows(
        con,
        """
        SELECT
            (SELECT count(*) FROM discovered_universe) AS discovered_universe,
            (SELECT count(*) FROM decision_queue) AS decision_queue,
            (SELECT count(*) FROM source_freshness) AS source_freshness,
            (SELECT count(*) FROM symbol_decision_snapshots) AS symbol_decision_snapshots
        """,
    )[0]
    missing = [name for name, count in counts.items() if int(count or 0) == 0]
    status = "read_only_ready" if not missing else "read_only_missing"
    if not requested_tables & DECISION_READ_MODEL_TABLES:
        status = "read_only_not_required"
    return {**counts, "status": status, "missing": missing}


def get_panel_snapshot(config: dict[str, Any] | AppConfig | None = None) -> dict[str, Any]:
    return load_panel_data(config)


def feed_signals(con: Any, config_watchlist: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """MungerMode-style decision feed, enriched with Joe's portfolio context."""

    watchlist = {str(item.get("symbol") or "").upper() for item in (config_watchlist or []) if item.get("symbol")}
    portfolio_rows = {str(row.get("symbol") or "").upper(): row for row in portfolio(con)}
    decision_rows = decision_queue(con)
    thesis_rows = {str(row.get("symbol") or "").upper(): row for row in thesis_monitor_rows(con, config_watchlist or [])}
    decision_by_symbol = {str(row.get("symbol") or "").upper(): row for row in decision_rows}
    output = _group_feed_events(
        _source_feed_events(con, portfolio_rows, watchlist, decision_by_symbol, thesis_rows),
        portfolio_rows,
        watchlist,
        decision_by_symbol,
    )

    return sorted(output, key=lambda item: (item.get("date") or "", item.get("score") or 0), reverse=True)[:48]


def universe_screen(con: Any, config_watchlist: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Compact watched/candidate ticker screen with quality, value, and action columns."""

    configured_watch = {str(item.get("symbol") or "").upper() for item in (config_watchlist or []) if item.get("symbol")}
    excluded_watch = {
        str(item.get("symbol") or "").upper()
        for item in manual_watchlist_rows(con, include_excluded=True)
        if item.get("watch_state") == "excluded"
    }
    portfolio_symbols = {str(row.get("symbol") or "").upper() for row in portfolio(con)}
    quote_by_symbol = {str(row.get("symbol") or "").upper(): row for row in quotes(con)}
    decision_by_symbol = {str(row.get("symbol") or "").upper(): row for row in decision_queue(con)}
    screener_by_symbol = {str(row.get("symbol") or "").upper(): row for row in screener(con)}
    valuation_percentile_by_symbol = _valuation_percentiles_by_symbol(con, list(screener_by_symbol))
    fundamental_by_symbol: dict[str, dict[str, Any]] = {}
    for row in fundamentals(con):
        symbol = str(row.get("symbol") or "").upper()
        if symbol and symbol not in fundamental_by_symbol:
            fundamental_by_symbol[symbol] = row
    valuation_by_symbol: dict[str, dict[str, Any]] = {}
    for row in valuations(con):
        symbol = str(row.get("symbol") or "").upper()
        if symbol and symbol not in valuation_by_symbol:
            valuation_by_symbol[symbol] = row
    technical_rows = technicals(con)
    technical_by_symbol = {str(row.get("symbol") or "").upper(): row for row in technical_rows}
    rs_rank_1m_by_symbol = _rank_percentiles(technical_rows, "return_20d")
    rs_rank_3m_by_symbol = _rank_percentiles(technical_rows, "return_3m")

    rows = []
    for universe in discovered_universe(con):
        symbol = str(universe.get("symbol") or "").upper()
        if not symbol:
            continue
        decision = decision_by_symbol.get(symbol, {})
        screener_row = screener_by_symbol.get(symbol, {})
        metrics = _dict_from_value(screener_row.get("metrics"))
        fundamental_metrics = _dict_from_value(fundamental_by_symbol.get(symbol, {}).get("metrics"))
        valuation = valuation_by_symbol.get(symbol, {})
        technical = technical_by_symbol.get(symbol, {})
        watch_state = "owned" if symbol in portfolio_symbols else "candidate" if symbol in excluded_watch else "watched" if symbol in configured_watch or _is_watch_universe(universe) else "candidate"
        quality = _quality_score(decision, metrics, valuation)
        forward_pe = _metric_number(metrics, "forward_pe", "forwardPE", "forward_pe_ratio", "pe_forward")
        pe_ratio = _pe_from_fundamentals(metrics, fundamental_metrics)
        ps_ratio = _ps_from_fundamentals(metrics, fundamental_metrics)
        roic = _metric_number(metrics, "roic", "returnOnInvestedCapital", "return_on_invested_capital", "return_on_capital")
        if not roic:
            roic = _roic_from_fundamentals(fundamental_metrics, metrics)
        revenue = _metric_number(metrics, "total_revenue", "totalRevenue", "revenue") or _optional_number(fundamental_metrics.get("revenue"))
        revenue_growth = _metric_number_present(metrics, "revenue_growth", "revenueGrowth")
        if revenue_growth is None:
            revenue_growth = _optional_number(fundamental_metrics.get("revenue_growth"))
        free_cash_flow = _free_cash_flow(metrics, fundamental_metrics)
        fcf_margin = _ratio(free_cash_flow, revenue)
        market_cap = _metric_number(metrics, "market_cap", "marketCap", "market_cap_basic", "market_capitalization")
        fcf_yield = _ratio(free_cash_flow, market_cap)
        valuation_percentile = _optional_number(valuation.get("valuation_percentile_own_history"))
        if valuation_percentile is None:
            valuation_percentile = _optional_number(valuation.get("own_history_percentile"))
        if valuation_percentile is None:
            valuation_percentile = valuation_percentile_by_symbol.get(symbol)
        rows.append(
            {
                "symbol": symbol,
                "name": universe.get("name") or screener_row.get("name") or symbol,
                "watch_state": watch_state,
                "market_cap": market_cap,
                "ps_ratio": ps_ratio,
                "pe_ratio": pe_ratio,
                "forward_pe": forward_pe,
                "forward_pe_source": "provider" if forward_pe else "missing",
                "rev_yoy": revenue_growth,
                "revenue_yoy": revenue_growth,
                "free_cash_flow": free_cash_flow,
                "fcf_yield": fcf_yield,
                "fcf_margin": fcf_margin,
                "roic": roic,
                "roic_source": "provider" if _metric_number(metrics, "roic", "returnOnInvestedCapital", "return_on_invested_capital", "return_on_capital") else "fundamental_proxy" if roic else "missing",
                "rating": _star_rating(quality),
                "quality_score": quality,
                "value_signal": _value_signal(valuation, metrics),
                "valuation_percentile_own_history": valuation_percentile,
                "action": decision.get("action_grade") or "Watch",
                "next_action": _universe_next_action(decision, watch_state),
                "portfolio_relevance": _portfolio_relevance([symbol], {s: {} for s in portfolio_symbols}, configured_watch, decision),
                "freshness": decision.get("freshness_status") or quote_by_symbol.get(symbol, {}).get("freshness_status") or "unknown",
                "price": quote_by_symbol.get(symbol, {}).get("price"),
                "change_pct": quote_by_symbol.get(symbol, {}).get("change_pct"),
                "rs_rank_1m": rs_rank_1m_by_symbol.get(symbol),
                "rs_rank_3m": rs_rank_3m_by_symbol.get(symbol),
                "rs_3m": rs_rank_3m_by_symbol.get(symbol),
                "return_3m": technical.get("return_3m"),
                "relvol_1m": technical.get("rel_volume_1m") or technical.get("relvol_1m"),
                "atr_pct_1m": technical.get("atr_pct_1m"),
                "source_count": universe.get("source_count") or universe.get("total_source_count") or 0,
                "rank": universe.get("universe_rank"),
            }
        )

    sorted_rows = sorted(rows, key=lambda row: (_watch_sort(row), -(float(row.get("quality_score") or 0)), int(row.get("rank") or 9999)))[:500]
    return [_compact_empty_fields(row) for row in sorted_rows]


def source_consensus(con: Any) -> list[dict[str, Any]]:
    """Ninety-day-style source consensus across local/private and public source families."""

    decision_rows = decision_queue(con)
    family_counts = _source_family_counts(decision_rows)
    local_rows: list[dict[str, Any]] = []
    local_rows.extend(_source_count_rows(con, "Arco / Birdclaw", "private_graph", "birdclaw_theses", "symbol", "created_at"))
    local_rows.extend(_source_count_rows(con, "SEC disclosures", "filing", "disclosures", "symbol", "filed_date"))
    local_rows.extend(_source_count_rows(con, "Market research packets", "research", "research_reports", "symbol", "created_at"))
    local_rows.extend(_source_count_rows(con, "News providers", "news", "news_items", "related_symbols", "published_at"))
    local_rows.extend(_news_provider_source_rows(con))
    local_rows.extend(_thesis_author_source_rows(con))
    local_rows.extend(_disclosure_investor_source_rows(con))
    local_rows.extend(_research_report_source_rows(con))
    local_rows.extend(_provider_source_rows(con))

    output: list[dict[str, Any]] = []
    for row in local_rows:
        key = str(row["source_name"]).lower()
        family = _source_family_for_name(key)
        fallback_bullish, fallback_bearish = family_counts.get(family, ([], []))
        bullish = _symbols_from_value(row.get("bullish_symbols")) or fallback_bullish
        bearish = _symbols_from_value(row.get("bearish_symbols")) or fallback_bearish
        output.append(
            {
                **row,
                "is_followed": True,
                "origin": "market",
                "bullish_symbols": bullish[:8],
                "bearish_symbols": bearish[:8],
                "net_consensus": len(bullish) - len(bearish),
                "recommendation": "loaded",
            }
        )

    loaded_names = {str(row["source_name"]).lower() for row in output}
    for registry_row in source_registry_rows(con):
        source_name = str(registry_row.get("source_name") or registry_row.get("source_id") or "")
        if not source_name or source_name.lower() in loaded_names:
            continue
        if not (int(registry_row.get("items_count") or 0) or int(registry_row.get("tickers_count") or 0)):
            continue
        output.append(
            {
                "source_name": source_name,
                "content_type": registry_row.get("source_family") or registry_row.get("source_kind"),
                "items_count": registry_row.get("items_count") or 0,
                "tickers_count": registry_row.get("tickers_count") or 0,
                "latest_at": registry_row.get("latest_run_at"),
                "is_followed": bool(registry_row.get("enabled")),
                "origin": registry_row.get("origin") or "source_registry",
                "bullish_symbols": [],
                "bearish_symbols": [],
                "net_consensus": 0,
                "recommendation": "loaded",
                "freshness": registry_row.get("freshness"),
                "source_id": registry_row.get("source_id"),
            }
        )

    sorted_output = sorted(output, key=lambda row: (row.get("is_followed") is not True, -int(row.get("items_count") or 0), str(row.get("source_name"))))
    return [_compact_empty_fields(row) for row in sorted_output]


def ownership_consensus(con: Any) -> list[dict[str, Any]]:
    """Disclosure consensus by ticker and investor for the superinvestor surface."""

    rows = _expanded_disclosure_positions(disclosures(con))
    by_symbol: dict[str, dict[str, Any]] = {}
    investors: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        investor = str(row.get("trader_name") or row.get("filer_name") or "Tracked investor")
        if investor:
            investor_row = investors.setdefault(investor, {"investor": investor, "holdings": 0, "latest_filed": "", "symbols": set(), "net_buys": 0, "net_sells": 0, "total_value": 0.0})
            investor_row["latest_filed"] = max(str(investor_row["latest_filed"] or ""), str(row.get("filed_date") or ""))
        if not symbol:
            continue
        action = str(row.get("action") or "").lower()
        value = _disclosure_value(row)
        item = by_symbol.setdefault(symbol, {"symbol": symbol, "name": symbol, "holders": set(), "net_buys": 0, "net_sells": 0, "total_value": 0.0, "latest_filed": "", "investors": []})
        item["holders"].add(investor)
        item["total_value"] = float(item["total_value"]) + value
        item["latest_filed"] = max(str(item["latest_filed"] or ""), str(row.get("filed_date") or ""))
        if "sell" in action or "sale" in action or "reduc" in action:
            item["net_sells"] += 1
        elif "buy" in action or "purchase" in action or "add" in action:
            item["net_buys"] += 1
        if investor not in item["investors"]:
            item["investors"].append(investor)
        investors[investor]["symbols"].add(symbol)
        investors[investor]["holdings"] += 1
        investors[investor]["total_value"] = float(investors[investor].get("total_value") or 0) + value
        if "sell" in action or "sale" in action or "reduc" in action:
            investors[investor]["net_sells"] += 1
        elif "buy" in action or "purchase" in action or "add" in action:
            investors[investor]["net_buys"] += 1

    output = []
    for item in by_symbol.values():
        holders = sorted(item.pop("holders"))
        output.append(
            {
                **item,
                "holders": len(holders),
                "holder_names": holders[:8],
                "investors": item.get("investors", [])[:8],
                "net_activity": int(item.get("net_buys") or 0) - int(item.get("net_sells") or 0),
            }
        )

    investor_rows = [
        {
            "source_type": "investor",
            "investor": investor,
            "symbol": "",
            "holders": 0,
            "holder_names": [],
            "investors": [],
            "net_buys": row["net_buys"],
            "net_sells": row["net_sells"],
            "net_activity": int(row["net_buys"]) - int(row["net_sells"]),
            "total_value": row["total_value"],
            "latest_filed": row["latest_filed"],
            "holdings": row["holdings"],
            "symbols": sorted(row["symbols"])[:10],
        }
        for investor, row in investors.items()
    ]
    consensus_rows = sorted(output, key=lambda row: (int(row["holders"]), float(row["total_value"] or 0)), reverse=True)[:250]
    combined = consensus_rows + sorted(investor_rows, key=lambda row: int(row["holdings"]), reverse=True)[:100]
    return [_compact_empty_fields(row) for row in combined]


def _disclosure_value(row: dict[str, Any]) -> float:
    raw = _dict_from_value(row.get("raw"))
    for value in (
        row.get("total_value"),
        row.get("holdings_value_thousands"),
        row.get("estimated_invested_usd"),
        row.get("amount_mid"),
        raw.get("amount_mid"),
        raw.get("value_usd"),
        raw.get("estimated_invested_usd"),
        raw.get("holdings_value_thousands"),
        row.get("amount"),
        raw.get("amount_raw"),
    ):
        parsed = _number_from_any(value)
        if parsed:
            return parsed
    return 0.0


def _expanded_disclosure_positions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for row in rows:
        if row.get("source_type") != "13f":
            expanded.append(row)
            continue
        holding_sample = row.get("holding_sample") if isinstance(row.get("holding_sample"), list) else []
        allocation_by_key = {
            _allocation_key(item): item
            for item in row.get("allocation_history", [])
            if isinstance(item, dict)
        }
        for holding in holding_sample:
            if not isinstance(holding, dict):
                continue
            symbol = _normalize_symbol_token(holding.get("symbol"))
            if not symbol:
                continue
            allocation = allocation_by_key.get(_allocation_key(holding), {})
            action = str(allocation.get("type") or "HOLDINGS")
            value = float(holding.get("market_value") or holding.get("value_thousands") or 0.0)
            expanded.append(
                {
                    **row,
                    "symbol": symbol,
                    "action": action,
                    "amount": value,
                    "total_value": value,
                    "holding_weight": holding.get("weight"),
                    "holding_security": holding.get("name"),
                    "holding_put_call": holding.get("put_call"),
                    "raw": {
                        "holding": holding,
                        "allocation": allocation,
                        "source_type": "13f_holding",
                        "value_thousands": value,
                    },
                }
            )
    return expanded


def _source_feed_events(
    con: Any,
    portfolio_rows: dict[str, Any],
    watchlist: set[str],
    decision_by_symbol: dict[str, dict[str, Any]],
    thesis_rows: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    known_symbols = set(decision_by_symbol) | set(portfolio_rows) | watchlist
    for row in query_rows(
        con,
        """
        SELECT id, published_at, provider, title, related_symbols, link, source
        FROM news_items
        ORDER BY published_at DESC
        LIMIT 80
        """,
    ):
        title = str(row.get("title") or "Source update")
        symbols = _symbols_from_value(row.get("related_symbols")) or _symbols_from_text(title, known_symbols)
        if not symbols:
            continue
        primary = _primary_symbol(symbols, portfolio_rows, watchlist)
        sentiment = _source_sentiment(title, "")
        decision = decision_by_symbol.get(primary, {})
        events.append(
            {
                "id": f"news:{row.get('id')}",
                "date": _date_text(row.get("published_at")),
                "source": str(row.get("provider") or row.get("source") or "News"),
                "source_type": "news",
                "title": title,
                "symbols": symbols,
                "primary_symbol": primary,
                "thesis": _source_event_thesis(primary, sentiment, decision, title),
                "antithesis": _source_event_countercase(primary, sentiment, decision_by_symbol.get(primary, {})),
                "evidence": [title, str(row.get("link") or "")],
                "portfolio_relevance": _portfolio_relevance(symbols, portfolio_rows, watchlist, decision),
                "next_action": _source_event_next_action(symbols, portfolio_rows, watchlist),
                "freshness": "current",
                "severity": "good" if sentiment == "bullish" else "bad" if sentiment == "bearish" else "info",
                "score": 62 if sentiment != "neutral" else 48,
            }
        )

    for row in query_rows(
        con,
        """
        SELECT id, symbol, author, created_at, thesis_summary, claims, source_url
        FROM birdclaw_theses
        ORDER BY created_at DESC
        LIMIT 60
        """,
    ):
        symbol = _normalize_symbol_token(row.get("symbol"))
        if not symbol:
            continue
        claims = _dict_from_value(row.get("claims"))
        evidence = claims.get("evidence") if isinstance(claims.get("evidence"), list) else []
        first_evidence = next((item for item in evidence if isinstance(item, dict)), {})
        decision = decision_by_symbol.get(symbol, {})
        thesis_text = _plain_text(claims.get("text")) or str(row.get("thesis_summary") or "Arco/Birdclaw evidence raised this ticker.")
        title = str(row.get("thesis_summary") or f"{symbol} thesis evidence")
        events.append(
            {
                "id": f"thesis:{row.get('id')}",
                "feed_group_key": _feed_group_key_from_parts("socials", row.get("created_at") or first_evidence.get("date"), title, thesis_text or first_evidence.get("text")),
                "date": _date_text(row.get("created_at") or first_evidence.get("date")),
                "source": str(row.get("author") or "Arco / Birdclaw"),
                "source_type": "socials",
                "title": title,
                "symbols": [symbol],
                "primary_symbol": symbol,
                "thesis": thesis_text,
                "antithesis": _countercase(symbol, decision, thesis_rows.get(symbol, {}), {}),
                "evidence": [str(first_evidence.get("text") or row.get("source_url") or "")],
                "portfolio_relevance": _portfolio_relevance([symbol], portfolio_rows, watchlist, decision),
                "next_action": "Convert the source claim into a thesis/invalidation update if it affects Joe's watchlist.",
                "freshness": str(decision.get("freshness_status") or "current"),
                "severity": "watch",
                "score": 68,
            }
        )

    for row in _expanded_disclosure_positions(disclosures(con))[:120]:
        symbol = _normalize_symbol_token(row.get("symbol"))
        if not symbol:
            continue
        source_type = str(row.get("source_type") or "")
        if "13f" not in source_type and "disclosure" not in source_type:
            continue
        investor = str(row.get("trader_name") or row.get("filer_name") or "Tracked investor")
        action = str(row.get("action") or "disclosed")
        decision = decision_by_symbol.get(symbol, {})
        sentiment = _source_sentiment("", action)
        events.append(
            {
                "id": f"filing:{investor}:{symbol}:{row.get('filed_date')}:{action}",
                "feed_group_key": _feed_group_key_from_parts("filing", row.get("filed_date") or row.get("event_date"), investor, action, row.get("source_url") or row.get("id")),
                "date": _date_text(row.get("filed_date") or row.get("event_date")),
                "source": investor,
                "source_type": "filing",
                "title": f"{investor} {action.lower()} disclosed positions",
                "action": action,
                "symbols": [symbol],
                "primary_symbol": symbol,
                "thesis": f"{investor} disclosure adds ownership evidence for {symbol}.",
                "antithesis": "Disclosure data is delayed and may not represent the investor's current position or intent.",
                "evidence": [str(row.get("source_url") or ""), f"value {_disclosure_value(row):,.0f}" if _disclosure_value(row) else ""],
                "portfolio_relevance": _portfolio_relevance([symbol], portfolio_rows, watchlist, decision),
                "next_action": "Use as consensus evidence only after checking price, thesis, and filing lag.",
                "freshness": str(decision.get("freshness_status") or "current"),
                "severity": "good" if sentiment == "bullish" else "bad" if sentiment == "bearish" else "info",
                "score": 56,
            }
        )

    for row in query_rows(
        con,
        """
        SELECT id, symbol, created_at, report_type, report_json
        FROM research_reports
        ORDER BY created_at DESC
        LIMIT 40
        """,
    ):
        symbol = _normalize_symbol_token(row.get("symbol"))
        if not symbol or symbol == "PORTFOLIO":
            continue
        report = _dict_from_value(row.get("report_json"))
        decision = decision_by_symbol.get(symbol, {})
        events.append(
            {
                "id": f"research:{row.get('id')}",
                "date": _date_text(row.get("created_at")),
                "source": "Market research packet",
                "source_type": "research",
                "title": f"{symbol} {str(row.get('report_type') or 'research').replace('_', ' ')}",
                "symbols": [symbol],
                "primary_symbol": symbol,
                "thesis": _plain_text(report.get("why_now")) or "Deterministic research packet has new evidence for this ticker.",
                "antithesis": _plain_text(report.get("bear_case") or report.get("invalidation")) or _countercase(symbol, decision, thesis_rows.get(symbol, {}), {}),
                "evidence": _string_list(report.get("why_now"))[:2] + _string_list(report.get("bull_case"))[:2],
                "portfolio_relevance": _portfolio_relevance([symbol], portfolio_rows, watchlist, decision),
                "next_action": _plain_text(report.get("entry_plan")) or "Review the ticker dossier before changing exposure.",
                "freshness": str(decision.get("freshness_status") or "current"),
                "severity": "info",
                "score": 58,
            }
        )

    for row in ticker_source_signal_rows(con, limit=120):
        if str(row.get("source_item_id") or "").startswith(("news:", "arco_thesis:", "disclosure:")):
            continue
        if _is_generic_source_signal(row):
            continue
        symbol = _normalize_symbol_token(row.get("symbol"))
        if not symbol:
            continue
        decision = decision_by_symbol.get(symbol, {})
        title = str(row.get("title") or row.get("thesis") or "Source signal")
        evidence = _string_list(row.get("evidence_refs"))
        events.append(
            {
                "id": f"source_signal:{row.get('id')}",
                "feed_group_key": _feed_group_key_from_parts("source_signal", row.get("source_item_id") or row.get("id"), row.get("source_id")),
                "source_item_id": row.get("source_item_id"),
                "date": _date_text(row.get("observed_at")),
                "source": str(row.get("source_name") or row.get("source_id") or "Source signal"),
                "source_type": str(row.get("signal_type") or "source_signal"),
                "title": title,
                "symbols": [symbol],
                "primary_symbol": symbol,
                "thesis": str(row.get("thesis") or _source_event_thesis(symbol, str(row.get("sentiment") or "neutral"), decision, title)),
                "antithesis": str(row.get("antithesis") or _source_event_countercase(symbol, str(row.get("sentiment") or "neutral"), decision)),
                "evidence": evidence or [title],
                "portfolio_relevance": _portfolio_relevance([symbol], portfolio_rows, watchlist, decision),
                "next_action": "Refresh market context before action." if row.get("needs_market_context") else _source_event_next_action([symbol], portfolio_rows, watchlist),
                "freshness": "needs_market_context" if row.get("needs_market_context") else str(decision.get("freshness_status") or "current"),
                "severity": "good" if row.get("sentiment") == "bullish" else "bad" if row.get("sentiment") == "bearish" else "info",
                "score": 58 if not row.get("needs_market_context") else 42,
            }
        )
    return events


def _group_feed_events(
    events: list[dict[str, Any]],
    portfolio_rows: dict[str, Any],
    watchlist: set[str],
    decision_by_symbol: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for event in events:
        symbols = _symbols_from_value(event.get("symbols"))
        primary_symbol = _normalize_symbol_token(event.get("primary_symbol"))
        if primary_symbol and primary_symbol not in symbols:
            symbols.insert(0, primary_symbol)
        if not symbols:
            continue

        key = _feed_event_group_key(event)
        group = grouped.setdefault(
            key,
            {
                **event,
                "symbols": [],
                "ticker_contexts": [],
                "feed_item_count": 0,
                "source_count": 0,
                "_sources": [],
                "_evidence": [],
                "_context_keys": set(),
            },
        )
        group["feed_item_count"] = int(group.get("feed_item_count") or 0) + 1
        group["score"] = max(float(group.get("score") or 0), float(event.get("score") or 0))
        group["date"] = max(str(group.get("date") or ""), str(event.get("date") or ""))
        group["severity"] = _feed_more_severe(str(group.get("severity") or ""), str(event.get("severity") or ""))
        _append_unique(group["_sources"], str(event.get("source") or "Source"))
        for item in _string_list(event.get("evidence")):
            _append_unique(group["_evidence"], item)
        for symbol in symbols:
            _append_unique(group["symbols"], symbol)
            context = {
                "symbol": symbol,
                "source": event.get("source"),
                "thesis": event.get("thesis"),
                "antithesis": event.get("antithesis"),
                "portfolio_relevance": event.get("portfolio_relevance"),
                "next_action": event.get("next_action"),
                "freshness": event.get("freshness"),
                "severity": event.get("severity"),
            }
            context_key = json.dumps(context, sort_keys=True, default=str)
            if context_key not in group["_context_keys"]:
                group["_context_keys"].add(context_key)
                group["ticker_contexts"].append(_compact_empty_fields(context))

    output: list[dict[str, Any]] = []
    for key, group in grouped.items():
        symbols = list(group.get("symbols") or [])
        primary_symbol = _primary_symbol(symbols, portfolio_rows, watchlist)
        sources = list(group.get("_sources") or [])
        evidence = list(group.get("_evidence") or [])
        source_type = str(group.get("source_type") or "")
        if sources:
            group["source"] = sources[0] if len(sources) == 1 else f"{sources[0]} +{len(sources) - 1}"
        group["sources"] = sources
        group["source_count"] = len(sources)
        group["evidence"] = evidence[:6]
        group["symbols"] = symbols
        group["ticker_count"] = len(symbols)
        group["primary_symbol"] = primary_symbol
        if int(group.get("feed_item_count") or 0) > 1:
            group["id"] = f"feed_group:{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"
        if len(symbols) > 1:
            group["portfolio_relevance"] = _portfolio_relevance(symbols, portfolio_rows, watchlist, decision_by_symbol.get(primary_symbol, {}))
            group["next_action"] = _source_event_next_action(symbols, portfolio_rows, watchlist)
            if source_type == "filing":
                action = str(group.get("action") or "disclosed").lower()
                group["title"] = f"{group.get('source') or 'Tracked investor'} {action} {len(symbols)} positions"
                group["thesis"] = f"{group.get('source') or 'Tracked investor'} disclosure adds ownership evidence across {len(symbols)} tickers."
            elif source_type not in {"socials", "news"} and int(group.get("feed_item_count") or 0) > 1:
                group["thesis"] = f"This source item maps to {len(symbols)} tickers: {', '.join(symbols[:8])}{'...' if len(symbols) > 8 else ''}."
        group.pop("feed_group_key", None)
        group.pop("_sources", None)
        group.pop("_evidence", None)
        group.pop("_context_keys", None)
        output.append(_compact_empty_fields(group))
    return output


def _feed_event_group_key(event: dict[str, Any]) -> str:
    explicit = str(event.get("feed_group_key") or "")
    if explicit:
        return explicit
    source_item_id = str(event.get("source_item_id") or "")
    if source_item_id:
        return _feed_group_key_from_parts("source_item", source_item_id)
    return _feed_group_key_from_parts(
        event.get("source_type"),
        event.get("date"),
        event.get("title"),
        event.get("source"),
        event.get("thesis"),
    )


def _feed_group_key_from_parts(*parts: Any) -> str:
    normalized = [_plain_text(part).lower() for part in parts if _plain_text(part)]
    return "feed:" + "|".join(normalized)


def _feed_more_severe(left: str, right: str) -> str:
    ranks = {"bad": 4, "bearish": 4, "sell": 4, "warn": 3, "watch": 3, "good": 2, "bullish": 2, "info": 1, "neutral": 1}
    return left if ranks.get(left, 0) >= ranks.get(right, 0) else right


def _append_unique(values: list[Any], value: Any) -> None:
    if value not in values:
        values.append(value)


def market_context(con: Any) -> list[dict[str, Any]]:
    """Macro/market posture only when it affects sizing or portfolio risk."""

    rows: list[dict[str, Any]] = []
    for cluster in exposure_clusters(con)[:8]:
        rows.append(
            {
                "metric": f"{cluster.get('cluster_name') or 'Exposure'} concentration",
                "latest_value": cluster.get("portfolio_weight"),
                "unit": "%",
                "date": cluster.get("as_of"),
                "percentile": None,
                "posture": cluster.get("concentration_level") or "watch",
                "portfolio_effect": cluster.get("risk_readout") or cluster.get("next_step") or "Review sizing only if concentration changed.",
                "history": [],
            }
        )
    for card in portfolio_risk_cards(con)[:8]:
        rows.append(
            {
                "metric": card.get("title") or card.get("risk_type") or "Portfolio risk",
                "latest_value": card.get("score"),
                "unit": "score",
                "date": card.get("as_of"),
                "percentile": None,
                "posture": card.get("severity") or "watch",
                "portfolio_effect": card.get("impact") or card.get("next_step") or card.get("summary"),
                "history": [],
            }
        )
    if not rows:
        rows.append(
            {
                "metric": "Position sizing posture",
                "latest_value": None,
                "unit": "",
                "date": None,
                "percentile": None,
                "posture": "neutral",
                "portfolio_effect": "No macro or portfolio-risk row currently changes sizing.",
                "history": [],
            }
        )
    return [_compact_empty_fields(row) for row in rows[:12]]


def market_valuation_reference_charts(con: Any) -> list[dict[str, Any]]:
    """Broad-market valuation series with latest percentile context."""

    rows = query_rows(
        con,
        """
        SELECT metric, as_of, label, value, suffix, higher_is_better, source, source_url
        FROM market_valuation_metric_points
        WHERE metric IN ('sp500_forward_pe', 'shiller_pe', 'sp500_pe', 'equity_risk_premium', 'sp500_price')
        ORDER BY metric, as_of
        """,
    )
    by_metric: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_metric.setdefault(str(row.get("metric") or ""), []).append(row)
    price_by_month = _price_overlay_by_month(by_metric.get("sp500_price") or [])
    output = []
    for metric in ("sp500_forward_pe", "shiller_pe", "sp500_pe", "equity_risk_premium"):
        points = by_metric.get(metric) or []
        if not points:
            continue
        latest = points[-1]
        values = [_optional_number(point.get("value")) for point in points]
        latest_value = _optional_number(latest.get("value"))
        percentile = _percentile_rank(values, latest_value)
        higher_is_better = bool(latest.get("higher_is_better"))
        score = percentile if higher_is_better else (100 - percentile if percentile is not None else None)
        output.append(
            _compact_empty_fields(
                {
                    "metric": metric,
                    "label": latest.get("label") or metric,
                    "latest_value": latest_value,
                    "latest_date": latest.get("as_of"),
                    "percentile": percentile,
                    "score": score,
                    "suffix": latest.get("suffix"),
                    "higher_is_better": higher_is_better,
                    "posture": _environment_posture(score),
                    "source": latest.get("source"),
                    "source_url": latest.get("source_url"),
                    "history": _sample_market_metric_history(points, price_by_month),
                }
            )
        )
    return output


def market_environment_assets(con: Any) -> list[dict[str, Any]]:
    """Latest broad-market asset rows used by the environment model."""

    return query_rows(
        con,
        """
        SELECT symbol, as_of, group_name, name, price, return_1d, return_ytd, return_1w,
               return_1m, return_1y, pct_from_52w_high, sma_10_up, sma_20_up,
               sma_50_up, sma_200_up, sma_20_gt_50, sma_50_gt_200, range_ratio_52w,
               color,
               CASE
                 WHEN source = 'fullstack_market_model_sheet' THEN 'market_environment_asset_matrix'
                 ELSE source
               END AS source
        FROM market_environment_asset_snapshots
        WHERE as_of = (SELECT max(as_of) FROM market_environment_asset_snapshots)
        ORDER BY
          CASE group_name
            WHEN 'Market' THEN 0
            WHEN 'Sectors' THEN 1
            WHEN 'Industries' THEN 2
            WHEN 'Managed ETFs' THEN 3
            WHEN 'Countries' THEN 4
            WHEN 'Others' THEN 5
            WHEN 'Macro' THEN 6
            ELSE 7
          END,
          symbol
        """
    )


class MarketDisplayContext:
    """Cached market display rows for one panel load."""

    def __init__(self, con: Any, symbols: list[str]) -> None:
        self.con = con
        self.symbols = sorted({str(symbol or "").upper() for symbol in symbols if symbol})
        self._histories: dict[str, list[dict[str, Any]]] | None = None
        self._quotes: dict[str, dict[str, Any]] | None = None
        self._screener: dict[str, dict[str, Any]] | None = None
        self._technicals: dict[str, dict[str, Any]] | None = None
        self._valuations: dict[str, dict[str, Any]] | None = None

    @property
    def histories(self) -> dict[str, list[dict[str, Any]]]:
        if self._histories is None:
            self._histories = technical_price_history(self.con, self.symbols, days=253)
        return self._histories

    @property
    def quotes_by_symbol(self) -> dict[str, dict[str, Any]]:
        if self._quotes is None:
            self._quotes = {str(row.get("symbol") or "").upper(): row for row in quotes(self.con) if str(row.get("symbol") or "").upper() in self.symbols}
        return self._quotes

    @property
    def screener_by_symbol(self) -> dict[str, dict[str, Any]]:
        if self._screener is None:
            self._screener = {str(row.get("symbol") or "").upper(): row for row in screener(self.con) if str(row.get("symbol") or "").upper() in self.symbols}
        return self._screener

    @property
    def technicals_by_symbol(self) -> dict[str, dict[str, Any]]:
        if self._technicals is None:
            self._technicals = {
                str(row.get("symbol") or "").upper(): row
                for row in technicals(self.con, symbols=self.symbols, price_history=self.histories)
            }
        return self._technicals

    @property
    def valuations_by_symbol(self) -> dict[str, dict[str, Any]]:
        if self._valuations is None:
            self._valuations = _preferred_valuation_by_symbol([row for row in valuations(self.con) if str(row.get("symbol") or "").upper() in self.symbols])
        return self._valuations


def market_display_context(con: Any, config_watchlist: list[dict[str, Any]] | None = None) -> MarketDisplayContext:
    return MarketDisplayContext(con, _market_stance_symbols(con, config_watchlist))


def market_valuation_charts(
    con: Any,
    config_watchlist: list[dict[str, Any]] | None = None,
    context: MarketDisplayContext | None = None,
) -> list[dict[str, Any]]:
    """Watchlist and whole-market valuation chart rows for the Market page."""

    display_context = context or market_display_context(con, config_watchlist)
    symbols = display_context.symbols
    histories = display_context.histories
    quote_by_symbol = display_context.quotes_by_symbol
    screener_by_symbol = display_context.screener_by_symbol
    technical_by_symbol = display_context.technicals_by_symbol
    valuation_by_symbol = display_context.valuations_by_symbol
    rows: list[dict[str, Any]] = []

    for symbol in symbols:
        metrics = _dict_from_value(screener_by_symbol.get(symbol, {}).get("metrics"))
        valuation = valuation_by_symbol.get(symbol, {})
        quote = quote_by_symbol.get(symbol, {})
        history = histories.get(symbol, [])
        latest_price = _optional_number(quote.get("price")) or _last_history_close(history)
        fair_value = _optional_number(valuation.get("fair_value"))
        upside = _optional_number(valuation.get("upside_pct"))
        forward_pe = _metric_number(metrics, "forward_pe", "forwardPE", "forward_pe_ratio", "pe_forward", "trailingPE", "trailing_pe")
        ps_ratio = _ps_from_fundamentals(metrics, {})
        market_cap = _metric_number(metrics, "market_cap", "marketCap", "market_cap_basic", "market_capitalization")
        chart_points = _valuation_chart_points(history, latest_price, fair_value)
        rows.append(
            _compact_empty_fields(
                {
                    "symbol": symbol,
                    "name": screener_by_symbol.get(symbol, {}).get("name") or symbol,
                    "scope": _market_symbol_scope(symbol, quote, config_watchlist),
                    "latest_price": latest_price,
                    "change_pct": quote.get("change_pct"),
                    "fair_value": fair_value,
                    "upside_pct": upside,
                    "forward_pe": forward_pe,
                    "ps_ratio": ps_ratio,
                    "market_cap": market_cap,
                    "valuation_posture": _valuation_posture(upside, forward_pe, ps_ratio),
                    "valuation_score": _valuation_score(upside, forward_pe, ps_ratio),
                    "technical_score": technical_by_symbol.get(symbol, {}).get("technical_score"),
                    "return_60d": technical_by_symbol.get(symbol, {}).get("return_60d"),
                    "method": valuation.get("method"),
                    "confidence": _dict_from_value(valuation.get("diagnostics")).get("confidence"),
                    "source": valuation.get("method") or screener_by_symbol.get(symbol, {}).get("source") or quote.get("source"),
                    "history": chart_points,
                    "coverage": _valuation_coverage(latest_price, fair_value, forward_pe, ps_ratio, history),
                    "next_action": _valuation_next_action(symbol, upside, forward_pe, ps_ratio),
                }
            )
        )

    aggregate = _market_valuation_aggregate(rows)
    return [aggregate, *rows] if aggregate else rows


def market_environment_model(con: Any, config_watchlist: list[dict[str, Any]] | None = None, include_exposure: bool = True) -> list[dict[str, Any]]:
    """Deterministic market environment model for sizing posture."""

    valuation_reference_rows = market_valuation_reference_charts(con)
    asset_rows = market_environment_assets(con)
    needs_watchlist_fallback = not valuation_reference_rows or not asset_rows
    display_context = market_display_context(con, config_watchlist) if include_exposure or needs_watchlist_fallback else None
    valuation_rows = market_valuation_charts(con, config_watchlist, context=display_context) if display_context else []
    ticker_rows = [row for row in valuation_rows if row.get("scope") != "whole_market"]
    stance_symbols = display_context.symbols if display_context else []
    technical_rows = list(display_context.technicals_by_symbol.values()) if display_context else []
    liquidity_rows = [row for row in liquidity(con) if str(row.get("symbol") or "").upper() in stance_symbols] if include_exposure and stance_symbols else []
    risk_rows = portfolio_risk_cards(con) if include_exposure else []
    correlation_rows = correlation_edges(con) if include_exposure else []
    earnings_rows = [row for row in earnings_setups(con) if str(row.get("symbol") or "").upper() in stance_symbols] if include_exposure and stance_symbols else []
    technical_scores = [score for score in (_optional_number(row.get("technical_score")) for row in technical_rows) if score is not None]
    breadth_score = (_share([score >= 55 for score in technical_scores]) * 100) if technical_scores else None
    broad_valuation_score = _average([_optional_number(row.get("score")) for row in valuation_reference_rows])
    watchlist_valuation_score = _average([_optional_number(row.get("valuation_score")) for row in ticker_rows])
    valuation_score = broad_valuation_score if broad_valuation_score is not None else watchlist_valuation_score
    valuation_evidence = _market_valuation_reference_summary(valuation_reference_rows) or f"{_format_metric(_median([_optional_number(row.get('forward_pe')) for row in ticker_rows]), 'x')} median forward P/E; {_format_metric(_median([_optional_number(row.get('upside_pct')) for row in ticker_rows]), '%')} median fair-value gap."
    asset_trend_score = _asset_trend_score(asset_rows)
    market_trend_score = asset_trend_score if asset_trend_score is not None else _average([_optional_number(row.get("technical_score")) for row in technical_rows])
    asset_breadth_score = _asset_breadth_score(asset_rows)
    market_breadth_score = asset_breadth_score if asset_breadth_score is not None else breadth_score
    risk_appetite_score = _risk_appetite_score(asset_rows)
    leadership_score = _leadership_score(asset_rows)
    valuation_source = _market_valuation_reference_source(valuation_reference_rows) if valuation_reference_rows else "Watchlist valuation models"
    market_asset_source = "Market environment asset matrix" if asset_rows else "Not loaded"

    buckets = [
        _environment_bucket(
            "Valuation",
            valuation_score,
            valuation_evidence,
            "Lean into new risk only when discounts compensate for thesis risk.",
            "Use broad-market valuation percentiles before increasing beta exposure.",
            weight=0.25,
            source=valuation_source,
        ),
        _environment_bucket(
            "Price Trend",
            market_trend_score,
            _asset_trend_summary(asset_rows) or f"{_format_metric(_average([_optional_number(row.get('return_60d')) * 100 for row in technical_rows if _optional_number(row.get('return_60d')) is not None]), '%')} average 60-day return across covered watchlist names.",
            "Positive trend supports normal sizing; weak trend argues for staged entries.",
            "Check whether broad indices and sectors remain above key moving averages.",
            weight=0.20,
            source=market_asset_source if asset_rows else "Watchlist technicals",
        ),
        _environment_bucket(
            "Market Breadth",
            market_breadth_score,
            _asset_breadth_summary(asset_rows) or f"{_format_metric(breadth_score, '%')} of covered names have constructive technical scores.",
            "Narrow breadth raises single-name selection risk.",
            "Prefer source-backed names only when breadth is not deteriorating.",
            weight=0.20,
            source=market_asset_source if asset_rows else "Watchlist technicals",
        ),
        _environment_bucket(
            "Risk Appetite",
            risk_appetite_score,
            _risk_appetite_summary(asset_rows),
            "Volatility, dollar, bonds, and crypto risk appetite change timing and cash posture.",
            "Reduce chase risk when volatility or macro pressure rises.",
            weight=0.15,
            source=market_asset_source,
        ),
        _environment_bucket(
            "Sector / Theme Leadership",
            leadership_score,
            _leadership_summary(asset_rows),
            "Sector and theme leadership shows whether risk is broadening or crowded.",
            "Favor leaders with breadth confirmation; fade crowded laggards.",
            weight=0.10,
            source=market_asset_source,
        ),
    ]
    if include_exposure:
        buckets.extend(
            [
                _environment_bucket(
                    "Liquidity",
                    _liquidity_score(liquidity_rows),
                    f"{len(liquidity_rows)} liquidity rows loaded; {_format_metric(_median([_optional_number(row.get('avg_dollar_volume')) for row in liquidity_rows]), '$')} median dollar volume.",
                    "Thin liquidity should cap position size even when thesis quality is high.",
                    "Avoid chasing low-volume watchlist names without a limit plan.",
                    weight=0.05,
                    source="Watchlist liquidity metrics",
                ),
                _environment_bucket(
                    "Portfolio Risk",
                    _portfolio_environment_score(risk_rows, correlation_rows),
                    f"{len(risk_rows)} portfolio risk cards and {len(correlation_rows)} major correlation edges currently affect the model.",
                    "Risk model overrides market optimism when concentration or correlation is elevated.",
                    "Review the highest-severity risk card before adding exposure.",
                    weight=0.05,
                    source="Portfolio risk model",
                ),
                _environment_bucket(
                    "Earnings Setup",
                    _average([_optional_number(row.get("score")) for row in earnings_rows]),
                    f"{len(earnings_rows)} earnings setup rows loaded for watched names.",
                    "Event risk should change timing more than thesis conviction.",
                    "Stage entries around high-score setups only when valuation is not stretched.",
                    weight=0.05,
                    source="Watchlist earnings setup",
                ),
            ]
        )
    scored = [bucket for bucket in buckets if bucket.get("score") is not None]
    overall_score = _weighted_environment_score(scored)
    overall = _environment_bucket(
        "Overall",
        overall_score,
        _overall_environment_summary(scored),
        _overall_portfolio_effect(overall_score),
        _overall_next_action(overall_score),
        weight=1.0,
        source="Weighted environment model",
    )
    return [_compact_empty_fields(row) for row in [overall, *buckets]]


def _market_stance_symbols(con: Any, config_watchlist: list[dict[str, Any]] | None = None) -> list[str]:
    symbols: list[str] = []
    for item in config_watchlist or []:
        symbols.append(_normalize_symbol_token(item.get("symbol")))
    for row in manual_watchlist_rows(con, include_excluded=False):
        symbols.append(_normalize_symbol_token(row.get("symbol")))
    for row in portfolio(con):
        symbols.append(_normalize_symbol_token(row.get("symbol")))
    for row in tradingview_watchlists(con):
        symbols.extend(_symbols_from_value(row.get("symbols")))
    for row in discovered_universe(con):
        if _is_watch_universe(row):
            symbols.append(_normalize_symbol_token(row.get("symbol")))
    for benchmark in ("SPY", "QQQ", "IWM"):
        symbols.append(benchmark)
    return sorted({symbol for symbol in symbols if symbol})


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


def _median(values: list[float | None]) -> float | None:
    cleaned = sorted(value for value in values if value is not None and value == value)
    if not cleaned:
        return None
    mid = len(cleaned) // 2
    if len(cleaned) % 2:
        return round(cleaned[mid], 4)
    return round((cleaned[mid - 1] + cleaned[mid]) / 2, 4)


def _percentile_rank(values: list[float | None], current: float | None) -> float | None:
    cleaned = sorted(value for value in values if value is not None and value == value)
    if current is None or len(cleaned) < 2:
        return None
    below = sum(1 for value in cleaned if value < current)
    return round((below / (len(cleaned) - 1)) * 100, 2)


def _optional_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    number = _number_from_any(value)
    return number if number == number else None


def _average(values: list[float | None]) -> float | None:
    cleaned = [value for value in values if value is not None and value == value]
    return round(sum(cleaned) / len(cleaned), 4) if cleaned else None


def _share(values: list[bool]) -> float:
    return sum(1 for value in values if value) / len(values) if values else 0.0


def _format_metric(value: float | None, unit: str) -> str:
    if value is None:
        return "n/a"
    if unit == "$":
        return f"${value / 1_000_000:.1f}M" if abs(value) >= 1_000_000 else f"${value:,.0f}"
    if unit == "%":
        return f"{value:+.1f}%"
    if unit == "x":
        return f"{value:.1f}x"
    return f"{value:.1f}"


def _last_history_close(history: list[dict[str, Any]]) -> float:
    for point in reversed(history):
        close = _number_from_any(point.get("close"))
        if close:
            return close
    return 0.0


def _symbols_from_value(value: Any) -> list[str]:
    symbols = []
    for item in _string_list(value):
        symbol = _normalize_symbol_token(item)
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _symbols_from_text(value: Any, known_symbols: set[str]) -> list[str]:
    text = str(value or "").upper()
    symbols = []
    for symbol in sorted(known_symbols, key=len, reverse=True):
        if not symbol or len(symbol) < 2:
            continue
        if re.search(rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])", text):
            symbols.append(symbol)
    return symbols


def _is_generic_source_signal(row: dict[str, Any]) -> bool:
    signal_type = str(row.get("signal_type") or "")
    if signal_type in {"earnings_event", "analyst_estimate"}:
        return True
    title = str(row.get("title") or "").strip()
    thesis = str(row.get("thesis") or "").strip()
    antithesis = str(row.get("antithesis") or "").strip()
    return bool(thesis and title == thesis and antithesis.startswith("No structured"))


def _normalize_symbol_token(value: Any) -> str:
    text = str(value or "").strip().strip('"').strip("'").upper()
    if not text:
        return ""
    if ":" in text:
        text = text.split(":")[-1]
    if text.startswith("$") or text.startswith("#"):
        text = text[1:]
    normalized = "".join(char for char in text if char.isalnum() or char in {".", "-"})
    return normalized.strip(".-")


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        return [str(item).strip() for item in value.values() if str(item).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("[") or stripped.startswith("{") or stripped.startswith('"'):
            try:
                return _string_list(json.loads(stripped))
            except Exception:
                pass
        return [item.strip() for item in stripped.replace("|", ";").replace(",", ";").split(";") if item.strip()]
    return [str(value).strip()] if str(value).strip() else []


def _dict_from_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _date_text(value: Any) -> str:
    if not value:
        return ""
    return str(value)[:10]


def _source_label(value: Any, fallback: str) -> str:
    items = _string_list(value)
    if items:
        return " + ".join(items[:2])
    return fallback.replace("_", " ")


def _fallback_signal_title(symbol: str, category: str) -> str:
    label = category.replace("_", " ").title()
    return f"{symbol} {label}".strip()


def _countercase(symbol: str, decision: dict[str, Any], thesis: dict[str, Any], row: dict[str, Any]) -> str:
    for value in (
        thesis.get("invalidation"),
        decision.get("invalidation"),
        row.get("blocker"),
    ):
        text = _plain_text(value)
        if text and text.lower() != "none":
            return text
    freshness = str(decision.get("freshness_status") or "")
    if freshness and freshness.lower() not in {"fresh", "current"}:
        return f"The {symbol} signal weakens if source freshness remains {freshness} or primary evidence is not refreshed."
    return "The countercase is that this signal is already reflected in price or lacks enough independent source confirmation."


def _plain_text(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, dict):
        return "; ".join(str(item).strip() for item in value.values() if str(item).strip())
    return str(value or "").strip()


def _portfolio_relevance(symbols: list[str], portfolio_rows: dict[str, Any], watchlist: set[str], decision: dict[str, Any]) -> str:
    owned = [symbol for symbol in symbols if symbol in portfolio_rows]
    watched = [symbol for symbol in symbols if symbol in watchlist and symbol not in owned]
    impact = _dict_from_value(decision.get("portfolio_impact"))
    if owned:
        if impact:
            return str(impact.get("summary") or impact.get("impact") or f"Owned exposure: {', '.join(owned[:4])}")
        return f"Owned exposure: {', '.join(owned[:4])}"
    if watched:
        return f"Watchlist impact: {', '.join(watched[:4])}"
    if symbols:
        return f"Candidate impact: compare {', '.join(symbols[:4])} against Joe's owned and watched names."
    return "Portfolio impact not yet tied to a ticker."


def _decision_thesis(decision: dict[str, Any], basis: dict[str, Any]) -> str:
    reasons = _string_list(decision.get("inclusion_reasons"))
    if reasons:
        return "; ".join(reasons[:3])
    counts = basis.get("source_counts")
    if isinstance(counts, dict) and counts:
        leaders = ", ".join(f"{key}:{value}" for key, value in list(counts.items())[:4])
        return f"Decision score is supported by source families {leaders}."
    return f"Decision model ranks this at {decision.get('score') or 0} with {decision.get('evidence_count') or 0} evidence rows."


def _decision_evidence(decision: dict[str, Any], basis: dict[str, Any]) -> list[str]:
    evidence = []
    counts = basis.get("source_counts")
    if isinstance(counts, dict):
        evidence.extend(f"{key}: {value}" for key, value in counts.items() if value)
    if decision.get("evidence_count"):
        evidence.append(f"{decision.get('evidence_count')} evidence rows")
    if decision.get("source_cluster"):
        evidence.append(str(decision.get("source_cluster")))
    return evidence[:4]


def _severity_from_decision(decision: dict[str, Any]) -> str:
    grade = str(decision.get("action_grade") or "").lower()
    freshness = str(decision.get("freshness_status") or "").lower()
    if "reject" in grade or "stale" in freshness:
        return "bad"
    if "act" in grade:
        return "good"
    if "research" in grade:
        return "watch"
    return "info"


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


def _number_from_any(value: Any) -> float:
    if isinstance(value, (int, float)) and value == value:
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace("$", "").replace(",", "").replace("%", ""))
        except ValueError:
            return 0.0
    return 0.0


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


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or not denominator:
        return None
    return numerator / denominator


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


def _meaningful_text(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text in {"", "-", "none", "None", "null", "N/A", "n/a"} else text


def _watch_sort(row: dict[str, Any]) -> int:
    return {"owned": 0, "watched": 1, "candidate": 2}.get(str(row.get("watch_state")), 3)


def _source_family_counts(decision_rows: list[dict[str, Any]]) -> dict[str, tuple[list[str], list[str]]]:
    output: dict[str, tuple[list[str], list[str]]] = {}
    for row in decision_rows:
        symbol = str(row.get("symbol") or "").upper()
        grade = str(row.get("action_grade") or "").lower()
        basis = _dict_from_value(row.get("decision_basis"))
        counts = basis.get("source_counts") if isinstance(basis.get("source_counts"), dict) else _dict_from_value(row.get("source_counts"))
        for key, value in counts.items() if isinstance(counts, dict) else []:
            if not value:
                continue
            for family_key in {str(key), _source_family_for_name(str(key).lower())}:
                bullish, bearish = output.setdefault(family_key, ([], []))
                target = bearish if "reject" in grade or "stale" in grade else bullish
                if symbol and symbol not in target:
                    target.append(symbol)
    return output


def _source_count_rows(con: Any, source_name: str, content_type: str, table_name: str, symbol_column: str, time_column: str) -> list[dict[str, Any]]:
    try:
        result = query_rows(
            con,
            f"""
            SELECT count(*) AS items_count,
                   count(DISTINCT {symbol_column}) AS tickers_count,
                   max({time_column}) AS latest_at
            FROM {table_name}
            """,
        )[0]
    except Exception:
        return []
    count = int(result.get("items_count") or 0)
    if count <= 0:
        return []
    return [
        {
            "source_name": source_name,
            "content_type": content_type,
            "items_count": count,
            "tickers_count": int(result.get("tickers_count") or 0),
            "latest_at": result.get("latest_at"),
        }
    ]


def _news_provider_source_rows(con: Any) -> list[dict[str, Any]]:
    provider_rows: dict[str, dict[str, Any]] = {}
    for row in query_rows(
        con,
        """
        SELECT provider, title, related_symbols, published_at, link
        FROM news_items
        WHERE provider IS NOT NULL
        ORDER BY published_at DESC
        LIMIT 600
        """,
    ):
        provider = str(row.get("provider") or "News")
        entry = provider_rows.setdefault(
            provider,
            {
                "source_name": provider,
                "content_type": "news",
                "items_count": 0,
                "tickers": set(),
                "latest_at": row.get("published_at"),
                "bullish": {},
                "bearish": {},
                "history": [],
            },
        )
        entry["items_count"] += 1
        entry["latest_at"] = max(str(entry.get("latest_at") or ""), str(row.get("published_at") or ""))
        symbols = _symbols_from_value(row.get("related_symbols"))
        sentiment = _source_sentiment(str(row.get("title") or ""), "")
        for symbol in symbols:
            entry["tickers"].add(symbol)
            if sentiment == "bearish":
                entry["bearish"][symbol] = int(entry["bearish"].get(symbol, 0)) + 1
            elif sentiment == "bullish":
                entry["bullish"][symbol] = int(entry["bullish"].get(symbol, 0)) + 1
        if symbols:
            entry["history"].append({"date": row.get("published_at"), "symbols": symbols[:4], "title": row.get("title"), "url": row.get("link"), "sentiment": sentiment})
    return [_source_row_from_entry(entry, "news_provider") for entry in provider_rows.values()]


def _thesis_author_source_rows(con: Any) -> list[dict[str, Any]]:
    author_rows: dict[str, dict[str, Any]] = {}
    for row in query_rows(
        con,
        """
        SELECT author, symbol, created_at, thesis_summary, source_url
        FROM birdclaw_theses
        ORDER BY created_at DESC
        LIMIT 300
        """,
    ):
        author = str(row.get("author") or "Arco / Birdclaw")
        symbol = _normalize_symbol_token(row.get("symbol"))
        entry = author_rows.setdefault(
            author,
            {"source_name": author, "content_type": "private_graph", "items_count": 0, "tickers": set(), "latest_at": row.get("created_at"), "bullish": {}, "bearish": {}, "history": []},
        )
        entry["items_count"] += 1
        entry["latest_at"] = max(str(entry.get("latest_at") or ""), str(row.get("created_at") or ""))
        if symbol:
            entry["tickers"].add(symbol)
            entry["bullish"][symbol] = int(entry["bullish"].get(symbol, 0)) + 1
            entry["history"].append({"date": row.get("created_at"), "symbols": [symbol], "title": row.get("thesis_summary"), "url": row.get("source_url"), "sentiment": "bullish"})
    return [_source_row_from_entry(entry, "private_source") for entry in author_rows.values()]


def _disclosure_investor_source_rows(con: Any) -> list[dict[str, Any]]:
    investor_rows: dict[str, dict[str, Any]] = {}
    for row in _expanded_disclosure_positions(disclosures(con)):
        symbol = _normalize_symbol_token(row.get("symbol"))
        investor = str(row.get("trader_name") or row.get("filer_name") or "Tracked investor")
        if not symbol:
            continue
        entry = investor_rows.setdefault(
            investor,
            {"source_name": investor, "content_type": "filing", "items_count": 0, "tickers": set(), "latest_at": row.get("filed_date"), "bullish": {}, "bearish": {}, "history": []},
        )
        action = str(row.get("action") or "")
        sentiment = _source_sentiment("", action)
        entry["items_count"] += 1
        entry["latest_at"] = max(str(entry.get("latest_at") or ""), str(row.get("filed_date") or row.get("event_date") or ""))
        entry["tickers"].add(symbol)
        target = "bearish" if sentiment == "bearish" else "bullish"
        entry[target][symbol] = int(entry[target].get(symbol, 0)) + 1
        entry["history"].append({"date": row.get("filed_date") or row.get("event_date"), "symbols": [symbol], "title": f"{action} {symbol}", "url": row.get("source_url"), "sentiment": sentiment})
    return [_source_row_from_entry(entry, "disclosure_source") for entry in investor_rows.values()]


def _research_report_source_rows(con: Any) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in query_rows(
        con,
        """
        SELECT symbol, created_at, report_type, report_json
        FROM research_reports
        ORDER BY created_at DESC
        LIMIT 200
        """,
    ):
        source_name = f"Market {str(row.get('report_type') or 'research').replace('_', ' ')}"
        symbol = _normalize_symbol_token(row.get("symbol"))
        if not symbol or symbol == "PORTFOLIO":
            continue
        report = _dict_from_value(row.get("report_json"))
        sentiment = "bearish" if _plain_text(report.get("decision")).lower() in {"avoid", "sell", "reject"} else "bullish"
        entry = rows.setdefault(
            source_name,
            {"source_name": source_name, "content_type": "research", "items_count": 0, "tickers": set(), "latest_at": row.get("created_at"), "bullish": {}, "bearish": {}, "history": []},
        )
        entry["items_count"] += 1
        entry["latest_at"] = max(str(entry.get("latest_at") or ""), str(row.get("created_at") or ""))
        entry["tickers"].add(symbol)
        entry["bearish" if sentiment == "bearish" else "bullish"][symbol] = int(entry["bearish" if sentiment == "bearish" else "bullish"].get(symbol, 0)) + 1
        entry["history"].append({"date": row.get("created_at"), "symbols": [symbol], "title": report.get("decision") or row.get("report_type"), "sentiment": sentiment})
    return [_source_row_from_entry(entry, "research_source") for entry in rows.values()]


def _source_row_from_entry(entry: dict[str, Any], origin: str) -> dict[str, Any]:
    bullish = _ranked_symbol_counts(entry.get("bullish", {}))
    bearish = _ranked_symbol_counts(entry.get("bearish", {}))
    return {
        "source_name": entry["source_name"],
        "content_type": entry["content_type"],
        "items_count": int(entry.get("items_count") or 0),
        "tickers_count": len(entry.get("tickers") or []),
        "latest_at": entry.get("latest_at"),
        "bullish_symbols": [symbol for symbol, _ in bullish[:12]],
        "bearish_symbols": [symbol for symbol, _ in bearish[:12]],
        "ticker_history": entry.get("history", [])[:12],
        "source_origin": origin,
    }


def _ranked_symbol_counts(counts: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(((symbol, int(count or 0)) for symbol, count in counts.items()), key=lambda item: (item[1], item[0]), reverse=True)


def _source_sentiment(title: str, action: str) -> str:
    text = f"{title} {action}".lower()
    bearish_terms = ("sell", "sold", "sale", "reduce", "reduced", "cut", "downgrade", "miss", "falls", "fell", "drops", "plunge", "probe", "lawsuit", "delay", "bear")
    bullish_terms = ("buy", "bought", "purchase", "add", "added", "increase", "increased", "raise", "upgrade", "beat", "beats", "rally", "surge", "bull", "holdings", "add")
    if any(term in text for term in bearish_terms):
        return "bearish"
    if any(term in text for term in bullish_terms):
        return "bullish"
    return "neutral"


def _source_event_thesis(symbol: str, sentiment: str, decision: dict[str, Any], title: str) -> str:
    if sentiment == "bullish":
        return f"Source coverage is incrementally positive for {symbol}: {title}"
    if sentiment == "bearish":
        return f"Source coverage raises a downside or invalidation question for {symbol}: {title}"
    reasons = _string_list(decision.get("inclusion_reasons"))
    if reasons:
        return f"{symbol} has a new source item to compare against existing decision evidence: {reasons[0]}"
    return f"{symbol} has a new source item that may affect the watchlist or portfolio review queue."


def _source_event_countercase(symbol: str, sentiment: str, decision: dict[str, Any]) -> str:
    if sentiment == "bullish":
        return "A single positive source item is not enough if valuation, liquidity, or thesis evidence fails to confirm it."
    if sentiment == "bearish":
        return "The negative source item may be transient unless it changes fundamentals, disclosure consensus, or thesis invalidation."
    return _countercase(symbol, decision, {}, {})


def _source_event_next_action(symbols: list[str], portfolio_rows: dict[str, Any], watchlist: set[str]) -> str:
    if any(symbol in portfolio_rows for symbol in symbols):
        return "Check whether this changes sizing, thesis state, or the next portfolio review action."
    if any(symbol in watchlist for symbol in symbols):
        return "Keep on watchlist and require confirming evidence before promotion."
    return "Promote to watchlist only if another independent source confirms the signal."


def _primary_symbol(symbols: list[str], portfolio_rows: dict[str, Any], watchlist: set[str]) -> str:
    for symbol in symbols:
        if symbol in portfolio_rows:
            return symbol
    for symbol in symbols:
        if symbol in watchlist:
            return symbol
    return symbols[0] if symbols else ""


def _provider_source_rows(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT provider, capability, count(*) AS items_count, max(finished_at) AS latest_at
        FROM provider_runs
        GROUP BY provider, capability
        ORDER BY latest_at DESC NULLS LAST
        LIMIT 40
        """,
    )
    return [
        {
            "source_name": f"{row.get('provider')}: {row.get('capability')}",
            "content_type": "provider",
            "items_count": int(row.get("items_count") or 0),
            "tickers_count": 0,
            "latest_at": row.get("latest_at"),
        }
        for row in rows
    ]


def _source_family_for_name(name: str) -> str:
    if "arco" in name or "birdclaw" in name:
        return "thesis"
    if "sec" in name or "filing" in name or "disclosure" in name:
        return "filing"
    if "news" in name:
        return "news"
    if "research" in name:
        return "research"
    if "tradingview" in name:
        return "tradingview"
    if "yfinance" in name:
        return "quote"
    return name.split(":")[0]


def candidates(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT c.run_date, c.symbol, i.name, i.asset_class, i.category,
               c.score AS final_score, c.decision, c.score_breakdown, c.evidence
        FROM candidates c
        LEFT JOIN instruments i ON i.symbol = c.symbol
        QUALIFY row_number() OVER (PARTITION BY c.symbol ORDER BY c.run_date DESC, c.score DESC) = 1
        ORDER BY c.score DESC
        LIMIT 200
        """,
    )
    decoded = [decode_fields(row, ("score_breakdown", "evidence")) for row in rows]
    for row in decoded:
        row["components"] = row.get("score_breakdown") or {}
        evidence = row.get("evidence")
        if not evidence:
            evidence = candidate_source_evidence(con, str(row.get("symbol") or ""))
            row["evidence"] = evidence
        row["evidence_count"] = len(evidence) if isinstance(evidence, list) else 0
        row["freshness"] = row.get("run_date")
    return [_compact_empty_fields(row) for row in decoded]


def candidate_source_evidence(con: Any, symbol: str) -> list[dict[str, Any]]:
    if not symbol:
        return []
    return [
        {
            "type": row.get("signal_type") or "source_signal",
            "source_id": row.get("source_id"),
            "summary": row.get("thesis"),
            "observed_at": row.get("observed_at"),
            "evidence_refs": decode_json_value(row.get("evidence_refs")) or [f"source_item:{row.get('source_item_id')}"],
        }
        for row in query_rows(
            con,
            """
            SELECT source_item_id, source_id, observed_at, signal_type, thesis, evidence_refs
            FROM ticker_source_signals
            WHERE upper(symbol) = upper(?)
            ORDER BY observed_at DESC NULLS LAST
            LIMIT 6
            """,
            [symbol],
        )
    ]


def opportunities_ranked(con: Any) -> list[dict[str, Any]]:
    """Composite opportunity read model used by the workstation UI."""

    decision_rows = decision_queue(con)
    if decision_rows:
        for row in decision_rows:
            row["composite_score"] = row.get("score")
            row["confidence_score"] = confidence_to_number(
                str(row.get("freshness_status") or ""),
                float(row.get("score") or 0),
                int(row.get("evidence_count") or 0),
            )
            basis = row.get("decision_basis") if isinstance(row.get("decision_basis"), dict) else {}
            row["source_counts"] = basis.get("source_counts") or {}
            row["source_count"] = sum(int(value or 0) for value in row["source_counts"].values())
            row["latest_price"] = row.get("latest_quote")
            row["observed_at"] = row.get("latest_quote_at")
            row["top_source"] = row.get("source_cluster")
            row["decision"] = row.get("action_grade")
            row["gates"] = row.get("blocking_gates") or []
        return [_compact_empty_fields(row) for row in decision_rows]

    source_counts = opportunity_source_counts(con)
    latest_quotes = {
        row["symbol"]: row
        for row in query_rows(
            con,
            """
            SELECT symbol, observed_at, price, change_pct
            FROM quotes_intraday
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY observed_at DESC) = 1
            """,
        )
    }
    ranked = []
    for index, row in enumerate(signal_rows(con), start=1):
        symbol = str(row.get("symbol") or "").upper()
        counts = source_counts.get(symbol, {})
        quote = latest_quotes.get(symbol, {})
        source_count = sum(counts.values())
        components = row.get("components") if isinstance(row.get("components"), dict) else {}
        score = float(row.get("score") or 0)
        confidence = row.get("confidence")
        confidence_score = confidence_to_number(str(confidence or ""), score, source_count)
        ranked.append(
            {
                **row,
                "rank": index,
                "composite_score": score,
                "score": score,
                "confidence_score": confidence_score,
                "source_counts": counts,
                "source_count": source_count,
                "latest_price": quote.get("price"),
                "change_pct": quote.get("change_pct"),
                "observed_at": quote.get("observed_at"),
                "top_source": top_source_label(counts, components),
            }
        )
    sorted_ranked = sorted(ranked, key=lambda item: (item.get("score") or 0, item.get("source_count") or 0), reverse=True)
    return [_compact_empty_fields(row) for row in sorted_ranked]


def opportunity_source_counts(con: Any) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}

    def add(source: str, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            symbol = str(row.get("symbol") or row.get("target_symbol") or "").upper()
            if not symbol:
                continue
            counts.setdefault(symbol, {})[source] = int(row.get("count") or 0)

    add("technical", query_rows(con, "SELECT symbol, count(*) AS count FROM technical_features GROUP BY symbol"))
    add("sepa", query_rows(con, "SELECT symbol, count(*) AS count FROM sepa_analyses GROUP BY symbol"))
    add("liquidity", query_rows(con, "SELECT symbol, count(*) AS count FROM liquidity_metrics GROUP BY symbol"))
    add("valuation", query_rows(con, "SELECT symbol, count(*) AS count FROM valuation_models GROUP BY symbol"))
    add("earnings_setup", query_rows(con, "SELECT symbol, count(*) AS count FROM earnings_setups GROUP BY symbol"))
    add("options_payoff", query_rows(con, "SELECT symbol, count(*) AS count FROM options_payoff_scenarios GROUP BY symbol"))
    add("thesis", query_rows(con, "SELECT symbol, count(*) AS count FROM birdclaw_theses GROUP BY symbol"))
    add("filing", query_rows(con, "SELECT symbol, count(*) AS count FROM disclosures WHERE symbol IS NOT NULL GROUP BY symbol"))
    add("earnings", query_rows(con, "SELECT symbol, count(*) AS count FROM earnings_events GROUP BY symbol"))
    return counts


def discovered_universe(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, name, asset_class, inclusion_reasons, source_counts,
               latest_source_timestamp, latest_observed_at, next_event_at,
               eligibility_status, eligibility_detail, evidence_score, discovery_score,
               liquidity_score, recency_score, universe_rank,
               decision_universe_member, updated_at
        FROM discovered_universe
        ORDER BY decision_universe_member DESC, universe_rank ASC, symbol
        LIMIT 1000
        """,
    )
    decoded = [decode_fields(row, ("inclusion_reasons", "source_counts")) for row in rows]
    for row in decoded:
        row["latest_source_at"] = row.get("latest_source_timestamp")
        counts = row.get("source_counts") if isinstance(row.get("source_counts"), dict) else {}
        row["source_count"] = sum(int(value or 0) for key, value in counts.items() if key not in {"config_watchlist", "manual_watchlist", "config", "instrument", "instruments", "candidate"})
        row["total_source_count"] = sum(int(value or 0) for value in counts.values())
        row["next_event_at"] = row.get("next_event_at") or "No upcoming event loaded"
    return [_compact_empty_fields(row) for row in decoded]


def decision_queue(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, rank, action_grade, decision_bucket, score,
               discovery_score, decision_score, action_score,
               freshness_status, quote_freshness, daily_analysis_freshness,
               filing_freshness, thesis_freshness, overall_decision_freshness,
               source_cluster, evidence_count, raw_source_rows, independent_source_count,
               evidence_items_count, primary_evidence_count,
               inclusion_reasons, blocking_gates, decision_basis,
               latest_quote, latest_quote_at, latest_observed_at, next_event_at,
               catalyst_window, liquidity_grade,
               portfolio_impact, invalidation
        FROM decision_queue
        ORDER BY rank ASC, score DESC
        LIMIT 250
        """,
    )
    decoded = [decode_fields(row, ("inclusion_reasons", "blocking_gates", "decision_basis", "portfolio_impact")) for row in rows]
    for row in decoded:
        row["next_event_at"] = row.get("next_event_at") or "No upcoming event loaded"
    return [_compact_empty_fields(row) for row in decoded]


def decision_readiness(con: Any) -> list[dict[str, Any]]:
    return [_compact_empty_fields(row) for row in decision_readiness_rows(con)]


def source_freshness(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT source_key, source_type, provider, last_observed_at, freshness_status,
               stale_after, status, detail, docs_only, checked_at
        FROM source_freshness
        ORDER BY docs_only ASC, freshness_status DESC, source_key
        """,
    )
    for row in rows:
        row["source"] = row.get("source_key")
        row["source_kind"] = "documentation" if row.get("docs_only") else row.get("source_type")
        row["provider_status"] = row.get("status")
    return [_compact_empty_fields(row) for row in rows]


def symbol_decision_snapshots(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, action_grade, freshness_status, quote_freshness,
               daily_analysis_freshness, filing_freshness, thesis_freshness, source_cluster,
               inclusion_reasons, blocking_gates, decision_basis, snapshot
        FROM symbol_decision_snapshots
        ORDER BY as_of DESC, symbol
        LIMIT 250
        """,
    )
    decoded = [decode_fields(row, ("inclusion_reasons", "blocking_gates", "decision_basis", "snapshot")) for row in rows]
    for row in decoded:
        snapshot = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
        row["invalidation"] = snapshot.get("invalidation")
    return [_compact_empty_fields(row) for row in decoded]


def opportunity_sources(con: Any) -> list[dict[str, Any]]:
    """One row per symbol/source leader for the Opportunities source panels."""

    panels: list[dict[str, Any]] = []
    panels.extend(
        source_rows(
            "technical",
            "Technical Setups",
            query_rows(
                con,
                """
                SELECT symbol, as_of AS source_date, score, verdict AS label, stage AS caption
                FROM sepa_analyses
                ORDER BY score DESC NULLS LAST
                LIMIT 50
                """,
            ),
        )
    )
    panels.extend(
        source_rows(
            "liquidity",
            "Liquidity",
            query_rows(
                con,
                """
                SELECT symbol, as_of AS source_date, avg_dollar_volume AS score,
                       grade AS label, 'average dollar volume' AS caption
                FROM liquidity_metrics
                ORDER BY avg_dollar_volume DESC NULLS LAST
                LIMIT 50
                """,
            ),
        )
    )
    panels.extend(
        source_rows(
            "valuation",
            "Valuation",
            query_rows(
                con,
                """
                SELECT symbol, as_of AS source_date, upside_pct AS score,
                       method AS label, 'modeled upside' AS caption
                FROM valuation_models
                ORDER BY upside_pct DESC NULLS LAST
                LIMIT 50
                """,
            ),
        )
    )
    panels.extend(
        source_rows(
            "earnings_setup",
            "Earnings Setups",
            query_rows(
                con,
                """
                SELECT symbol, as_of AS source_date, score,
                       verdict AS label, 'revision/surprise setup' AS caption
                FROM earnings_setups
                ORDER BY score DESC NULLS LAST
                LIMIT 50
                """,
            ),
        )
    )
    panels.extend(
        source_rows(
            "options_payoff",
            "Options Payoff",
            query_rows(
                con,
                """
                SELECT symbol, as_of AS source_date, COALESCE(max_profit, 0) AS score,
                       strategy_type AS label, 'deterministic payoff scenario' AS caption
                FROM options_payoff_scenarios
                ORDER BY as_of DESC, symbol
                LIMIT 50
                """,
            ),
        )
    )
    panels.extend(
        source_rows(
            "thesis",
            "Thesis / Memos",
            query_rows(
                con,
                """
                SELECT symbol, created_at AS source_date, 1 AS score,
                       author AS label, thesis_summary AS caption
                FROM birdclaw_theses
                ORDER BY created_at DESC
                LIMIT 50
                """,
            ),
        )
    )
    panels.extend(
        source_rows(
            "filings",
            "Trader Filings",
            query_rows(
                con,
                """
                SELECT symbol, filed_date AS source_date,
                       TRY_CAST(json_extract(raw, '$.holdings_value_thousands') AS DOUBLE) AS score,
                       coalesce(trader_name, filer_name) AS label, action AS caption
                FROM disclosures
                WHERE symbol IS NOT NULL
                ORDER BY filed_date DESC NULLS LAST
                LIMIT 50
                """,
            ),
        )
    )
    panels.extend(
        source_rows(
            "news",
            "News / Catalysts",
            query_rows(
                con,
                """
                SELECT symbol, event_date AS source_date, 1 AS score,
                       event AS label, expected_impact AS caption
                FROM catalysts
                ORDER BY event_date ASC NULLS LAST
                LIMIT 50
                """,
            ),
        )
    )
    return [_compact_empty_fields(row) for row in panels]


def source_rows(source_key: str, title: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _compact_empty_fields(
            {
                "source_key": source_key,
                "title": title,
                "symbol": str(row.get("symbol") or "").upper(),
                "score": row.get("score"),
                "label": row.get("label"),
                "caption": row.get("caption"),
                "source_date": row.get("source_date"),
            }
        )
        for row in rows
        if row.get("symbol")
    ]


def technicals(
    con: Any,
    symbols: list[str] | set[str] | tuple[str, ...] | None = None,
    price_history: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    normalized_symbols = sorted({str(symbol or "").upper() for symbol in (symbols or []) if str(symbol or "").strip()})
    where_clause = ""
    params: list[Any] = []
    if normalized_symbols:
        where_clause = f"WHERE upper(symbol) IN ({', '.join(['?'] * len(normalized_symbols))})"
        params.extend(normalized_symbols)
    rows = query_rows(
        con,
        f"""
        SELECT symbol, date, features
        FROM technical_features
        {where_clause}
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY date DESC) = 1
        ORDER BY date DESC, symbol
        LIMIT 1000
        """,
        params,
    )
    decoded = [decode_fields(row, ("features",)) for row in rows]
    if price_history is None:
        price_history = technical_price_history(con, [str(row.get("symbol") or "").upper() for row in decoded], days=253)
    for row in decoded:
        symbol = str(row.get("symbol") or "").upper()
        features = row.get("features") if isinstance(row.get("features"), dict) else {}
        history = price_history.get(symbol) or []
        row["close"] = features.get("close")
        row["ma20"] = features.get("ma20")
        row["ma50"] = features.get("ma50")
        row["ma200"] = features.get("ma200")
        row["return_20d"] = features.get("return_20d")
        row["return_60d"] = features.get("return_60d")
        row["return_3m"] = features.get("return_3m") if features.get("return_3m") is not None else trailing_return(history, days=63)
        row["return_ytd"] = features.get("return_ytd") if features.get("return_ytd") is not None else period_return(history, "ytd")
        row["return_1y"] = features.get("return_1y") if features.get("return_1y") is not None else period_return(history, "1y")
        row["technical_score"] = features.get("technical_score")
        row["drawdown_from_high"] = features.get("drawdown_from_high")
        row["range_recovery"] = features.get("range_recovery")
        row["volume_ratio_20_60"] = features.get("volume_ratio_20_60")
        row["rel_volume_1m"] = features.get("rel_volume_1m") if features.get("rel_volume_1m") is not None else relative_volume(history, recent_days=22, baseline_days=63)
        row["volume_bars_1m"] = one_month_volume_bar_points(history)
        row["atr_pct_1m"] = features.get("atr_pct_1m") if features.get("atr_pct_1m") is not None else average_true_range_pct(history, days=22)
        row["atr_pct_1m_points"] = true_range_pct_points(history, days=22)
        row["chart_1y"] = sampled_price_points(history, max_points=253)
        row["rs_1m_bars"] = one_month_bar_points(history)
        row["rs_3m_bars"] = period_bar_points(history, days=63)
        row["source"] = features.get("source") or features.get("price_source")
    return [_compact_empty_fields(row) for row in decoded]


def technical_price_history(con: Any, symbols: list[str], days: int = 253) -> dict[str, list[dict[str, Any]]]:
    normalized = sorted({symbol for symbol in symbols if symbol})
    if not normalized:
        return {}
    placeholders = ", ".join(["?"] * len(normalized))
    history_rows = query_rows(
        con,
        f"""
        SELECT symbol, date, high, low, close, volume
        FROM (
            SELECT symbol, date, high, low, close, volume,
                   row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS recency_rank
            FROM prices_daily
            WHERE symbol IN ({placeholders})
        )
        WHERE recency_rank <= {int(days)}
        ORDER BY symbol, date
        """,
        normalized,
    )
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in history_rows:
        symbol = str(row.get("symbol") or "").upper()
        close = row.get("close")
        if not symbol or close is None:
            continue
        by_symbol.setdefault(symbol, []).append(
            {
                "date": str(row.get("date") or ""),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": close,
                "volume": row.get("volume"),
            }
        )
    return by_symbol


def sampled_price_points(history: list[dict[str, Any]], max_points: int = 253) -> list[float] | None:
    closes = [_number_from_any(point.get("close")) for point in history]
    closes = [close for close in closes if close]
    if len(closes) < 2:
        return None
    if len(closes) <= max_points:
        return [round(close, 4) for close in closes]
    last_index = len(closes) - 1
    sampled: list[float] = []
    for index in range(max_points):
        source_index = round((index / (max_points - 1)) * last_index)
        sampled.append(round(closes[source_index], 4))
    return sampled


def one_month_bar_points(history: list[dict[str, Any]]) -> list[float] | None:
    return period_bar_points(history, days=22)


def period_bar_points(history: list[dict[str, Any]], days: int) -> list[float] | None:
    closes = [_number_from_any(point.get("close")) for point in history[-days:]]
    closes = [close for close in closes if close]
    if len(closes) < 2:
        return None
    low = min(closes)
    high = max(closes)
    spread = high - low or 1
    return [round(((close - low) / spread) * 100, 2) for close in closes]


def one_month_volume_bar_points(history: list[dict[str, Any]]) -> list[float] | None:
    volumes = [_number_from_any(point.get("volume")) for point in history[-22:]]
    volumes = [volume for volume in volumes if volume and volume > 0]
    if len(volumes) < 2:
        return None
    peak = max(volumes) or 1
    return [round((volume / peak) * 100, 2) for volume in volumes]


def trailing_return(history: list[dict[str, Any]], days: int) -> float | None:
    points = [point for point in history[-days:] if point.get("close") not in (None, 0)]
    if len(points) < 2:
        return None
    start = _number_from_any(points[0].get("close"))
    end = _number_from_any(points[-1].get("close"))
    if not start or not end:
        return None
    return (end / start) - 1


def relative_volume(history: list[dict[str, Any]], recent_days: int, baseline_days: int) -> float | None:
    volumes = [_number_from_any(point.get("volume")) for point in history if point.get("volume") not in (None, 0)]
    volumes = [volume for volume in volumes if volume and volume > 0]
    if len(volumes) < recent_days + 1:
        return None
    recent = volumes[-recent_days:]
    baseline = volumes[-(recent_days + baseline_days) : -recent_days] or volumes[:-recent_days]
    recent_avg = sum(recent) / len(recent)
    baseline_avg = sum(baseline) / len(baseline) if baseline else None
    if not baseline_avg:
        return None
    return recent_avg / baseline_avg


def true_range_pct_points(history: list[dict[str, Any]], days: int) -> list[float] | None:
    points = history[-(days + 1) :]
    values: list[float] = []
    previous_close: float | None = None
    for point in points:
        high = _number_from_any(point.get("high"))
        low = _number_from_any(point.get("low"))
        close = _number_from_any(point.get("close"))
        if not high or not low or not close:
            previous_close = close
            continue
        ranges = [high - low]
        if previous_close:
            ranges.extend([abs(high - previous_close), abs(low - previous_close)])
        true_range = max(ranges)
        values.append(true_range / close)
        previous_close = close
    values = values[-days:]
    return [round(value, 4) for value in values] if len(values) >= 2 else None


def average_true_range_pct(history: list[dict[str, Any]], days: int) -> float | None:
    values = true_range_pct_points(history, days)
    if not values:
        return None
    return sum(values) / len(values)


def period_return(history: list[dict[str, Any]], period: str) -> float | None:
    points = [point for point in history if point.get("close") not in (None, 0)]
    if len(points) < 2:
        return None
    last = points[-1]
    last_close = _number_from_any(last.get("close"))
    if not last_close:
        return None
    if period == "ytd":
        year = str(last.get("date") or "")[:4]
        start = next((point for point in points if str(point.get("date") or "").startswith(year)), points[0])
    else:
        start = points[0]
    start_close = _number_from_any(start.get("close"))
    if not start_close:
        return None
    return (last_close / start_close) - 1


def research_packets(con: Any) -> list[dict[str, Any]]:
    symbols = [
        str(row.get("symbol") or "").upper()
        for row in query_rows(
            con,
            """
            SELECT symbol
            FROM candidates
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY run_date DESC, score DESC) = 1
            ORDER BY score DESC
            LIMIT 25
            """,
        )
    ]
    packets: list[dict[str, Any]] = []
    for symbol in symbols:
        if not symbol:
            continue
        packet = build_research_packet(con, symbol)
        if not packet.get("candidate"):
            continue
        memo = generate_deterministic_memo(packet)
        report = memo.get("json") or {}
        packets.append(
            {
                "symbol": symbol,
                "created_at": packet.get("created_at"),
                "decision": report.get("decision"),
                "conviction": report.get("conviction"),
                "why_now": report.get("why_now"),
                "bull_case": report.get("bull_case"),
                "bear_case": report.get("bear_case"),
                "invalidation": report.get("invalidation"),
                "entry_plan": report.get("entry_plan"),
                "position_sizing": report.get("position_sizing"),
                "portfolio_impact": report.get("portfolio_impact"),
                "evidence_count": len(packet.get("arco_thesis_evidence") or []),
                "price_rows": len(packet.get("prices_recent") or []),
                "has_position": bool(packet.get("portfolio_position")),
            }
        )
    return packets


def confidence_to_number(label: str, score: float, source_count: int) -> int:
    normalized = label.lower()
    if "high" in normalized:
        return 85
    if "medium" in normalized:
        return 65
    if "low" in normalized:
        return 35
    return int(max(20, min(95, score * 0.7 + min(source_count, 8) * 4)))


def top_source_label(counts: dict[str, int], components: dict[str, Any]) -> str:
    if counts:
        return max(counts.items(), key=lambda item: item[1])[0]
    if components:
        return max(components.items(), key=lambda item: float(item[1] or 0))[0]
    return "candidate"


def portfolio(con: Any) -> list[dict[str, Any]]:
    effective_rows = brokers.effective_portfolio_rows(con)
    rows: list[dict[str, Any]] = []
    for item in effective_rows:
        symbol = str(item.get("symbol") or "").upper()
        instrument = query_rows(con, "SELECT name, asset_class, category FROM instruments WHERE symbol = ? LIMIT 1", [symbol])
        meta = instrument[0] if instrument else {}
        rows.append(
            {
                "symbol": symbol,
                "name": meta.get("name") or symbol,
                "asset_class": item.get("asset_class") or meta.get("asset_class"),
                "category": meta.get("category"),
                "quantity": item.get("quantity"),
                "avg_cost": item.get("avg_cost") or item.get("average_cost"),
                "average_cost": item.get("average_cost") or item.get("avg_cost"),
                "purchase_date": item.get("purchase_date"),
                "holding_days": item.get("holding_days"),
                "tax_lot_term": item.get("tax_lot_term") or ("broker" if item.get("source") == "ibkr" else "unknown"),
                "notes": item.get("notes") or "",
                "position_source": item.get("source"),
                "provider": item.get("provider"),
                "account_id": item.get("account_id"),
                "updated_at": item.get("updated_at"),
                "market_price": item.get("market_price"),
                "broker_market_value": item.get("market_value"),
                "broker_unrealized_pnl": item.get("unrealized_pnl"),
            }
        )
    quotes_by_symbol = {str(row.get("symbol") or "").upper(): row for row in canonical_quote_rows(con)}
    decision_by_symbol = {
        str(row.get("symbol") or "").upper(): row
        for row in query_rows(
            con,
            """
            SELECT symbol, action_grade, freshness_status
            FROM decision_queue
            WHERE symbol IN (SELECT symbol FROM portfolio_positions)
            """,
        )
    }
    for row in rows:
        decision = decision_by_symbol.get(str(row.get("symbol") or "").upper(), {})
        action_grade = decision.get("action_grade")
        freshness = decision.get("freshness_status")
        row["signal"] = action_grade
        row["action"] = "Refresh data" if freshness in {"stale", "failed", "missing"} else "Review setup" if action_grade in {"Reject", "Watch", "Research", "Act"} else None
        quote = quotes_by_symbol.get(str(row.get("symbol") or "").upper(), {})
        price = row.get("market_price") or quote.get("price")
        row["price"] = price
        row["change_pct"] = quote.get("change_pct")
        row["change_abs"] = quote.get("change_abs")
        row["quote_source"] = "ibkr" if row.get("position_source") == "ibkr" and row.get("broker_market_value") is not None else quote.get("source")
        row["quote_freshness"] = quote.get("freshness_status")
        if price is None:
            row["market_value"] = row.get("broker_market_value")
            row["unrealized_pnl"] = row.get("broker_unrealized_pnl")
            row["unrealized_pnl_pct"] = None
            continue
        quantity = float(row.get("quantity") or 0)
        avg_cost = float(row.get("avg_cost") or 0)
        row["market_value"] = row.get("broker_market_value") if row.get("broker_market_value") is not None else quantity * float(price)
        row["unrealized_pnl"] = row.get("broker_unrealized_pnl") if row.get("broker_unrealized_pnl") is not None else quantity * (float(price) - avg_cost)
        row["unrealized_pnl_pct"] = ((float(price) - avg_cost) / avg_cost) * 100 if avg_cost > 0 else None
    total_market_value = sum(float(row.get("market_value") or 0) for row in rows if row.get("market_value") is not None)
    for row in rows:
        row["portfolio_weight"] = (float(row["market_value"]) / total_market_value) * 100 if total_market_value and row.get("market_value") is not None else None
    return [_compact_empty_fields(row) for row in rows]


def theses(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(con, "SELECT symbol, thesis_json, updated_at FROM theses ORDER BY updated_at DESC")
    decoded = [decode_fields(row, ("thesis_json",)) for row in rows]
    if decoded:
        return decoded
    birdclaw_rows = query_rows(
        con,
        """
        SELECT symbol, author, created_at AS updated_at, thesis_summary, claims, engagement, source_url
        FROM birdclaw_theses
        ORDER BY created_at DESC
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("claims", "engagement")) for row in birdclaw_rows]


def catalysts(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        WITH calendar_rows AS (
            SELECT id, symbol, event_date, event, expected_impact, source,
                   start_at, end_at, timezone, event_scope, event_kind, importance,
                   COALESCE(verification_status, 'confirmed') AS verification_status,
                   source_url, source_name, raw
            FROM catalysts
            UNION ALL
            SELECT 'earnings-' || symbol || '-' || CAST(event_date AS TEXT) AS id,
                   symbol,
                   event_date,
                   event_type AS event,
                   'Earnings event from yfinance calendar snapshot' AS expected_impact,
                   source,
                   CAST(NULL AS TIMESTAMP) AS start_at,
                   CAST(NULL AS TIMESTAMP) AS end_at,
                   'America/New_York' AS timezone,
                   'watchlist' AS event_scope,
                   'earnings' AS event_kind,
                   'medium' AS importance,
                   'watch' AS verification_status,
                   CAST(NULL AS TEXT) AS source_url,
                   'yfinance' AS source_name,
                   metrics AS raw
            FROM earnings_events
            UNION ALL
            SELECT 'filing-' || id AS id,
                   symbol,
                   COALESCE(filed_date, event_date) AS event_date,
                   COALESCE(source_type, 'filing') || ' filed' AS event,
                   COALESCE(action, amount, 'Public disclosure filing') AS expected_impact,
                   source_type AS source,
                   CAST(NULL AS TIMESTAMP) AS start_at,
                   CAST(NULL AS TIMESTAMP) AS end_at,
                   'America/New_York' AS timezone,
                   'filing' AS event_scope,
                   'filing' AS event_kind,
                   'medium' AS importance,
                   'confirmed' AS verification_status,
                   source_url,
                   trader_name AS source_name,
                   raw
            FROM disclosures
            WHERE COALESCE(filed_date, event_date) IS NOT NULL
        )
        SELECT *
        FROM calendar_rows
        ORDER BY
            CASE WHEN event_date >= current_date THEN 0 ELSE 1 END,
            CASE WHEN event_date >= current_date THEN event_date END ASC NULLS LAST,
            CASE WHEN event_date < current_date THEN event_date END DESC NULLS LAST,
            start_at ASC NULLS LAST,
            event
        LIMIT 200
        """,
    )
    decoded = [decode_fields(row, ("raw",)) for row in rows]
    return [_compact_empty_fields(row) for row in decoded]


def fundamentals(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, period_end, filing_date, form_type, metrics, source_url,
               'equity' AS asset_class, 'sec_companyfacts' AS source
        FROM equity_fundamentals
        UNION ALL
        SELECT symbol, date AS period_end, date AS filing_date, 'coingecko_market' AS form_type,
               metrics, source AS source_url, 'crypto' AS asset_class, source
        FROM crypto_fundamentals
        ORDER BY filing_date DESC, symbol
        LIMIT 200
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("metrics",))) for row in rows]


def quotes(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        WITH latest_intraday AS (
            SELECT symbol, observed_at, price, change_pct, change_abs, currency, source, raw,
                   concat(source, ':', symbol) AS freshness_key
            FROM quotes_intraday
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY observed_at DESC) = 1
        ),
        intraday_status AS (
            SELECT i.*, COALESCE(f.freshness_status, 'unknown') AS freshness_status
            FROM latest_intraday i
            LEFT JOIN source_freshness f ON f.source_key = i.freshness_key
        ),
        latest_daily AS (
            SELECT symbol, date AS observed_at, close AS price,
                   CASE WHEN previous_close > 0 THEN ((close - previous_close) / previous_close) * 100 ELSE NULL END AS change_pct,
                   CASE WHEN previous_close IS NOT NULL THEN close - previous_close ELSE NULL END AS change_abs,
                   'USD' AS currency,
                   concat('previous_close:', source) AS source,
                   '{}' AS raw,
                   concat('previous_close:', symbol) AS freshness_key
            FROM (
                SELECT symbol, date, close, source,
                       lag(close) OVER (PARTITION BY symbol ORDER BY date) AS previous_close
                FROM prices_daily
            )
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY date DESC) = 1
        ),
        daily_status AS (
            SELECT d.*, COALESCE(f.freshness_status, 'unknown') AS freshness_status
            FROM latest_daily d
            LEFT JOIN source_freshness f ON f.source_key = d.freshness_key
        ),
        candidates AS (
            SELECT 0 AS priority, symbol, observed_at, price, change_pct, change_abs, currency, source, raw, freshness_status
            FROM intraday_status WHERE freshness_status = 'fresh'
            UNION ALL
            SELECT 1 AS priority, symbol, observed_at, price, change_pct, change_abs, currency, source, raw, freshness_status
            FROM daily_status WHERE freshness_status = 'fresh'
            UNION ALL
            SELECT 2 AS priority, symbol, observed_at, price, change_pct, change_abs, currency, source, raw, freshness_status
            FROM intraday_status WHERE freshness_status <> 'fresh'
            UNION ALL
            SELECT 2 AS priority, symbol, observed_at, price, change_pct, change_abs, currency, source, raw, freshness_status
            FROM daily_status WHERE freshness_status <> 'fresh'
        )
        SELECT symbol, observed_at, price, change_pct, change_abs, currency, source, raw, freshness_status
        FROM candidates
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY priority ASC, observed_at DESC) = 1
        ORDER BY observed_at DESC, symbol
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]


def screener(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT run_id, symbol, observed_at, name, metrics, source
        FROM market_screener_rows
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY observed_at DESC) = 1
        ORDER BY observed_at DESC, symbol
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("metrics",))) for row in rows]


def options_expiries(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, expiry, dte, contracts_count, observed_at, source, raw
        FROM options_expiries
        ORDER BY observed_at DESC, symbol, expiry
        LIMIT 300
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]


def options_chain(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, expiry, strike, option_type, bid, ask, mid, iv, delta, gamma,
               theta, vega, rho, theo, bid_iv, ask_iv, contract_symbol, observed_at, source, raw
        FROM options_chain
        QUALIFY dense_rank() OVER (PARTITION BY symbol, expiry ORDER BY observed_at DESC) = 1
        ORDER BY observed_at DESC, symbol, expiry, strike, option_type
        LIMIT 400
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]


def options_provider_capabilities(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT provider, observed_at, supports_expiries, supports_chain_quotes,
               supports_greeks, supports_theoretical_price, supports_open_interest,
               supports_volume, supports_full_chain, status, detail, raw
        FROM options_provider_capabilities
        ORDER BY observed_at DESC, provider
        LIMIT 20
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]


def options_expiry_signals(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, expiry, as_of, source, dte, spot, contract_count, chain_rows,
               atm_strike, atm_iv, expected_move, expected_move_pct, put_call_iv_skew,
               call_spread_pct, put_spread_pct, spread_quality, liquidity_score,
               hedge_put_strike, hedge_put_mid, covered_call_strike, covered_call_mid,
               unavailable_signals, raw
        FROM options_expiry_signals
        ORDER BY as_of DESC, symbol, dte, expiry
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("unavailable_signals", "raw"))) for row in rows]


def options_ticker_signals(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, source, status, nearest_expiry, nearest_dte, atm_iv,
               iv_regime, expected_move, expected_move_pct, skew_signal,
               put_call_iv_skew, spread_quality, liquidity_score, hedge_summary,
               income_summary, unavailable_signals, raw
        FROM options_ticker_signals
        ORDER BY as_of DESC, symbol
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("unavailable_signals", "raw"))) for row in rows]


def option_strategy_versions(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT strategy_version, strategy_name, version, created_at, status,
               parameters, promoted_at, supersedes, notes
        FROM option_strategy_versions
        ORDER BY created_at DESC, strategy_version
        LIMIT 100
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("parameters",))) for row in rows]


def option_radar_summary(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        WITH latest_snapshot AS (
            SELECT max(snapshot_time) AS snapshot_time
            FROM option_snapshot
        ),
        latest_candidates AS (
            SELECT max(snapshot_time) AS snapshot_time
            FROM candidate_event
        )
        SELECT
            (SELECT snapshot_time FROM latest_snapshot) AS latest_snapshot_time,
            (SELECT snapshot_time FROM latest_candidates) AS latest_candidate_time,
            (SELECT count(DISTINCT ticker) FROM option_snapshot WHERE snapshot_time = (SELECT snapshot_time FROM latest_snapshot)) AS scanned_tickers_current,
            (SELECT count(*) FROM option_snapshot WHERE snapshot_time = (SELECT snapshot_time FROM latest_snapshot)) AS snapshot_rows_current,
            (SELECT count(DISTINCT ticker) FROM option_snapshot) AS scanned_tickers_total,
            (SELECT count(*) FROM option_snapshot) AS snapshot_rows_total,
            (SELECT string_agg(DISTINCT data_source, ', ' ORDER BY data_source) FROM option_snapshot) AS data_sources,
            (
                SELECT count(DISTINCT ticker)
                FROM candidate_event
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND state != 'REJECT'
            ) AS opportunity_tickers_current,
            (
                SELECT count(*)
                FROM candidate_event
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND state != 'REJECT'
            ) AS opportunity_rows_current,
            (
                SELECT count(*)
                FROM candidate_event
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND state = 'FIRE'
            ) AS fire_rows_current,
            (
                SELECT count(*)
                FROM candidate_event
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND state = 'SETUP'
            ) AS setup_rows_current,
            (
                SELECT count(*)
                FROM candidate_event
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND state = 'WATCH'
            ) AS watch_rows_current,
            (
                SELECT count(*)
                FROM candidate_event
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND state = 'REJECT'
            ) AS reject_rows_current,
            (
                SELECT count(*)
                FROM option_radar_opportunity
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND tier = 'Exceptional'
            ) AS exceptional_opportunities_current,
            (
                SELECT count(*)
                FROM option_radar_opportunity
                WHERE snapshot_time = (SELECT snapshot_time FROM latest_candidates)
                  AND tier = 'Research'
            ) AS research_opportunities_current
        """,
    )
    return [_compact_empty_fields(row) for row in rows]


def option_radar_opportunity(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT opportunity_id, snapshot_time, ticker, strategy_version, tier,
               primary_event_id, primary_contract_id, primary_state,
               conviction_score, asymmetry_score, entry_quality_score,
               catalyst_score, evidence_score, regime_score, survivability_score,
               learning_score, required_move_pct, premium_mid,
               premium_fill_assumption, required_10x_price, buy_under,
               entry_zone, max_loss_assumption, position_sizing_band,
               why_now, kill_switch, top_reasons, blockers, quality_status,
               quality_flags, evidence_refs, alternative_contracts, raw
        FROM option_radar_opportunity
        ORDER BY CASE tier WHEN 'Exceptional' THEN 0 WHEN 'Research' THEN 1 ELSE 2 END,
                 conviction_score DESC NULLS LAST,
                 required_move_pct ASC NULLS LAST,
                 ticker
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("top_reasons", "blockers", "quality_flags", "evidence_refs", "alternative_contracts", "raw"))) for row in rows]


def option_snapshot(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT snapshot_time, ticker, underlying_price, expiration, strike, option_type,
               bid, ask, mid, last, volume, open_interest, iv, delta, gamma, theta,
               vega, dte, spread_pct, data_source, contract_id, raw
        FROM option_snapshot
        ORDER BY snapshot_time DESC, ticker, expiration, strike, option_type
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]


def option_features(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT snapshot_time, contract_id, ticker, required_2x_price,
               required_5x_price, required_10x_price, required_move_10x_pct,
               breakeven, iv_percentile, iv_rank, liquidity_score,
               convexity_score, raw
        FROM option_features
        ORDER BY snapshot_time DESC, ticker, contract_id
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]


def stock_features(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT snapshot_time, ticker, price, ma_20, ma_50, ma_200,
               rs_vs_qqq_20d, rs_vs_qqq_60d, atr_pct, volume_ratio,
               distance_from_52w_high, base_length_days, breakout_level, raw
        FROM stock_features
        ORDER BY snapshot_time DESC, ticker
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]


def agent_thesis(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT thesis_id, ticker, created_at, agent_version, bull_target_price,
               bull_target_date, base_target_price, core_thesis, required_proofs,
               invalidation_conditions, catalysts, catalyst_summary, bear_case,
               confidence, evidence_refs, raw
        FROM agent_thesis
        ORDER BY created_at DESC, ticker
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("required_proofs", "invalidation_conditions", "catalysts", "evidence_refs", "raw"))) for row in rows]


def agent_thesis_request(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT request_id, created_at, ticker, event_id, strategy_version,
               priority_score, status, prompt, context, raw
        FROM agent_thesis_request
        ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'failed' THEN 1 WHEN 'agent_failed' THEN 1 WHEN 'superseded' THEN 2 ELSE 3 END,
                 priority_score DESC NULLS LAST,
                 created_at DESC
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("context", "raw"))) for row in rows]


def agent_thesis_validation(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT validation_id, thesis_id, ticker, strategy_version,
               validation_date, candidate_event_id, candidate_snapshot_time,
               validated_at, state, reason, option_still_valid, stock_progress,
               iv_status, candidate_state,
               proof_status, catalyst_status, invalidation_status, evidence_status,
               red_team_status, red_team_flags, evidence_refs, raw
        FROM agent_thesis_validation
        ORDER BY validation_date DESC NULLS LAST, validated_at DESC, ticker
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("red_team_flags", "evidence_refs", "raw"))) for row in rows]


def agent_postmortem_request(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT request_id, created_at, source_type, source_id, ticker,
               strategy_version, priority_score, status, prompt, context, raw
        FROM agent_postmortem_request
        ORDER BY created_at DESC, priority_score DESC NULLS LAST
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("context", "raw"))) for row in rows]


def agent_postmortem(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT postmortem_id, request_id, source_type, source_id, created_at,
               agent_version, ticker, strategy_version, outcome_type,
               failure_type, evidence, proposed_rule_change,
               proposed_parameter_changes, expected_effect, risk, confidence,
               evidence_refs, raw
        FROM agent_postmortem
        ORDER BY created_at DESC, confidence DESC NULLS LAST
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("evidence", "proposed_parameter_changes", "evidence_refs", "raw"))) for row in rows]


def candidate_event(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT event_id, snapshot_time, ticker, contract_id, strategy_version,
               state, premium_mid, premium_fill_assumption, required_10x_price,
               required_move_pct, buy_under, trigger_reason, thesis_id, score,
               quality_status, quality_flags, raw
        FROM candidate_event
        ORDER BY snapshot_time DESC, score DESC, ticker
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("quality_flags", "raw"))) for row in rows]


def shadow_trade(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT trade_id, event_id, entry_time, entry_price_assumption, exit_time,
               exit_price, status, max_return_seen, max_drawdown_seen, time_to_2x,
               time_to_5x, time_to_10x, exit_reason, raw
        FROM shadow_trade
        ORDER BY entry_time DESC, trade_id
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]


def candidate_event_mark(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT mark_id, event_id, contract_id, ticker, strategy_version,
               candidate_state, mark_time, alert_time, premium_fill_assumption,
               mark_price, current_return, return_1d, return_5d, return_20d,
               return_60d, max_return_since_alert, max_drawdown_since_alert,
               time_to_2x, time_to_5x, time_to_10x, dte, spread_pct, iv,
               underlying_price, raw
        FROM candidate_event_mark
        ORDER BY mark_time DESC, ticker, contract_id
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]


def candidate_event_attribution(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT attribution_id, event_id, contract_id, ticker, strategy_version,
               candidate_state, snapshot_time, prior_snapshot_time,
               option_return, underlying_return, iv_change, theta_decay,
               spread_change, stock_move_effect, iv_effect, theta_effect,
               spread_effect, unexplained_effect, label, raw
        FROM candidate_event_attribution
        ORDER BY snapshot_time DESC, ticker, contract_id
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]


def shadow_trade_mark(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT mark_id, trade_id, event_id, contract_id, ticker,
               strategy_version, mark_time, entry_time, entry_price_assumption,
               mark_price, current_return, return_1d, return_5d, return_20d,
               return_60d, max_return_since_alert, max_drawdown_since_alert,
               time_to_2x, time_to_5x, time_to_10x, dte, spread_pct, iv,
               underlying_price, expired_worthless_probability_change, raw
        FROM shadow_trade_mark
        ORDER BY mark_time DESC, ticker, contract_id
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]


def radar_state_transition(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT transition_id, evaluated_at, snapshot_time, ticker, contract_id,
               strategy_version, previous_state, state, candidate_state, event_id,
               trade_id, mark_id, thesis_id, trigger_reason, evidence_refs, raw
        FROM radar_state_transition
        ORDER BY snapshot_time DESC, ticker, contract_id
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("evidence_refs", "raw"))) for row in rows]


def option_attribution(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT attribution_id, trade_id, event_id, contract_id, snapshot_time,
               prior_snapshot_time, option_return, underlying_return, iv_change,
               theta_decay, spread_change, stock_move_effect, iv_effect,
               theta_effect, spread_effect, unexplained_effect, label, raw
        FROM option_attribution
        ORDER BY snapshot_time DESC, trade_id
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]


def missed_winner_event(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT missed_id, detected_at, ticker, contract_id, strategy_version,
               first_snapshot_time, winner_snapshot_time, entry_price_assumption,
               winner_price, max_return_seen, winner_threshold, filter_reason,
               proposed_strategy_family, raw
        FROM missed_winner_event
        ORDER BY detected_at DESC, max_return_seen DESC
        LIMIT 1000
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]


def strategy_mutation_proposal(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT proposal_id, created_at, source_type, strategy_version,
               proposed_strategy_version, proposed_parameter_changes, rationale,
               expected_effect, risk, status, requires_backtest,
               requires_forward_test, human_approval_status, approved_by,
               approved_at, evidence_refs, raw
        FROM strategy_mutation_proposal
        ORDER BY created_at DESC, proposed_strategy_version
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("proposed_parameter_changes", "evidence_refs", "raw"))) for row in rows]


def strategy_backtest_result(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT backtest_id, proposal_id, evaluated_at, strategy_version,
               proposed_strategy_version, lookback_start, lookback_end,
               baseline_candidate_count, proposed_candidate_count,
               baseline_hit_rate_2x, baseline_hit_rate_5x, baseline_hit_rate_10x,
               proposed_hit_rate_2x, proposed_hit_rate_5x, proposed_hit_rate_10x,
               proposed_false_positive_rate, verdict, metrics, raw
        FROM strategy_backtest_result
        ORDER BY evaluated_at DESC, proposed_strategy_version
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("metrics", "raw"))) for row in rows]


def strategy_forward_test_result(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT forward_test_id, proposal_id, evaluated_at, strategy_version,
               proposed_strategy_version, forward_start, forward_end,
               days_observed, baseline_candidate_count, proposed_candidate_count,
               baseline_hit_rate_2x, baseline_hit_rate_5x, baseline_hit_rate_10x,
               proposed_hit_rate_2x, proposed_hit_rate_5x, proposed_hit_rate_10x,
               status, verdict, metrics, raw
        FROM strategy_forward_test_result
        ORDER BY evaluated_at DESC, proposed_strategy_version
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("metrics", "raw"))) for row in rows]


def strategy_cohort_result(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT cohort_id, evaluated_at, strategy_version, cohort_type,
               cohort_value, candidate_count, hit_rate_2x, hit_rate_5x,
               hit_rate_10x, false_positive_rate, median_max_return,
               median_max_drawdown, average_time_to_2x, early_entry_rate,
               theta_iv_bleed_rate, good_convexity_rate, qqq_above_200d_rate,
               raw
        FROM strategy_cohort_result
        ORDER BY evaluated_at DESC, cohort_type, candidate_count DESC
        LIMIT 500
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]


def options_payoff_scenarios(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, symbol, as_of, expiry, strategy_type, spot, dte, iv,
               net_premium, max_profit, max_loss, breakevens, legs, curve,
               diagnostics, source
        FROM options_payoff_scenarios
        ORDER BY as_of DESC, symbol, expiry, strategy_type
        LIMIT 300
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("breakevens", "legs", "curve", "diagnostics"))) for row in rows]


def news(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, published_at, provider, title, related_symbols, link, source, raw
        FROM news_items
        ORDER BY published_at DESC
        LIMIT 200
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("related_symbols", "raw"))) for row in rows]


def tradingview_symbol_search(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, query, observed_at, symbol, description, instrument_type,
               exchange, country, currency, source, raw
        FROM tradingview_symbol_search
        ORDER BY observed_at DESC, query, symbol
        LIMIT 300
        """,
    )
    return [decode_fields(row, ("raw",)) for row in rows]


def tradingview_watchlists(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, observed_at, name, color, symbol_count, symbols, source, raw
        FROM tradingview_watchlists
        ORDER BY observed_at DESC, color NULLS LAST, name
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("symbols", "raw")) for row in rows]


def tradingview_alerts(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, observed_at, name, symbol, alert_type, condition, value,
               active, status, fired_at, source, raw
        FROM tradingview_alerts
        ORDER BY observed_at DESC, fired_at DESC NULLS LAST, symbol
        LIMIT 300
        """,
    )
    return [decode_fields(row, ("raw",)) for row in rows]


def tradingview_chart_state(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, observed_at, layout_id, symbol, interval, url, source, raw
        FROM tradingview_chart_state
        ORDER BY observed_at DESC
        LIMIT 50
        """,
    )
    return [decode_fields(row, ("raw",)) for row in rows]


def sepa(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, score, stage, verdict, checklist, metrics
        FROM sepa_analyses
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY as_of DESC, score DESC NULLS LAST) = 1
        ORDER BY as_of DESC, score DESC NULLS LAST, symbol
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("checklist", "metrics")) for row in rows]


def liquidity(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, grade, avg_daily_volume, avg_dollar_volume,
               turnover_ratio, amihud_illiquidity, impact_1pct_adv_bps, metrics
        FROM liquidity_metrics
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY as_of DESC, avg_dollar_volume DESC NULLS LAST) = 1
        ORDER BY as_of DESC, avg_dollar_volume DESC NULLS LAST, symbol
        LIMIT 200
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("metrics",))) for row in rows]


def correlations(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, target_symbol AS symbol, as_of, lookback_days, peers, metrics
        FROM correlation_runs
        QUALIFY row_number() OVER (PARTITION BY target_symbol ORDER BY as_of DESC) = 1
        ORDER BY as_of DESC, target_symbol
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("peers", "metrics")) for row in rows]


def etf_premiums(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, market_price, nav, premium_pct, metrics, source
        FROM etf_premiums
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY as_of DESC) = 1
        ORDER BY as_of DESC, abs(premium_pct) DESC
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("metrics",)) for row in rows]


def analyst_estimates(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, estimates, source
        FROM analyst_estimates
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY as_of DESC) = 1
        ORDER BY as_of DESC, symbol
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("estimates",)) for row in rows]


def earnings(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, event_date, event_type, metrics, source
        FROM earnings_events
        ORDER BY event_date DESC, symbol
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("metrics",)) for row in rows]


def earnings_setups(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, event_date, setup_type, score, revision_score,
               surprise_score, estimate_spread_score, sentiment_score, verdict,
               metrics, source
        FROM earnings_setups
        QUALIFY dense_rank() OVER (PARTITION BY symbol ORDER BY as_of DESC) = 1
        ORDER BY as_of DESC, score DESC NULLS LAST, symbol
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("metrics",)) for row in rows]


def valuations(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        WITH valuation_history AS (
            SELECT symbol, as_of, method, fair_value, upside_pct, assumptions, diagnostics,
                   CASE
                     WHEN count(*) OVER (PARTITION BY symbol, method) > 1
                     THEN (1 - percent_rank() OVER (PARTITION BY symbol, method ORDER BY upside_pct)) * 100
                     ELSE NULL
                   END AS own_history_percentile
            FROM valuation_models
        )
        SELECT symbol, as_of, method, fair_value, upside_pct, assumptions, diagnostics,
               own_history_percentile,
               own_history_percentile AS valuation_percentile_own_history
        FROM valuation_history
        QUALIFY dense_rank() OVER (PARTITION BY symbol ORDER BY as_of DESC) = 1
        ORDER BY as_of DESC, upside_pct DESC NULLS LAST
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("assumptions", "diagnostics")) for row in rows]


def provider_runs(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, provider, capability, started_at, finished_at, status, detail, raw
        FROM provider_runs
        ORDER BY finished_at DESC
        LIMIT 100
        """,
    )
    return [_compact_empty_fields(decode_fields(row, ("raw",))) for row in rows]


def disclosures(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        WITH recent_non_13f AS (
            SELECT source_type, trader_name, filer_name, symbol, event_date, filed_date,
                   action, amount, raw, source_url
            FROM disclosures
            WHERE source_type != '13f'
            ORDER BY filed_date DESC NULLS LAST
            LIMIT 200
        ),
        all_13f AS (
            SELECT source_type, trader_name, filer_name, symbol, event_date, filed_date,
                   action, amount, raw, source_url
            FROM disclosures
            WHERE source_type = '13f'
        )
        SELECT source_type, trader_name, filer_name, symbol, event_date, filed_date,
               action, amount, raw, source_url
        FROM recent_non_13f
        UNION ALL
        SELECT source_type, trader_name, filer_name, symbol, event_date, filed_date,
               action, amount, raw, source_url
        FROM all_13f
        ORDER BY filed_date DESC NULLS LAST
        """,
    )
    decoded = [decode_fields(row, ("raw",)) for row in rows]
    enrich_13f_disclosure_rows(decoded)
    for row in decoded:
        raw = row.get("raw") or {}
        if isinstance(raw, dict):
            _copy_nonempty_raw_fields(
                row,
                raw,
                (
                    "holdings_count",
                    "holdings_value_thousands",
                    "total_value",
                    "estimated_invested_usd",
                    "performance_percent",
                    "platform_stats",
                    "metadata",
                    "transactions_count",
                    "transactions",
                    "sp500_history",
                    "source_caveat",
                    "lag_caveat",
                    "next_filing_due_date",
                ),
            )
            portfolio_history = row.get("portfolio_history") or raw.get("portfolio_history")
            if portfolio_history not in (None, "", [], {}):
                row["portfolio_history"] = portfolio_history
            holdings = raw.get("holdings")
            if isinstance(holdings, list):
                row["holding_sample"] = sorted_13f_holdings(holdings)[:25] if row.get("source_type") == "13f" else holdings[:25]
                trimmed_raw = dict(raw)
                trimmed_raw.pop("holdings", None)
                row["raw"] = trimmed_raw
    return [_compact_empty_fields(row) for row in decoded]


def _copy_nonempty_raw_fields(row: dict[str, Any], raw: dict[str, Any], fields: tuple[str, ...]) -> None:
    for field in fields:
        value = raw.get(field)
        if value not in (None, "", [], {}):
            row[field] = value


def _compact_empty_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}


def enrich_13f_disclosure_rows(rows: list[dict[str, Any]]) -> None:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        if row.get("source_type") != "13f" or not isinstance(raw, dict):
            continue
        key = str(row.get("trader_name") or row.get("filer_name") or raw.get("cik") or "")
        grouped.setdefault(key, []).append(row)

    for group_rows in grouped.values():
        ordered = sorted(group_rows, key=lambda row: str(row.get("event_date") or ""))
        previous_weights: dict[str, float] = {}
        filing_history = []
        for row in ordered:
            raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
            holdings = sorted_13f_holdings(raw.get("holdings") if isinstance(raw, dict) else [])
            current_weights = {holding_key(holding): float(holding.get("weight") or 0.0) for holding in holdings}
            filing_history.append(
                {
                    "date": str(row.get("event_date") or ""),
                    "filed_date": str(row.get("filed_date") or ""),
                    "value": float(raw.get("holdings_value_thousands") or sum(float(holding.get("market_value") or 0.0) for holding in holdings)),
                    "holdings_count": raw.get("holdings_count") or len(holdings),
                }
            )
            history = []
            for holding in holdings[:25]:
                key = holding_key(holding)
                weight = float(holding.get("weight") or 0.0)
                previous = previous_weights.get(key, 0.0)
                history.append(
                    {
                        "symbol": holding.get("symbol"),
                        "security": holding.get("name"),
                        "put_call": holding.get("put_call"),
                        "date": str(row.get("event_date") or ""),
                        "filed_date": str(row.get("filed_date") or ""),
                        "type": "ADD" if previous == 0 and weight > 0 else "INCREASE" if weight > previous else "DECREASE" if weight < previous else "UNCHANGED",
                        "quantity": holding.get("shares_or_principal_amount") or 0,
                        "estimated_amount": float(holding.get("market_value") or 0.0),
                        "price": None,
                        "weight_before": previous,
                        "weight_after": weight,
                    }
                )
            row["allocation_history"] = history
            row["portfolio_history"] = list(filing_history)
            previous_weights = current_weights


def sorted_13f_holdings(holdings: Any) -> list[dict[str, Any]]:
    if not isinstance(holdings, list):
        return []
    total_value = sum(float(row.get("value_thousands") or 0.0) for row in holdings if isinstance(row, dict))
    sorted_rows = []
    for holding in holdings:
        if not isinstance(holding, dict):
            continue
        value = float(holding.get("value_thousands") or 0.0)
        row = dict(holding)
        row["market_value"] = value
        row["weight"] = (value / total_value * 100) if total_value else 0.0
        sorted_rows.append(row)
    return sorted(sorted_rows, key=lambda row: float(row.get("weight") or 0.0), reverse=True)


def holding_key(holding: dict[str, Any]) -> str:
    return ":".join(
        [
            str(holding.get("symbol") or holding.get("cusip") or holding.get("name") or ""),
            str(holding.get("put_call") or ""),
            str(holding.get("title") or ""),
        ]
    )


def _allocation_key(holding: dict[str, Any]) -> str:
    return ":".join(
        [
            _normalize_symbol_token(holding.get("symbol")),
            str(holding.get("put_call") or ""),
            str(holding.get("security") or holding.get("name") or ""),
        ]
    )


def reports(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, symbol, created_at, report_type, report_markdown, report_json, evidence
        FROM research_reports
        ORDER BY created_at DESC
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("report_json", "evidence")) for row in rows]


def source_health(con: Any) -> list[dict[str, Any]]:
    return query_rows(con, "SELECT * FROM source_health ORDER BY checked_at DESC")


def trader_profiles(profile_dir: Path) -> list[dict[str, Any]]:
    if not profile_dir.exists():
        return []
    rows = []
    for path in sorted(profile_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        rows.append({"id": path.stem, "name": first_heading(text) or path.stem, "profile_markdown": text})
    return rows


def first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def decode_fields(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    decoded = dict(row)
    for field in fields:
        if field in decoded:
            try:
                decoded[field] = decode_json_value(decoded[field])
            except Exception:
                pass
    return decoded


def decode_json_value(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)
