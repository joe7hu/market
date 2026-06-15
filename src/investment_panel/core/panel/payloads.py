"""Backend-owned panel payload assembly.

FastAPI wraps core read-model output in ``PanelData`` for app concerns, but the
shape of panel scopes and watchlist section payloads belongs with the core panel
contract. Callers supply row/status accessors; this module owns table selection,
counts, dashboard slices, and watched/unwatched section derivation.
"""

from __future__ import annotations

from typing import Any, Callable

from investment_panel.core.panel.contracts import tables_for_scope
from investment_panel.core.panel.coerce import _symbols_from_value


RowsForTable = Callable[[str], list[dict[str, Any]]]


DASHBOARD_ROW_KEYS = (
    ("decision_queue", "decision_queue", 12),
    ("decision_readiness", "decision_readiness", 12),
    ("near_term_catalysts", "catalysts", 8),
    ("portfolio", "portfolio", 8),
    ("thesis_monitor", "thesis_monitor", 8),
    ("source_freshness", "source_freshness", 12),
    ("source_health", "source_health", 8),
    ("sources", "sources", 12),
    ("source_runs", "source_runs", 12),
    ("source_items", "source_items", 12),
    ("ticker_source_signals", "ticker_source_signals", 12),
    ("broker_status", "broker_status", 8),
    ("agent_recommendations", "agent_recommendations", 8),
    ("daily_brief", "daily_brief", 12),
    ("feed_signals", "feed_signals", 12),
    ("universe_screen", "universe_screen", 12),
    ("source_consensus", "source_consensus", 12),
    ("ownership_consensus", "ownership_consensus", 12),
    ("market_context", "market_context", 12),
    ("market_valuation_reference_charts", "market_valuation_reference_charts", 8),
    ("market_valuation_charts", "market_valuation_charts", 24),
    ("market_environment_assets", "market_environment_assets", 80),
    ("market_environment_model", "market_environment_model", 12),
    ("portfolio_risk_cards", "portfolio_risk_cards", 8),
    ("review_actions", "review_actions", 8),
    ("option_radar_candidates", "candidate_event", 12),
    ("candidate_event_marks", "candidate_event_mark", 12),
    ("candidate_event_attributions", "candidate_event_attribution", 12),
    ("shadow_trades", "shadow_trade", 12),
    ("shadow_trade_marks", "shadow_trade_mark", 12),
    ("radar_state_transitions", "radar_state_transition", 12),
    ("option_attributions", "option_attribution", 12),
    ("missed_winners", "missed_winner_event", 12),
    ("strategy_mutation_proposals", "strategy_mutation_proposal", 12),
    ("strategy_backtests", "strategy_backtest_result", 12),
    ("strategy_forward_tests", "strategy_forward_test_result", 12),
    ("strategy_cohorts", "strategy_cohort_result", 12),
    ("agent_thesis_requests", "agent_thesis_request", 12),
    ("agent_thesis_validations", "agent_thesis_validation", 12),
    ("agent_postmortem_requests", "agent_postmortem_request", 12),
    ("agent_postmortems", "agent_postmortem", 12),
    ("disclosures", "disclosures", 8),
    ("news", "news", 8),
)

METRIC_TABLES = (
    ("decision_queue", "decision_queue"),
    ("discovered_universe", "discovered_universe"),
    ("candidates", "candidates"),
    ("holdings", "portfolio"),
    ("theses", "theses"),
    ("thesis_monitor", "thesis_monitor"),
    ("catalysts", "catalysts"),
    ("fundamentals", "fundamentals"),
    ("disclosures", "disclosures"),
    ("quotes", "quotes"),
    ("news", "news"),
    ("sepa", "sepa"),
    ("liquidity", "liquidity"),
    ("earnings", "earnings"),
    ("earnings_setups", "earnings_setups"),
    ("valuations", "valuations"),
    ("options_payoff_scenarios", "options_payoff_scenarios"),
    ("options_ticker_signals", "options_ticker_signals"),
    ("option_radar_candidates", "candidate_event"),
    ("candidate_event_marks", "candidate_event_mark"),
    ("candidate_event_attributions", "candidate_event_attribution"),
    ("shadow_trades", "shadow_trade"),
    ("shadow_trade_marks", "shadow_trade_mark"),
    ("radar_state_transitions", "radar_state_transition"),
    ("option_attributions", "option_attribution"),
    ("missed_winners", "missed_winner_event"),
    ("strategy_mutation_proposals", "strategy_mutation_proposal"),
    ("strategy_backtests", "strategy_backtest_result"),
    ("strategy_forward_tests", "strategy_forward_test_result"),
    ("strategy_cohorts", "strategy_cohort_result"),
    ("agent_thesis_requests", "agent_thesis_request"),
    ("agent_thesis_validations", "agent_thesis_validation"),
    ("agent_postmortem_requests", "agent_postmortem_request"),
    ("agent_postmortems", "agent_postmortem"),
    ("source_runs", "source_runs"),
    ("source_items", "source_items"),
    ("ticker_source_signals", "ticker_source_signals"),
    ("broker_providers", "broker_status"),
    ("agent_recommendations", "agent_recommendations"),
    ("daily_brief", "daily_brief"),
    ("feed_signals", "feed_signals"),
    ("universe_screen", "universe_screen"),
    ("source_consensus", "source_consensus"),
    ("ownership_consensus", "ownership_consensus"),
    ("market_context", "market_context"),
    ("market_valuation_reference_charts", "market_valuation_reference_charts"),
    ("market_valuation_charts", "market_valuation_charts"),
    ("market_environment_assets", "market_environment_assets"),
    ("market_environment_model", "market_environment_model"),
    ("portfolio_risk_cards", "portfolio_risk_cards"),
    ("review_actions", "review_actions"),
)


def dashboard_payload(status: dict[str, Any], rows_for_table: RowsForTable) -> dict[str, Any]:
    """Build the dashboard summary from backend read-model rows."""

    decision_queue = rows_for_table("decision_queue")
    candidates = rows_for_table("candidates")
    source_freshness = rows_for_table("source_freshness")
    source_health = rows_for_table("source_health")
    sources = rows_for_table("sources")

    payload: dict[str, Any] = {
        "status": status,
        "metrics": {
            key: len(rows_for_table(table_name))
            for key, table_name in METRIC_TABLES
        },
        "priority_candidates": (decision_queue or candidates)[:8],
    }
    payload["metrics"]["sources"] = len(sources) or len(source_freshness) or len(source_health)
    for output_key, table_name, limit in DASHBOARD_ROW_KEYS:
        payload[output_key] = rows_for_table(table_name)[:limit]
    return payload


def panel_snapshot_payload(
    *,
    scope: str,
    status: dict[str, Any],
    rows_for_table: RowsForTable,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """Build a scope payload using the canonical panel contract."""

    if scope in {"watchlist-watched", "watchlist-unwatched"}:
        return watchlist_section_payload(scope=scope, status=status, rows_for_table=rows_for_table, offset=offset, limit=limit)

    selected = tables_for_scope(scope)
    return {
        "scope": scope,
        "status": status,
        "dashboard": dashboard_payload(status, rows_for_table) if scope == "dashboard" else None,
        "tables": {name: _table_payload(rows_for_table(name)) for name in selected},
    }


def watchlist_section_payload(
    *,
    scope: str,
    status: dict[str, Any],
    rows_for_table: RowsForTable,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """Build watched/unwatched watchlist slices from the backend watchlist rows."""

    watched = scope == "watchlist-watched"
    prefix = "watchlist_watched" if watched else "watchlist_unwatched"
    sanitized_offset = max(0, int(offset or 0))
    sanitized_limit = max(1, int(limit)) if limit is not None else None
    universe_rows = [row for row in watchlist_universe_rows(rows_for_table) if _is_active_watchlist_row(row) == watched]
    total_count = len(universe_rows)
    page_rows = universe_rows[sanitized_offset : sanitized_offset + sanitized_limit] if sanitized_limit is not None else universe_rows
    symbols = {_primary_symbol(row) for row in page_rows if _primary_symbol(row)}
    table_rows = {
        prefix: page_rows,
        f"{prefix}_quotes": _rows_for_symbols(rows_for_table("quotes"), symbols),
        f"{prefix}_fundamentals": _rows_for_symbols(rows_for_table("fundamentals"), symbols),
        f"{prefix}_technicals": _rows_for_symbols(rows_for_table("technicals"), symbols),
        f"{prefix}_valuations": _rows_for_symbols(rows_for_table("valuations"), symbols),
        f"{prefix}_screener": _rows_for_symbols(rows_for_table("screener"), symbols),
        f"{prefix}_decision_queue": _rows_for_symbols(rows_for_table("decision_queue"), symbols),
        f"{prefix}_research_packets": _rows_for_symbols(rows_for_table("research_packets"), symbols),
        f"{prefix}_memos": _rows_for_symbols(rows_for_table("ticker_memos"), symbols),
        f"{prefix}_thesis_monitor": _rows_for_symbols(rows_for_table("thesis_monitor"), symbols),
        f"{prefix}_portfolio": _rows_for_symbols(rows_for_table("portfolio"), symbols),
        f"{prefix}_options": _rows_for_symbols(rows_for_table("options_ticker_signals"), symbols),
    }
    table_counts = {name: len(rows) for name, rows in table_rows.items()}
    table_counts[prefix] = total_count
    if watched:
        unwatched_count = len([row for row in watchlist_universe_rows(rows_for_table) if not _is_active_watchlist_row(row)])
        table_rows["watchlist_unwatched"] = []
        table_counts["watchlist_unwatched"] = unwatched_count
    return {
        "scope": scope,
        "status": status,
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


def watchlist_universe_rows(rows_for_table: RowsForTable) -> list[dict[str, Any]]:
    """Merge universe rows with manual watchlist overlays."""

    manual_by_symbol = {
        _primary_symbol(row): row
        for row in rows_for_table("manual_watchlist")
        if _primary_symbol(row)
    }
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_row in rows_for_table("universe_screen"):
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


def _table_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"rows": rows, "count": len(rows)}


def _is_active_watchlist_row(row: dict[str, Any]) -> bool:
    return str(row.get("watch_state") or "").lower() in {"owned", "watched"}


def _primary_symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("ticker") or "").upper()


def _rows_for_symbols(rows: list[dict[str, Any]], symbols: set[str]) -> list[dict[str, Any]]:
    if not symbols:
        return []
    return [row for row in rows if row_symbols(row) & symbols]


def row_symbols(row: dict[str, Any]) -> set[str]:
    symbols = {_primary_symbol(row)}
    for key in ("symbols", "related_symbols", "tickers", "bullish_symbols", "bearish_symbols"):
        symbols.update(_symbols_from_value(row.get(key)))
    return {symbol for symbol in symbols if symbol}
