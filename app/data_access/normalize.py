"""Raw panel data -> PanelData normalization."""

from __future__ import annotations
from typing import Any, Iterable

from app.data_access.types import DataStatus, PanelData



def _normalize_panel_data(raw_data: Any) -> PanelData:
    if isinstance(raw_data, PanelData):
        return raw_data

    if isinstance(raw_data, dict):
        status = raw_data.get("status")
        if isinstance(status, DataStatus):
            data_status = status
        else:
            data_status = DataStatus(
                ready=bool(raw_data.get("ready", True)),
                message=str(raw_data.get("message", "Loaded data from core helpers.")),
                source=str(raw_data.get("source", "core")),
            )
        tables = raw_data.get("tables")
        if tables is None:
            tables = {
                key: value
                for key, value in raw_data.items()
                if key not in {"status", "ready", "message", "source", "metadata"}
            }
        return PanelData(
            status=data_status,
            tables=dict(tables),
            metadata=dict(raw_data.get("metadata", {})),
        )

    tables = {
        name: getattr(raw_data, name)
        for name in (
            "candidates",
            "discovered_universe",
            "decision_queue",
            "decision_readiness",
            "source_freshness",
            "symbol_decision_snapshot",
            "symbol_decision_snapshots",
            "signals",
            "ticker_memos",
            "portfolio",
            "theses",
            "thesis_monitor",
            "trader_twins",
            "catalysts",
            "fundamentals",
            "disclosures",
            "quotes",
            "screener",
            "options_expiries",
            "options_chain",
            "options_payoff_scenarios",
            "options_provider_capabilities",
            "options_expiry_signals",
            "options_ticker_signals",
            "option_strategy_versions",
            "option_snapshot",
            "option_features",
            "stock_features",
            "agent_thesis",
            "agent_thesis_request",
            "agent_thesis_validation",
            "agent_postmortem_request",
            "agent_postmortem",
            "candidate_event",
            "candidate_event_mark",
            "candidate_event_attribution",
            "shadow_trade",
            "shadow_trade_mark",
            "radar_state_transition",
            "option_attribution",
            "missed_winner_event",
            "strategy_mutation_proposal",
            "strategy_backtest_result",
            "strategy_forward_test_result",
            "strategy_cohort_result",
            "news",
            "tradingview_symbol_search",
            "tradingview_watchlists",
            "tradingview_alerts",
            "tradingview_chart_state",
            "sepa",
            "liquidity",
            "correlations",
            "etf_premiums",
            "analyst_estimates",
            "earnings",
            "earnings_setups",
            "valuations",
            "provider_runs",
            "broker_status",
            "broker_accounts",
            "broker_positions",
            "broker_market_snapshots",
            "broker_scanner_signals",
            "agent_recommendations",
            "paper_orders",
            "daily_brief",
            "feed_signals",
            "universe_screen",
            "manual_watchlist",
            "source_consensus",
            "source_ticker_rankings",
            "ownership_consensus",
            "market_context",
            "market_valuation_reference_charts",
            "market_valuation_charts",
            "market_environment_assets",
            "market_environment_model",
            "exposure_clusters",
            "correlation_edges",
            "portfolio_risk_cards",
            "review_actions",
            "source_health",
            "sources",
            "source_runs",
            "source_items",
            "ticker_source_signals",
            "settings",
        )
        if hasattr(raw_data, name)
    }
    return PanelData(
        status=DataStatus(True, "Loaded data from core helpers.", "core"),
        tables=tables,
        metadata={},
    )
