"""API payload builders for panel views."""

from __future__ import annotations
import os
from typing import Any, Iterable
from app.panel_contracts import (
    DECISION_REPAIR_TABLES,
    SOURCE_REPAIR_TABLES,
    TICKER_TABLES,
    panel_contract_payload as contract_panel_payload,
    tables_for_scope as contract_tables_for_scope,
)
from investment_panel.core.option_agent_thesis import DEFAULT_AGENT_THESIS_REQUEST_LIMIT

from app.data_access.types import PanelData
from app.data_access.config import tables_for_scope
from app.data_access.coerce import _int_value, _matching_ticker_rows, _row_symbols, jsonable
from app.data_access.ticker_dossier import _ensure_ticker_dossier_tables
from app.data_access.decision_brief import ticker_decision_brief



def status_payload(panel_data: PanelData) -> dict[str, Any]:
    return {
        "ready": panel_data.status.ready,
        "message": panel_data.status.message,
        "source": panel_data.status.source,
        "metadata": jsonable(panel_data.metadata),
    }




def _runtime_metadata(config: dict[str, Any]) -> dict[str, Any]:
    agents = config.get("agents", {}) if isinstance(config.get("agents"), dict) else {}
    option_thesis = agents.get("option_thesis", {}) if isinstance(agents.get("option_thesis"), dict) else {}
    option_postmortem = agents.get("option_postmortem", {}) if isinstance(agents.get("option_postmortem"), dict) else {}
    return {
        "agents": {
            "option_thesis": _agent_runtime_metadata(option_thesis, default_limit=20) | {
                "request_cap": DEFAULT_AGENT_THESIS_REQUEST_LIMIT,
                "queue_policy": "current_top_ranked_candidates_only",
                "cadence": "daily_premarket",
                "max_runs_per_day": 1,
            },
            "option_postmortem": _agent_runtime_metadata(option_postmortem, default_limit=20) | {
                "cadence": "daily_premarket",
                "max_runs_per_day": 1,
            },
        },
        "options_radar": {
            "deterministic_cadence": "hourly",
            "agent_cadence": "daily_premarket",
        },
        "scheduler": {
            "agent_refresh_seconds": os.environ.get("MARKET_AGENT_REFRESH_SECONDS", "0"),
            "radar_refresh_seconds": os.environ.get("MARKET_RADAR_REFRESH_SECONDS", "900"),
            "source_refresh_seconds": os.environ.get("MARKET_SOURCE_REFRESH_SECONDS", "3600"),
            "learning_refresh_seconds": os.environ.get("MARKET_LEARNING_REFRESH_SECONDS", "21600"),
            "radar_option_source": os.environ.get("MARKET_RADAR_OPTION_SOURCE", "ibkr"),
        },
    }




def _agent_runtime_metadata(config: dict[str, Any], *, default_limit: int) -> dict[str, Any]:
    command = str(config.get("command") or "")
    enabled = bool(config.get("enabled", bool(command)))
    configured = bool(command.strip())
    return {
        "enabled": enabled,
        "configured": configured,
        "active": enabled and configured,
        "status": "active" if enabled and configured else "paused",
        "limit": _int_value(config.get("limit"), default_limit),
        "timeout_seconds": _int_value(config.get("timeout_seconds"), 120),
    }




def table_payload(panel_data: PanelData, table_name: str) -> dict[str, Any]:
    rows = panel_data.rows(table_name)
    return {"rows": rows, "count": len(rows), "status": status_payload(panel_data)}




def signals_payload(panel_data: PanelData) -> dict[str, Any]:
    rows = panel_data.rows("signals") or panel_data.rows("candidates")
    return {"rows": rows, "count": len(rows), "status": status_payload(panel_data)}




def dashboard_payload(panel_data: PanelData) -> dict[str, Any]:
    decision_queue = panel_data.rows("decision_queue")
    decision_readiness = panel_data.rows("decision_readiness")
    discovered_universe = panel_data.rows("discovered_universe")
    source_freshness = panel_data.rows("source_freshness")
    candidates = panel_data.rows("candidates")
    portfolio = panel_data.rows("portfolio")
    theses = panel_data.rows("theses")
    thesis_monitor = panel_data.rows("thesis_monitor")
    catalysts = panel_data.rows("catalysts")
    fundamentals = panel_data.rows("fundamentals")
    disclosures = panel_data.rows("disclosures")
    quotes = panel_data.rows("quotes")
    news = panel_data.rows("news")
    sepa = panel_data.rows("sepa")
    liquidity = panel_data.rows("liquidity")
    earnings = panel_data.rows("earnings")
    earnings_setups = panel_data.rows("earnings_setups")
    valuations = panel_data.rows("valuations")
    option_payoffs = panel_data.rows("options_payoff_scenarios")
    option_signals = panel_data.rows("options_ticker_signals")
    option_candidates = panel_data.rows("candidate_event")
    candidate_event_marks = panel_data.rows("candidate_event_mark")
    candidate_event_attributions = panel_data.rows("candidate_event_attribution")
    shadow_trades = panel_data.rows("shadow_trade")
    shadow_trade_marks = panel_data.rows("shadow_trade_mark")
    radar_state_transitions = panel_data.rows("radar_state_transition")
    option_attributions = panel_data.rows("option_attribution")
    missed_winners = panel_data.rows("missed_winner_event")
    strategy_proposals = panel_data.rows("strategy_mutation_proposal")
    strategy_backtests = panel_data.rows("strategy_backtest_result")
    strategy_forward_tests = panel_data.rows("strategy_forward_test_result")
    strategy_cohorts = panel_data.rows("strategy_cohort_result")
    agent_thesis_requests = panel_data.rows("agent_thesis_request")
    agent_thesis_validations = panel_data.rows("agent_thesis_validation")
    agent_postmortem_requests = panel_data.rows("agent_postmortem_request")
    agent_postmortems = panel_data.rows("agent_postmortem")
    source_health = panel_data.rows("source_health")
    sources = panel_data.rows("sources")
    source_runs = panel_data.rows("source_runs")
    source_items = panel_data.rows("source_items")
    ticker_source_signals = panel_data.rows("ticker_source_signals")
    broker_status = panel_data.rows("broker_status")
    agent_recommendations = panel_data.rows("agent_recommendations")
    daily_brief = panel_data.rows("daily_brief")
    feed_signals = panel_data.rows("feed_signals")
    universe_screen = panel_data.rows("universe_screen")
    source_consensus = panel_data.rows("source_consensus")
    ownership_consensus = panel_data.rows("ownership_consensus")
    market_context = panel_data.rows("market_context")
    market_valuation_reference_charts = panel_data.rows("market_valuation_reference_charts")
    market_valuation_charts = panel_data.rows("market_valuation_charts")
    market_environment_assets = panel_data.rows("market_environment_assets")
    market_environment_model = panel_data.rows("market_environment_model")
    portfolio_risk_cards = panel_data.rows("portfolio_risk_cards")
    review_actions = panel_data.rows("review_actions")
    priority_rows = decision_queue or candidates
    return {
        "status": status_payload(panel_data),
        "metrics": {
            "decision_queue": len(decision_queue),
            "discovered_universe": len(discovered_universe),
            "candidates": len(candidates),
            "holdings": len(portfolio),
            "theses": len(theses),
            "thesis_monitor": len(thesis_monitor),
            "catalysts": len(catalysts),
            "fundamentals": len(fundamentals),
            "disclosures": len(disclosures),
            "quotes": len(quotes),
            "news": len(news),
            "sepa": len(sepa),
            "liquidity": len(liquidity),
            "earnings": len(earnings),
            "earnings_setups": len(earnings_setups),
            "valuations": len(valuations),
            "options_payoff_scenarios": len(option_payoffs),
            "options_ticker_signals": len(option_signals),
            "option_radar_candidates": len(option_candidates),
            "candidate_event_marks": len(candidate_event_marks),
            "candidate_event_attributions": len(candidate_event_attributions),
            "shadow_trades": len(shadow_trades),
            "shadow_trade_marks": len(shadow_trade_marks),
            "radar_state_transitions": len(radar_state_transitions),
            "option_attributions": len(option_attributions),
            "missed_winners": len(missed_winners),
            "strategy_mutation_proposals": len(strategy_proposals),
            "strategy_backtests": len(strategy_backtests),
            "strategy_forward_tests": len(strategy_forward_tests),
            "strategy_cohorts": len(strategy_cohorts),
            "agent_thesis_requests": len(agent_thesis_requests),
            "agent_thesis_validations": len(agent_thesis_validations),
            "agent_postmortem_requests": len(agent_postmortem_requests),
            "agent_postmortems": len(agent_postmortems),
            "sources": len(sources) or len(source_freshness) or len(source_health),
            "source_runs": len(source_runs),
            "source_items": len(source_items),
            "ticker_source_signals": len(ticker_source_signals),
            "broker_providers": len(broker_status),
            "agent_recommendations": len(agent_recommendations),
            "daily_brief": len(daily_brief),
            "feed_signals": len(feed_signals),
            "universe_screen": len(universe_screen),
            "source_consensus": len(source_consensus),
            "ownership_consensus": len(ownership_consensus),
            "market_context": len(market_context),
            "market_valuation_reference_charts": len(market_valuation_reference_charts),
            "market_valuation_charts": len(market_valuation_charts),
            "market_environment_assets": len(market_environment_assets),
            "market_environment_model": len(market_environment_model),
            "portfolio_risk_cards": len(portfolio_risk_cards),
            "review_actions": len(review_actions),
        },
        "decision_queue": decision_queue[:12],
        "decision_readiness": decision_readiness[:12],
        "priority_candidates": priority_rows[:8],
        "near_term_catalysts": catalysts[:8],
        "portfolio": portfolio[:8],
        "thesis_monitor": thesis_monitor[:8],
        "source_freshness": source_freshness[:12],
        "source_health": source_health[:8],
        "sources": sources[:12],
        "source_runs": source_runs[:12],
        "source_items": source_items[:12],
        "ticker_source_signals": ticker_source_signals[:12],
        "broker_status": broker_status[:8],
        "agent_recommendations": agent_recommendations[:8],
        "daily_brief": daily_brief[:12],
        "feed_signals": feed_signals[:12],
        "universe_screen": universe_screen[:12],
        "source_consensus": source_consensus[:12],
        "ownership_consensus": ownership_consensus[:12],
        "market_context": market_context[:12],
        "market_valuation_reference_charts": market_valuation_reference_charts[:8],
        "market_valuation_charts": market_valuation_charts[:24],
        "market_environment_assets": market_environment_assets[:80],
        "market_environment_model": market_environment_model[:12],
        "portfolio_risk_cards": portfolio_risk_cards[:8],
        "review_actions": review_actions[:8],
        "option_radar_candidates": option_candidates[:12],
        "candidate_event_marks": candidate_event_marks[:12],
        "candidate_event_attributions": candidate_event_attributions[:12],
        "shadow_trades": shadow_trades[:12],
        "shadow_trade_marks": shadow_trade_marks[:12],
        "radar_state_transitions": radar_state_transitions[:12],
        "option_attributions": option_attributions[:12],
        "missed_winners": missed_winners[:12],
        "strategy_mutation_proposals": strategy_proposals[:12],
        "strategy_backtests": strategy_backtests[:12],
        "strategy_forward_tests": strategy_forward_tests[:12],
        "strategy_cohorts": strategy_cohorts[:12],
        "agent_thesis_requests": agent_thesis_requests[:12],
        "agent_thesis_validations": agent_thesis_validations[:12],
        "agent_postmortem_requests": agent_postmortem_requests[:12],
        "agent_postmortems": agent_postmortems[:12],
        "disclosures": disclosures[:8],
        "news": news[:8],
    }




def panel_snapshot_payload(panel_data: PanelData, scope: str, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
    if scope in {"watchlist-watched", "watchlist-unwatched"}:
        return watchlist_section_payload(panel_data, scope, offset=offset, limit=limit)

    selected = tables_for_scope(scope)
    return {
        "scope": scope,
        "status": status_payload(panel_data),
        "dashboard": dashboard_payload(panel_data) if scope == "dashboard" else None,
        "tables": {name: {"rows": panel_data.rows(name), "count": len(panel_data.rows(name))} for name in selected},
    }




def watchlist_section_payload(panel_data: PanelData, scope: str, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
    watched = scope == "watchlist-watched"
    prefix = "watchlist_watched" if watched else "watchlist_unwatched"
    sanitized_offset = max(0, int(offset or 0))
    sanitized_limit = max(1, int(limit)) if limit is not None else None
    universe_rows = [row for row in _watchlist_universe_rows(panel_data) if _is_active_watchlist_row(row) == watched]
    total_count = len(universe_rows)
    page_rows = universe_rows[sanitized_offset : sanitized_offset + sanitized_limit] if sanitized_limit is not None else universe_rows
    symbols = {str(row.get("symbol") or row.get("ticker") or "").upper() for row in page_rows if row.get("symbol") or row.get("ticker")}
    table_rows = {
        prefix: page_rows,
        f"{prefix}_quotes": _rows_for_symbols(panel_data.rows("quotes"), symbols),
        f"{prefix}_fundamentals": _rows_for_symbols(panel_data.rows("fundamentals"), symbols),
        f"{prefix}_technicals": _rows_for_symbols(panel_data.rows("technicals"), symbols),
        f"{prefix}_valuations": _rows_for_symbols(panel_data.rows("valuations"), symbols),
        f"{prefix}_screener": _rows_for_symbols(panel_data.rows("screener"), symbols),
        f"{prefix}_decision_queue": _rows_for_symbols(panel_data.rows("decision_queue"), symbols),
        f"{prefix}_portfolio": _rows_for_symbols(panel_data.rows("portfolio"), symbols),
        f"{prefix}_options": _rows_for_symbols(panel_data.rows("options_ticker_signals"), symbols),
    }
    table_counts = {name: len(rows) for name, rows in table_rows.items()}
    table_counts[prefix] = total_count
    if watched:
        unwatched_count = len([row for row in _watchlist_universe_rows(panel_data) if not _is_active_watchlist_row(row)])
        table_rows["watchlist_unwatched"] = []
        table_counts["watchlist_unwatched"] = unwatched_count
    return {
        "scope": scope,
        "status": status_payload(panel_data),
        "dashboard": None,
        "tables": {
            name: {
                "rows": rows,
                "count": table_counts[name],
                "offset": sanitized_offset if name == prefix else 0,
                "limit": sanitized_limit,
            }
            for name, rows in table_rows.items()
        },
    }




def _watchlist_universe_rows(panel_data: PanelData) -> list[dict[str, Any]]:
    manual_by_symbol = _manual_watchlist_by_symbol(panel_data)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_row in panel_data.rows("universe_screen"):
        symbol = _primary_symbol(raw_row)
        if not symbol:
            continue
        seen.add(symbol)
        manual = manual_by_symbol.get(symbol)
        watch_state = str((manual or {}).get("watch_state") or raw_row.get("watch_state") or "").lower()
        if watch_state == "excluded":
            continue
        row = dict(raw_row)
        if manual:
            row["watch_state"] = watch_state or "watched"
            row["name"] = manual.get("name") or row.get("name") or symbol
            row["asset_class"] = manual.get("asset_class") or row.get("asset_class")
        rows.append(row)

    for symbol, manual in manual_by_symbol.items():
        if symbol in seen:
            continue
        watch_state = str(manual.get("watch_state") or "watched").lower()
        if watch_state == "excluded":
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": manual.get("name") or symbol,
                "asset_class": manual.get("asset_class") or ("crypto" if symbol.endswith("-USD") else "equity"),
                "watch_state": "watched",
                "source_count": 0,
                "rating": "-",
                "quality_score": None,
                "value_signal": "manual",
                "action": "Watch",
                "next_action": "New manual watchlist symbol. Run market refresh for full valuation and momentum context.",
                "freshness": "manual",
            }
        )
    return rows




def _manual_watchlist_by_symbol(panel_data: PanelData) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in panel_data.rows("manual_watchlist"):
        symbol = _primary_symbol(row)
        if symbol:
            rows[symbol] = row
    return rows




def _is_active_watchlist_row(row: dict[str, Any]) -> bool:
    return str(row.get("watch_state") or "").lower() in {"owned", "watched"}




def _primary_symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("ticker") or "").upper()




def _rows_for_symbols(rows: list[dict[str, Any]], symbols: set[str]) -> list[dict[str, Any]]:
    if not symbols:
        return []
    return [row for row in rows if _row_symbols(row) & symbols]




def _filter_ticker_panel_data(panel_data: PanelData, ticker: str) -> PanelData:
    normalized = ticker.upper()
    filtered_tables: dict[str, Any] = {}
    for table_name in TICKER_TABLES:
        rows = panel_data.rows(table_name)
        if table_name == "options_provider_capabilities":
            filtered_tables[table_name] = rows
        elif table_name == "correlation_edges":
            filtered_tables[table_name] = [
                row
                for row in rows
                if normalized in {str(row.get("symbol") or "").upper(), str(row.get("peer_symbol") or "").upper()}
            ]
        else:
            filtered_tables[table_name] = _matching_ticker_rows(rows, normalized)
    return PanelData(status=panel_data.status, tables=filtered_tables, metadata=panel_data.metadata)




def ticker_payload(panel_data: PanelData, ticker: str) -> dict[str, Any]:
    normalized_ticker = ticker.upper()
    tables = {
        "candidates": _matching_ticker_rows(panel_data.rows("candidates"), normalized_ticker),
        "decision_queue": _matching_ticker_rows(panel_data.rows("decision_queue"), normalized_ticker),
        "discovered_universe": _matching_ticker_rows(panel_data.rows("discovered_universe"), normalized_ticker),
        "universe_screen": _matching_ticker_rows(panel_data.rows("universe_screen"), normalized_ticker),
        "symbol_decision_snapshots": _matching_ticker_rows(panel_data.rows("symbol_decision_snapshots"), normalized_ticker),
        "symbol_decision_snapshot": _matching_ticker_rows(panel_data.rows("symbol_decision_snapshot"), normalized_ticker),
        "opportunities_ranked": _matching_ticker_rows(panel_data.rows("opportunities_ranked"), normalized_ticker),
        "opportunity_sources": _matching_ticker_rows(panel_data.rows("opportunity_sources"), normalized_ticker),
        "feed_signals": _matching_ticker_rows(panel_data.rows("feed_signals"), normalized_ticker),
        "source_consensus": _matching_ticker_rows(panel_data.rows("source_consensus"), normalized_ticker),
        "ticker_source_signals": _matching_ticker_rows(panel_data.rows("ticker_source_signals"), normalized_ticker),
        "ownership_consensus": _matching_ticker_rows(panel_data.rows("ownership_consensus"), normalized_ticker),
        "portfolio": _matching_ticker_rows(panel_data.rows("portfolio"), normalized_ticker),
        "theses": _matching_ticker_rows(panel_data.rows("theses"), normalized_ticker),
        "thesis_monitor": _matching_ticker_rows(panel_data.rows("thesis_monitor"), normalized_ticker),
        "catalysts": _matching_ticker_rows(panel_data.rows("catalysts"), normalized_ticker),
        "signals": _matching_ticker_rows(panel_data.rows("signals"), normalized_ticker),
        "fundamentals": _matching_ticker_rows(panel_data.rows("fundamentals"), normalized_ticker),
        "disclosures": _matching_ticker_rows(panel_data.rows("disclosures"), normalized_ticker),
        "quotes": _matching_ticker_rows(panel_data.rows("quotes"), normalized_ticker),
        "options_expiries": _matching_ticker_rows(panel_data.rows("options_expiries"), normalized_ticker),
        "options_chain": _matching_ticker_rows(panel_data.rows("options_chain"), normalized_ticker),
        "options_payoff_scenarios": _matching_ticker_rows(panel_data.rows("options_payoff_scenarios"), normalized_ticker),
        "options_provider_capabilities": panel_data.rows("options_provider_capabilities"),
        "options_expiry_signals": _matching_ticker_rows(panel_data.rows("options_expiry_signals"), normalized_ticker),
        "options_ticker_signals": _matching_ticker_rows(panel_data.rows("options_ticker_signals"), normalized_ticker),
        "news": _matching_ticker_rows(panel_data.rows("news"), normalized_ticker),
        "tradingview_symbol_search": _matching_ticker_rows(panel_data.rows("tradingview_symbol_search"), normalized_ticker),
        "tradingview_watchlists": _matching_ticker_rows(panel_data.rows("tradingview_watchlists"), normalized_ticker),
        "tradingview_alerts": _matching_ticker_rows(panel_data.rows("tradingview_alerts"), normalized_ticker),
        "tradingview_chart_state": _matching_ticker_rows(panel_data.rows("tradingview_chart_state"), normalized_ticker),
        "sepa": _matching_ticker_rows(panel_data.rows("sepa"), normalized_ticker),
        "liquidity": _matching_ticker_rows(panel_data.rows("liquidity"), normalized_ticker),
        "correlations": _matching_ticker_rows(panel_data.rows("correlations"), normalized_ticker),
        "etf_premiums": _matching_ticker_rows(panel_data.rows("etf_premiums"), normalized_ticker),
        "analyst_estimates": _matching_ticker_rows(panel_data.rows("analyst_estimates"), normalized_ticker),
        "earnings": _matching_ticker_rows(panel_data.rows("earnings"), normalized_ticker),
        "earnings_setups": _matching_ticker_rows(panel_data.rows("earnings_setups"), normalized_ticker),
        "valuations": _matching_ticker_rows(panel_data.rows("valuations"), normalized_ticker),
        "technicals": _matching_ticker_rows(panel_data.rows("technicals"), normalized_ticker),
        "research_packets": _matching_ticker_rows(panel_data.rows("research_packets"), normalized_ticker),
        "exposure_clusters": [
            row
            for row in panel_data.rows("exposure_clusters")
            if normalized_ticker in _row_symbols(row)
        ],
        "correlation_edges": [
            row
            for row in panel_data.rows("correlation_edges")
            if normalized_ticker in {str(row.get("symbol") or "").upper(), str(row.get("peer_symbol") or "").upper()}
        ],
        "portfolio_risk_cards": [
            row
            for row in panel_data.rows("portfolio_risk_cards")
            if normalized_ticker in _row_symbols(row)
        ],
        "review_actions": [
            row
            for row in panel_data.rows("review_actions")
            if normalized_ticker in _row_symbols(row)
        ],
        "memos": _matching_ticker_rows(
            panel_data.rows("ticker_memos") or panel_data.rows("memos"),
            normalized_ticker,
        ),
    }
    _ensure_ticker_dossier_tables(normalized_ticker, tables)
    return {
        "ticker": normalized_ticker,
        "status": status_payload(panel_data),
        "tables": tables,
        "decision_snapshot": (tables["symbol_decision_snapshot"] or tables["symbol_decision_snapshots"] or [None])[0],
        "decision_brief": ticker_decision_brief(normalized_ticker, tables),
        "found": any(tables.values()),
    }
