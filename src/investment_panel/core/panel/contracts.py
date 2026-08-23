"""Panel route/table contracts shared by API loaders and tests."""

from __future__ import annotations

from typing import Any


PANEL_SCOPE_TABLES: dict[str, tuple[str, ...]] = {
    "feed": ("feed_signals",),
    "today": (
        "preopen_daily_brief",
        "daily_brief",
        "portfolio_risk_cards",
        "portfolio",
        "ticker_decisions",
        # Today reads canonical published ticker decisions and legacy Radar
        # rows for symbol discovery. It must never show a separately copied
        # option action queue from an older daily-brief publication. Thesis
        # work, feed rows, and broad decision queues remain lazy routes.
        "option_radar_opportunity",
    ),
    "watchlist": (
        "universe_screen",
        "manual_watchlist",
        "discovered_universe",
        "decision_queue",
        "research_packets",
        "ticker_memos",
        "thesis_monitor",
        "quotes",
        "portfolio",
        "screener",
        "technicals",
        "valuations",
        "options_ticker_signals",
    ),
    "sources": (
        "source_ticker_rankings",
        "ticker_source_signals",
        "source_items",
        "source_consensus",
        "feed_signals",
        "opportunity_sources",
        "theses",
        "news",
        "sources",
    ),
    # The portfolio model already preserves SEC source links and filing
    # provenance. Do not duplicate the full raw disclosure corpus in this
    # user-facing scope; the direct disclosures endpoint remains available.
    "superinvestors": ("superinvestor_portfolios", "ownership_consensus"),
    "market": ("market_valuation_reference_charts", "market_environment_assets", "market_environment_model"),
    "dashboard": (
        "decision_queue",
        "discovered_universe",
        "quotes",
        "screener",
        "portfolio",
        "catalysts",
        "earnings",
        "earnings_setups",
        "analyst_estimates",
        "fundamentals",
        "liquidity",
        "correlations",
        "technicals",
        "sepa",
        "valuations",
        "options_expiries",
        "options_payoff_scenarios",
        "options_provider_capabilities",
        "options_expiry_signals",
        "options_ticker_signals",
        "option_strategy_versions",
        "option_radar_opportunity",
        "option_snapshot",
        "option_features",
        "stock_features",
        "agent_thesis",
        "agent_thesis_request",
        "agent_thesis_validation",
        "candidate_event",
        "radar_alert",
        "candidate_event_mark",
        "candidate_event_attribution",
        "shadow_trade",
        "shadow_trade_mark",
        "radar_state_transition",
        "trade_journal",
        "vol_surface_features",
        "conviction_calibration",
        "option_calibration",
        "option_attribution",
        "missed_winner_event",
        "strategy_mutation_proposal",
        "strategy_backtest_result",
        "strategy_forward_test_result",
        "strategy_cohort_result",
        "disclosures",
        "theses",
        "thesis_monitor",
        "research_packets",
        "opportunity_sources",
        "ticker_source_signals",
        "exposure_clusters",
        "correlation_edges",
        "portfolio_risk_cards",
        "review_actions",
    ),
    "opportunities": (
        "decision_queue",
        "opportunities_ranked",
        "opportunity_sources",
        "signals",
        "candidates",
        "quotes",
        "catalysts",
        "earnings",
        "earnings_setups",
        "analyst_estimates",
        "liquidity",
        "technicals",
        "sepa",
        "valuations",
        "options_expiries",
        "options_payoff_scenarios",
        "options_expiry_signals",
        "options_ticker_signals",
        "candidate_event",
        "radar_alert",
        "option_radar_opportunity",
        "candidate_event_mark",
        "candidate_event_attribution",
        "shadow_trade",
        "shadow_trade_mark",
        "radar_state_transition",
        "trade_journal",
        "vol_surface_features",
        "conviction_calibration",
        "option_attribution",
        "missed_winner_event",
        "strategy_mutation_proposal",
        "strategy_backtest_result",
        "strategy_forward_test_result",
        "strategy_cohort_result",
        "agent_thesis_request",
        "agent_thesis_validation",
        "screener",
        "portfolio",
        "discovered_universe",
        "exposure_clusters",
        "correlation_edges",
        "portfolio_risk_cards",
        "review_actions",
    ),
    "portfolio": (
        "portfolio",
        "portfolio_summary",
        "portfolio_performance",
        "portfolio_transactions",
        "quotes",
        "correlation_edges",
        "exposure_clusters",
        "portfolio_risk_cards",
        "review_actions",
    ),
    "research": (
        "decision_queue",
        "research_packets",
        "ticker_memos",
        "theses",
        "thesis_monitor",
        "news",
        "fundamentals",
        "signals",
        "quotes",
        "earnings",
        "earnings_setups",
        "analyst_estimates",
        "valuations",
        "options_payoff_scenarios",
        "options_ticker_signals",
        "candidate_event",
        "radar_alert",
        "option_radar_opportunity",
        "candidate_event_mark",
        "candidate_event_attribution",
        "shadow_trade",
        "shadow_trade_mark",
        "radar_state_transition",
        "trade_journal",
        "vol_surface_features",
        "conviction_calibration",
        "option_attribution",
        "missed_winner_event",
        "strategy_mutation_proposal",
        "strategy_backtest_result",
        "strategy_forward_test_result",
        "strategy_cohort_result",
        "agent_thesis_request",
        "agent_thesis_validation",
        "agent_postmortem_request",
        "agent_postmortem",
    ),
    "thesis-monitor": (
        "thesis_monitor",
        "theses",
    ),
    "options-radar": (
        "option_radar_summary",
        "option_radar_opportunity",
    ),
    # Recovery, provider, batch, and capture details are operational health
    # data.  Keep them off the primary trade-decision payload.
    "health": (
        "source_catalog",
        "source_freshness",
        "source_health",
        "source_runs",
        "provider_runs",
        "broker_status",
        "option_recovery_funnel",
        "option_recovery_event",
        "option_recovery_opportunity",
        "option_recovery_family_performance",
        "option_recovery_agent_provenance",
        "option_recovery_health",
    ),
    "decision-inbox": (),
    "filings": ("ownership_consensus", "disclosures"),
    "calendar": ("catalysts", "earnings"),
    "settings": (),
}

WATCHLIST_SECTION_TABLES = (
    "universe_screen",
    "manual_watchlist",
    "quotes",
    "fundamentals",
    "technicals",
    "valuations",
    "screener",
    "decision_queue",
    "research_packets",
    "ticker_memos",
    "thesis_monitor",
    "portfolio",
    "options_ticker_signals",
)

TICKER_TABLES = (
    "candidates",
    "decision_queue",
    "discovered_universe",
    "universe_screen",
    "symbol_decision_snapshot",
    "symbol_decision_snapshots",
    "opportunities_ranked",
    "opportunity_sources",
    "feed_signals",
    "source_consensus",
    "ticker_source_signals",
    "ownership_consensus",
    "portfolio",
    "theses",
    "thesis_monitor",
    "catalysts",
    "signals",
    "fundamentals",
    "disclosures",
    "quotes",
    "options_expiries",
    "options_chain",
    "options_payoff_scenarios",
    "options_provider_capabilities",
    "options_expiry_signals",
    "options_ticker_signals",
    "news",
    "instrument_market_identity",
    "sepa",
    "liquidity",
    "analyst_estimates",
    "earnings",
    "earnings_setups",
    "valuations",
    "technicals",
    "research_packets",
    "portfolio_risk_cards",
    "review_actions",
    "ticker_memos",
    "decision_truth",
    "event_decision_packets",
    "event_scout_events",
    "ticker_decisions",
    "ticker_outcomes",
    "ticker_benchmark_snapshot",
)

# The dossier route must not load every evidence model before it can render a
# decision.  These tables support the decision header, canonical price,
# portfolio relevance, thesis state, and the current option signal.  Deeper
# evidence is fetched only when its panel is opened.
TICKER_INITIAL_TABLES = (
    "symbol_decision_snapshot",
    "decision_queue",
    "opportunities_ranked",
    "candidates",
    "quotes",
    "portfolio",
    "broker_accounts",
    "broker_positions",
    "source_freshness",
    "option_strategy_versions",
    "theses",
    "thesis_monitor",
    "catalysts",
    "earnings",
    "fundamentals",
    "valuations",
    "analyst_estimates",
    "disclosures",
    "ownership_consensus",
    "technicals",
    "options_ticker_signals",
    "options_payoff_scenarios",
    "ticker_decisions",
    "ticker_outcomes",
    "ticker_benchmark_snapshot",
)

DECISION_REPAIR_TABLES = {
    "decision_readiness",
    "decision_queue",
    "discovered_universe",
    "feed_signals",
    "source_freshness",
    "symbol_decision_snapshot",
    "symbol_decision_snapshots",
    "thesis_monitor",
    "universe_screen",
}

SOURCE_REPAIR_TABLES = {
    "feed_signals",
    "news",
    "opportunity_sources",
    "source_consensus",
    "source_items",
    "source_runs",
    "source_ticker_rankings",
    "sources",
    "ticker_source_signals",
}

FRONTEND_TABLE_KEY_OVERRIDES = {
    "ticker_memos": "memos",
}

FRONTEND_ADDITIONAL_TABLES = (
    "broker_market_snapshots",
    "broker_scanner_signals",
    "market_valuation_charts",
)

WATCHLIST_SECTION_OUTPUT_TABLES = (
    "watchlist_watched",
    "watchlist_unwatched",
    "watchlist_watched_quotes",
    "watchlist_unwatched_quotes",
    "watchlist_watched_fundamentals",
    "watchlist_unwatched_fundamentals",
    "watchlist_watched_technicals",
    "watchlist_unwatched_technicals",
    "watchlist_watched_valuations",
    "watchlist_unwatched_valuations",
    "watchlist_watched_screener",
    "watchlist_unwatched_screener",
    "watchlist_watched_decision_queue",
    "watchlist_unwatched_decision_queue",
    "watchlist_watched_research_packets",
    "watchlist_unwatched_research_packets",
    "watchlist_watched_memos",
    "watchlist_unwatched_memos",
    "watchlist_watched_thesis_monitor",
    "watchlist_unwatched_thesis_monitor",
    "watchlist_watched_portfolio",
    "watchlist_unwatched_portfolio",
    "watchlist_watched_options",
    "watchlist_unwatched_options",
)


def tables_for_scope(scope: str) -> tuple[str, ...]:
    if scope in {"watchlist-watched", "watchlist-unwatched"}:
        return WATCHLIST_SECTION_TABLES
    return PANEL_SCOPE_TABLES.get(scope, PANEL_SCOPE_TABLES["dashboard"])


def panel_snapshot_table_names() -> frozenset[str]:
    names: set[str] = set()
    for scope, tables in PANEL_SCOPE_TABLES.items():
        if scope != "settings":
            names.update(tables)
    names.update(WATCHLIST_SECTION_OUTPUT_TABLES)
    return frozenset(names)


def frontend_table_names() -> frozenset[str]:
    """Every backend table name that the frontend PanelData model may expose."""

    names = set(panel_snapshot_table_names())
    names.update(TICKER_TABLES)
    names.update(FRONTEND_ADDITIONAL_TABLES)
    return frozenset(names)


def frontend_key_for_table(table_name: str) -> str:
    if table_name in FRONTEND_TABLE_KEY_OVERRIDES:
        return FRONTEND_TABLE_KEY_OVERRIDES[table_name]
    head, *tail = table_name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def panel_contract_payload() -> dict[str, Any]:
    return {
        "scopes": {scope: list(tables) for scope, tables in PANEL_SCOPE_TABLES.items()},
        "watchlist_section_tables": list(WATCHLIST_SECTION_TABLES),
        "watchlist_section_output_tables": list(WATCHLIST_SECTION_OUTPUT_TABLES),
        "ticker_tables": list(TICKER_TABLES),
        "frontend_table_keys": {
            table_name: frontend_key_for_table(table_name)
            for table_name in sorted(frontend_table_names())
        },
    }
