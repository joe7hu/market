"""Single declaration of panel read models: ``name -> loader`` binding.

Every read model the API can serve is registered here exactly once. The
``snapshot`` dispatcher and the app-side panel-scope contracts derive from this
registry instead of restating the table-name list, so adding a read model is a
single edit and scope contracts cannot drift onto a name that has no loader
(an invariant test in ``tests/`` enforces the latter).

A loader takes a :class:`ReadContext` (the open connection plus the request-scoped
values some accessors need) and returns the read model's rows. The context keeps
loader signatures uniform so the dispatcher can call any of them the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from investment_panel.core import brokers
from investment_panel.core.daily_brief import daily_brief
from investment_panel.core.decision import manual_watchlist_rows
from investment_panel.core.portfolio_intelligence import correlation_edges, exposure_clusters, portfolio_risk_cards, review_actions
from investment_panel.core.signals import signal_rows
from investment_panel.core.sources import source_item_rows, source_registry_rows, source_run_rows, source_ticker_ranking_rows, ticker_source_signal_rows
from investment_panel.core.thesis_monitor import thesis_monitor_rows

from investment_panel.core.panel.technicals import technicals
from investment_panel.core.panel.disclosures import disclosures
from investment_panel.core.panel.market_environment import market_context, market_environment_assets, market_environment_model, market_valuation_charts, market_valuation_reference_charts
from investment_panel.core.panel.feed import feed_signals, ownership_consensus, source_consensus, universe_screen
from investment_panel.core.panel.read_equity import analyst_estimates, candidates, catalysts, correlations, decision_queue, decision_readiness, discovered_universe, earnings, earnings_setups, etf_premiums, fundamentals, liquidity, news, opportunities_ranked, opportunity_sources, portfolio, provider_runs, quotes, reports, research_packets, screener, sepa, source_freshness, source_health, theses, trader_profiles, tradingview_alerts, tradingview_chart_state, tradingview_symbol_search, tradingview_watchlists, valuations
from investment_panel.core.panel.read_options import option_features, option_radar_opportunity, option_radar_summary, option_snapshot, option_strategy_versions, options_chain, options_expiries, options_expiry_signals, options_payoff_scenarios, options_provider_capabilities, options_ticker_signals, stock_features
from investment_panel.core.panel.read_learning import agent_postmortem, agent_postmortem_request, agent_thesis, agent_thesis_request, agent_thesis_validation, candidate_event, candidate_event_attribution, candidate_event_mark, conviction_calibration, missed_winner_event, option_attribution, radar_alert, radar_state_transition, shadow_trade, shadow_trade_mark, strategy_backtest_result, strategy_cohort_result, strategy_forward_test_result, strategy_mutation_proposal, trade_journal, vol_surface_features


@dataclass
class ReadContext:
    """Request-scoped inputs every read-model loader resolves against."""

    con: Any
    active_watchlist: list[dict[str, Any]]
    app_config: Any
    decision_snapshots: list[dict[str, Any]]


ReadLoader = Callable[[ReadContext], list[dict[str, Any]]]


# The canonical catalog. Add a read model here and nowhere else; the dispatcher
# and the scope-contract invariant both read from this dict.
READ_MODELS: dict[str, ReadLoader] = {
    "signals": lambda ctx: signal_rows(ctx.con),
    "opportunities_ranked": lambda ctx: opportunities_ranked(ctx.con),
    "opportunity_sources": lambda ctx: opportunity_sources(ctx.con),
    "discovered_universe": lambda ctx: discovered_universe(ctx.con),
    "decision_queue": lambda ctx: decision_queue(ctx.con),
    "decision_readiness": lambda ctx: decision_readiness(ctx.con),
    "source_freshness": lambda ctx: source_freshness(ctx.con),
    "symbol_decision_snapshot": lambda ctx: ctx.decision_snapshots,
    "symbol_decision_snapshots": lambda ctx: ctx.decision_snapshots,
    "candidates": lambda ctx: candidates(ctx.con),
    "portfolio": lambda ctx: portfolio(ctx.con),
    "theses": lambda ctx: theses(ctx.con),
    "manual_watchlist": lambda ctx: manual_watchlist_rows(ctx.con),
    "thesis_monitor": lambda ctx: thesis_monitor_rows(ctx.con, ctx.active_watchlist),
    "catalysts": lambda ctx: catalysts(ctx.con),
    "fundamentals": lambda ctx: fundamentals(ctx.con),
    "disclosures": lambda ctx: disclosures(ctx.con),
    "quotes": lambda ctx: quotes(ctx.con),
    "screener": lambda ctx: screener(ctx.con),
    "options_expiries": lambda ctx: options_expiries(ctx.con),
    "options_chain": lambda ctx: options_chain(ctx.con),
    "options_payoff_scenarios": lambda ctx: options_payoff_scenarios(ctx.con),
    "options_provider_capabilities": lambda ctx: options_provider_capabilities(ctx.con),
    "options_expiry_signals": lambda ctx: options_expiry_signals(ctx.con),
    "options_ticker_signals": lambda ctx: options_ticker_signals(ctx.con),
    "option_strategy_versions": lambda ctx: option_strategy_versions(ctx.con),
    "option_radar_summary": lambda ctx: option_radar_summary(ctx.con),
    "option_radar_opportunity": lambda ctx: option_radar_opportunity(ctx.con),
    "option_snapshot": lambda ctx: option_snapshot(ctx.con),
    "option_features": lambda ctx: option_features(ctx.con),
    "stock_features": lambda ctx: stock_features(ctx.con),
    "agent_thesis": lambda ctx: agent_thesis(ctx.con),
    "agent_thesis_request": lambda ctx: agent_thesis_request(ctx.con),
    "agent_thesis_validation": lambda ctx: agent_thesis_validation(ctx.con),
    "agent_postmortem_request": lambda ctx: agent_postmortem_request(ctx.con),
    "agent_postmortem": lambda ctx: agent_postmortem(ctx.con),
    "radar_alert": lambda ctx: radar_alert(ctx.con),
    "candidate_event": lambda ctx: candidate_event(ctx.con),
    "candidate_event_mark": lambda ctx: candidate_event_mark(ctx.con),
    "candidate_event_attribution": lambda ctx: candidate_event_attribution(ctx.con),
    "conviction_calibration": lambda ctx: conviction_calibration(ctx.con),
    "vol_surface_features": lambda ctx: vol_surface_features(ctx.con),
    "trade_journal": lambda ctx: trade_journal(ctx.con),
    "shadow_trade": lambda ctx: shadow_trade(ctx.con),
    "shadow_trade_mark": lambda ctx: shadow_trade_mark(ctx.con),
    "radar_state_transition": lambda ctx: radar_state_transition(ctx.con),
    "option_attribution": lambda ctx: option_attribution(ctx.con),
    "missed_winner_event": lambda ctx: missed_winner_event(ctx.con),
    "strategy_mutation_proposal": lambda ctx: strategy_mutation_proposal(ctx.con),
    "strategy_backtest_result": lambda ctx: strategy_backtest_result(ctx.con),
    "strategy_forward_test_result": lambda ctx: strategy_forward_test_result(ctx.con),
    "strategy_cohort_result": lambda ctx: strategy_cohort_result(ctx.con),
    "news": lambda ctx: news(ctx.con),
    "tradingview_symbol_search": lambda ctx: tradingview_symbol_search(ctx.con),
    "tradingview_watchlists": lambda ctx: tradingview_watchlists(ctx.con),
    "tradingview_alerts": lambda ctx: tradingview_alerts(ctx.con),
    "tradingview_chart_state": lambda ctx: tradingview_chart_state(ctx.con),
    "sepa": lambda ctx: sepa(ctx.con),
    "liquidity": lambda ctx: liquidity(ctx.con),
    "correlations": lambda ctx: correlations(ctx.con),
    "etf_premiums": lambda ctx: etf_premiums(ctx.con),
    "analyst_estimates": lambda ctx: analyst_estimates(ctx.con),
    "earnings": lambda ctx: earnings(ctx.con),
    "earnings_setups": lambda ctx: earnings_setups(ctx.con),
    "valuations": lambda ctx: valuations(ctx.con),
    "technicals": lambda ctx: technicals(ctx.con),
    "research_packets": lambda ctx: research_packets(ctx.con),
    "provider_runs": lambda ctx: provider_runs(ctx.con),
    "broker_status": lambda ctx: brokers.broker_status_rows(ctx.con),
    "broker_accounts": lambda ctx: brokers.broker_accounts(ctx.con),
    "broker_positions": lambda ctx: brokers.broker_positions(ctx.con),
    "broker_market_snapshots": lambda ctx: brokers.broker_market_snapshots(ctx.con),
    "broker_scanner_signals": lambda ctx: brokers.broker_scanner_signals(ctx.con),
    "agent_recommendations": lambda ctx: brokers.agent_recommendations(ctx.con),
    "paper_orders": lambda ctx: brokers.paper_orders(ctx.con),
    "daily_brief": lambda ctx: daily_brief(ctx.con),
    "feed_signals": lambda ctx: feed_signals(ctx.con, ctx.active_watchlist),
    "universe_screen": lambda ctx: universe_screen(ctx.con, ctx.active_watchlist),
    "source_consensus": lambda ctx: source_consensus(ctx.con),
    "ownership_consensus": lambda ctx: ownership_consensus(ctx.con),
    "market_context": lambda ctx: market_context(ctx.con),
    "market_valuation_reference_charts": lambda ctx: market_valuation_reference_charts(ctx.con),
    "market_valuation_charts": lambda ctx: market_valuation_charts(ctx.con, ctx.active_watchlist),
    "market_environment_assets": lambda ctx: market_environment_assets(ctx.con),
    "market_environment_model": lambda ctx: market_environment_model(ctx.con, ctx.active_watchlist),
    "exposure_clusters": lambda ctx: exposure_clusters(ctx.con),
    "correlation_edges": lambda ctx: correlation_edges(ctx.con),
    "portfolio_risk_cards": lambda ctx: portfolio_risk_cards(ctx.con),
    "review_actions": lambda ctx: review_actions(ctx.con),
    "ticker_memos": lambda ctx: reports(ctx.con),
    "trader_twins": lambda ctx: trader_profiles(ctx.app_config.trader_profile_dir),
    "source_health": lambda ctx: source_health(ctx.con),
    "sources": lambda ctx: source_registry_rows(ctx.con),
    "source_runs": lambda ctx: source_run_rows(ctx.con),
    "source_ticker_rankings": lambda ctx: source_ticker_ranking_rows(ctx.con),
    "source_items": lambda ctx: source_item_rows(ctx.con),
    "ticker_source_signals": lambda ctx: ticker_source_signal_rows(ctx.con),
}


def read_model_names() -> frozenset[str]:
    """The set of read-model names the dispatcher can serve."""

    return frozenset(READ_MODELS)


def load_read_models(ctx: ReadContext, names: set[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Load the requested read models (or all of them) against ``ctx``.

    Unknown names are ignored so a stale scope entry degrades to an absent table
    rather than raising; the scope-contract invariant test is what flags drift.
    """

    selected = set(READ_MODELS) if not names else (set(names) & set(READ_MODELS))
    return {name: READ_MODELS[name](ctx) for name in READ_MODELS if name in selected}
