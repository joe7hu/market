"""Ticker dossier read-model composition."""

from __future__ import annotations

from typing import Any, Callable

from investment_panel.core.portfolio_intelligence import correlation_edges, exposure_clusters, portfolio_risk_cards, review_actions
from investment_panel.core.signals import signal_rows
from investment_panel.core.sources import ticker_source_signal_rows
from investment_panel.core.thesis_monitor import thesis_monitor_rows

from investment_panel.core.panel.coerce import _normalize_symbol_token, _symbols_from_value
from investment_panel.core.panel.disclosures import disclosures
from investment_panel.core.panel.feed import feed_signals, ownership_consensus, source_consensus, universe_screen
from investment_panel.core.panel.read_equity import candidates, catalysts, decision_queue, discovered_universe, opportunities_ranked, opportunity_sources, portfolio, symbol_decision_snapshots, theses
from investment_panel.core.panel.read_market_data import analyst_estimates, correlations, earnings, earnings_setups, etf_premiums, fundamentals, liquidity, news, quotes, sepa, valuations
from investment_panel.core.panel.read_options import options_chain, options_expiries, options_expiry_signals, options_payoff_scenarios, options_provider_capabilities, options_ticker_signals
from investment_panel.core.panel.read_research import reports, research_packets
from investment_panel.core.panel.read_tradingview import instrument_market_identity, tradingview_alerts, tradingview_chart_state, tradingview_symbol_search, tradingview_watchlists
from investment_panel.core.panel.technicals import technicals


TickerLoader = Callable[[Any, list[dict[str, Any]], str], list[dict[str, Any]]]


def _filtered(loader: Callable[..., list[dict[str, Any]]]) -> TickerLoader:
    return lambda con, _watchlist, symbol: rows_matching_symbol(loader(con), symbol)


def _watchlist_filtered(loader: Callable[[Any, list[dict[str, Any]]], list[dict[str, Any]]]) -> TickerLoader:
    return lambda con, watchlist, symbol: rows_matching_symbol(loader(con, watchlist), symbol)


TICKER_DOSSIER_LOADERS: dict[str, TickerLoader] = {
    "candidates": _filtered(candidates),
    "decision_queue": _filtered(decision_queue),
    "discovered_universe": _filtered(discovered_universe),
    "universe_screen": _watchlist_filtered(universe_screen),
    "symbol_decision_snapshot": _filtered(symbol_decision_snapshots),
    "symbol_decision_snapshots": _filtered(symbol_decision_snapshots),
    "opportunities_ranked": _filtered(opportunities_ranked),
    "opportunity_sources": _filtered(opportunity_sources),
    "feed_signals": _watchlist_filtered(feed_signals),
    "source_consensus": _filtered(source_consensus),
    "ticker_source_signals": lambda con, _watchlist, symbol: ticker_source_signal_rows(con, symbol=symbol),
    "ownership_consensus": _filtered(ownership_consensus),
    "portfolio": _filtered(portfolio),
    "theses": _filtered(theses),
    "thesis_monitor": lambda con, watchlist, symbol: rows_matching_symbol(thesis_monitor_rows(con, watchlist), symbol),
    "catalysts": _filtered(catalysts),
    "signals": _filtered(signal_rows),
    "fundamentals": lambda con, _watchlist, symbol: fundamentals(con, symbols=[symbol]),
    "disclosures": _filtered(disclosures),
    "quotes": _filtered(quotes),
    "options_expiries": _filtered(options_expiries),
    "options_chain": _filtered(options_chain),
    "options_payoff_scenarios": _filtered(options_payoff_scenarios),
    "options_provider_capabilities": lambda con, _watchlist, _symbol: options_provider_capabilities(con),
    "options_expiry_signals": _filtered(options_expiry_signals),
    "options_ticker_signals": _filtered(options_ticker_signals),
    "news": _filtered(news),
    "instrument_market_identity": _filtered(instrument_market_identity),
    "tradingview_symbol_search": _filtered(tradingview_symbol_search),
    "tradingview_watchlists": _filtered(tradingview_watchlists),
    "tradingview_alerts": _filtered(tradingview_alerts),
    "tradingview_chart_state": _filtered(tradingview_chart_state),
    "sepa": _filtered(sepa),
    "liquidity": _filtered(liquidity),
    "correlations": _filtered(correlations),
    "etf_premiums": _filtered(etf_premiums),
    "analyst_estimates": lambda con, _watchlist, symbol: analyst_estimates(con, symbols=[symbol]),
    "earnings": _filtered(earnings),
    "earnings_setups": _filtered(earnings_setups),
    "valuations": lambda con, _watchlist, symbol: valuations(con, symbols=[symbol]),
    "technicals": lambda con, _watchlist, symbol: technicals(con, symbols=[symbol]),
    "research_packets": _filtered(research_packets),
    "exposure_clusters": _filtered(exposure_clusters),
    "correlation_edges": _filtered(correlation_edges),
    "portfolio_risk_cards": _filtered(portfolio_risk_cards),
    "review_actions": _filtered(review_actions),
    "ticker_memos": _filtered(reports),
}


def load_ticker_dossier_tables(con: Any, active_watchlist: list[dict[str, Any]], symbol: str) -> dict[str, list[dict[str, Any]]]:
    """Load every ticker dossier table using the table's symbol-aware adapter."""

    normalized = _normalize_symbol_token(symbol)
    return {
        name: loader(con, active_watchlist, normalized)
        for name, loader in TICKER_DOSSIER_LOADERS.items()
    }


def ticker_payload_tables(rows_for_table: Callable[[str], list[dict[str, Any]]], symbol: str) -> dict[str, list[dict[str, Any]]]:
    """Build the ticker payload table map from already-loaded read models.

    This is the API/test path equivalent of :func:`load_ticker_dossier_tables`.
    It keeps ticker table names and symbol-matching semantics with the core
    ticker dossier module instead of restating them in the FastAPI payload layer.
    """

    normalized = _normalize_symbol_token(symbol)
    tables: dict[str, list[dict[str, Any]]] = {}
    for table_name in TICKER_DOSSIER_LOADERS:
        source_name = "ticker_memos" if table_name == "ticker_memos" else table_name
        if table_name == "options_provider_capabilities":
            rows = rows_for_table(source_name)
        else:
            rows = rows_matching_symbol(rows_for_table(source_name), normalized)
        tables["memos" if table_name == "ticker_memos" else table_name] = rows
    return tables


def rows_matching_symbol(rows: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    normalized = _normalize_symbol_token(symbol)
    return [row for row in rows if row_matches_symbol(row, normalized)]


def row_matches_symbol(row: dict[str, Any], symbol: str) -> bool:
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
