"""Top-level panel orchestration and dispatch.

The full-panel read uses the read-model :mod:`registry` for its name->loader
dispatch; the per-symbol ticker dossier composes accessors directly because it
filters and pushes the symbol down. Both choose their DuckDB connection mode by
need: a pure read takes a read-only connection, and only the ensure/refresh seam
opens read-write.
"""

from __future__ import annotations
from pathlib import Path
from threading import Lock
from typing import Any
from investment_panel.core.config import AppConfig, config_to_dict, load_config
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.decision import effective_watchlist, refresh_decision_read_models
from investment_panel.core.portfolio_intelligence import correlation_edges, exposure_clusters, portfolio_risk_cards, review_actions
from investment_panel.core.signals import signal_rows
from investment_panel.core.sources import ensure_canonical_sources, ticker_source_signal_rows
from investment_panel.core.thesis_monitor import thesis_monitor_rows

from investment_panel.core.panel.coerce import _normalize_symbol_token, _symbols_from_value
from investment_panel.core.panel.registry import ReadContext, load_read_models
from investment_panel.core.panel.technicals import technicals
from investment_panel.core.panel.disclosures import disclosures
from investment_panel.core.panel.feed import feed_signals, ownership_consensus, source_consensus, universe_screen
from investment_panel.core.panel.read_equity import analyst_estimates, candidates, catalysts, correlations, decision_queue, discovered_universe, earnings, earnings_setups, etf_premiums, fundamentals, liquidity, news, opportunities_ranked, opportunity_sources, portfolio, quotes, reports, research_packets, sepa, symbol_decision_snapshots, theses, tradingview_alerts, tradingview_chart_state, tradingview_symbol_search, tradingview_watchlists, valuations
from investment_panel.core.panel.read_options import options_chain, options_expiries, options_expiry_signals, options_payoff_scenarios, options_provider_capabilities, options_ticker_signals



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


def _resolve_config(config: dict[str, Any] | AppConfig | None) -> tuple[AppConfig, Path, list[dict[str, Any]]]:
    """Normalize the caller's config into ``(app_config, db_path, watchlist)``.

    The API passes a plain dict (FastAPI compatibility path); every other caller
    passes an :class:`AppConfig`. This is the single place that branch lives.
    """

    app_config = config if isinstance(config, AppConfig) else load_config()
    if isinstance(config, dict):
        db_path = Path(config.get("database", {}).get("duckdb_path", "data/investment.duckdb"))
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        config_watchlist = list(config.get("watchlist", []))
    else:
        db_path = app_config.database.duckdb_path
        config_watchlist = app_config.watchlist
    return app_config, db_path, config_watchlist


def _ensure_or_probe_read_models(
    con: Any,
    active_watchlist: list[dict[str, Any]],
    requested_tables: set[str],
    should_ensure_sources: bool,
    should_ensure_decision: bool,
) -> dict[str, int | str | list[str]]:
    """Run the write-side refresh seam, or a read-only readiness probe.

    This is the only place the panel read path writes. When neither ensure flag
    is set the connection is read-only and this just reports row counts.
    """

    if should_ensure_sources:
        ensure_canonical_sources(con)
    if should_ensure_decision:
        return ensure_decision_read_models(con, active_watchlist)
    return decision_readiness_snapshot(con, requested_tables)


def load_panel_data(
    config: dict[str, Any] | AppConfig | None = None,
    table_names: list[str] | set[str] | tuple[str, ...] | None = None,
    ensure_decision_models: bool | None = None,
    ensure_source_models: bool | None = None,
) -> dict[str, Any]:
    app_config, db_path, config_watchlist = _resolve_config(config)
    init_db(db_path)
    requested_tables = set(table_names or [])
    should_ensure_decision = (not requested_tables) if ensure_decision_models is None else ensure_decision_models
    should_ensure_sources = should_ensure_decision or ((not requested_tables) if ensure_source_models is None else ensure_source_models)
    # Pure reads take a read-only connection so they neither acquire the DuckDB
    # writer lock nor serialize behind other readers; only the ensure/refresh
    # seam needs read-write.
    needs_write = should_ensure_sources or should_ensure_decision
    with db(db_path, read_only=not needs_write) as con:
        active_watchlist = effective_watchlist(con, config_watchlist)
        decision_refresh = _ensure_or_probe_read_models(
            con,
            active_watchlist,
            requested_tables,
            should_ensure_sources,
            should_ensure_decision,
        )
        decision_snapshots = symbol_decision_snapshots(con) if not requested_tables or requested_tables & DECISION_READ_MODEL_TABLES else []
        ctx = ReadContext(
            con=con,
            active_watchlist=active_watchlist,
            app_config=app_config,
            decision_snapshots=decision_snapshots,
        )
        tables = load_read_models(ctx, requested_tables or None)
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
    app_config, db_path, config_watchlist = _resolve_config(config)
    symbol = str(ticker or "").upper().strip()
    init_db(db_path)
    with db(db_path, read_only=not ensure_decision_models) as con:
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
