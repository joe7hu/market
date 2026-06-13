"""Top-level panel orchestration and dispatch."""

from __future__ import annotations
from pathlib import Path
from threading import Lock
from typing import Any
from investment_panel.core.config import AppConfig, config_to_dict, load_config
from investment_panel.core import brokers
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.daily_brief import daily_brief
from investment_panel.core.decision import canonical_quote_rows, decision_readiness_rows, effective_watchlist, manual_watchlist_rows, refresh_decision_read_models
from investment_panel.core.portfolio_intelligence import correlation_edges, exposure_clusters, portfolio_risk_cards, review_actions
from investment_panel.core.signals import signal_rows
from investment_panel.core.sources import ensure_canonical_sources, source_item_rows, source_registry_rows, source_run_rows, source_ticker_ranking_rows, ticker_source_signal_rows
from investment_panel.core.thesis_monitor import thesis_monitor_rows

from investment_panel.core.panel.coerce import _normalize_symbol_token, _symbols_from_value
from investment_panel.core.panel.technicals import technicals
from investment_panel.core.panel.disclosures import disclosures
from investment_panel.core.panel.market_environment import market_context, market_environment_assets, market_environment_model, market_valuation_charts, market_valuation_reference_charts
from investment_panel.core.panel.feed import feed_signals, ownership_consensus, source_consensus, universe_screen
from investment_panel.core.panel.read_equity import analyst_estimates, candidates, catalysts, correlations, decision_queue, decision_readiness, discovered_universe, earnings, earnings_setups, etf_premiums, fundamentals, liquidity, news, opportunities_ranked, opportunity_sources, portfolio, provider_runs, quotes, reports, research_packets, screener, sepa, source_freshness, source_health, symbol_decision_snapshots, theses, trader_profiles, tradingview_alerts, tradingview_chart_state, tradingview_symbol_search, tradingview_watchlists, valuations
from investment_panel.core.panel.read_options import option_features, option_radar_opportunity, option_radar_summary, option_snapshot, option_strategy_versions, options_chain, options_expiries, options_expiry_signals, options_payoff_scenarios, options_provider_capabilities, options_ticker_signals, stock_features
from investment_panel.core.panel.read_learning import agent_postmortem, agent_postmortem_request, agent_thesis, agent_thesis_request, agent_thesis_validation, candidate_event, candidate_event_attribution, candidate_event_mark, conviction_calibration, missed_winner_event, option_attribution, radar_alert, radar_state_transition, shadow_trade, shadow_trade_mark, strategy_backtest_result, strategy_cohort_result, strategy_forward_test_result, strategy_mutation_proposal, trade_journal, vol_surface_features



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
            "radar_alert": lambda: radar_alert(con),
            "candidate_event": lambda: candidate_event(con),
            "candidate_event_mark": lambda: candidate_event_mark(con),
            "candidate_event_attribution": lambda: candidate_event_attribution(con),
            "conviction_calibration": lambda: conviction_calibration(con),
            "vol_surface_features": lambda: vol_surface_features(con),
            "trade_journal": lambda: trade_journal(con),
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
            "fundamentals": fundamentals(con, symbols=[symbol]),
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
            "analyst_estimates": analyst_estimates(con, symbols=[symbol]),
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
