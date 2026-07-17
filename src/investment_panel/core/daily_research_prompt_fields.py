"""Field allowlists for the daily research handoff."""

DAILY_RESEARCH_TABLES = (
    "preopen_daily_brief", "daily_brief", "portfolio", "portfolio_summary",
    "portfolio_risk_cards", "review_actions", "exposure_clusters", "correlation_edges",
    "universe_screen", "manual_watchlist", "quotes", "fundamentals", "technicals",
    "valuations", "analyst_estimates", "options_ticker_signals", "thesis_monitor",
    "decision_queue", "catalysts", "earnings", "market_environment_model",
    "market_environment_assets", "market_valuation_reference_charts", "option_radar_summary",
    "option_radar_opportunity", "research_packets", "ticker_memos", "source_consensus",
    "ownership_consensus", "feed_signals",
)

DAILY_RESEARCH_QUERY_LIMITS = {
    "quotes": 100,
    "fundamentals": 400,
    "technicals": 100,
    "valuations": 200,
    "analyst_estimates": 100,
    "options_ticker_signals": 100,
    "catalysts": 200,
    "earnings": 200,
    "research_packets": 200,
    "ticker_memos": 200,
    "source_consensus": 50,
    "ownership_consensus": 200,
}

DAILY_RESEARCH_SYMBOL_TABLES = frozenset({
    "quotes", "fundamentals", "technicals", "valuations", "analyst_estimates",
    "options_ticker_signals", "catalysts", "earnings", "research_packets",
    "ticker_memos", "ownership_consensus",
})

DAILY_RESEARCH_MACRO_SYMBOLS = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "VIX", "VXX", "TLT", "UUP", "GLD", "USO",
    "BTC-USD", "ETH-USD",
})

PORTFOLIO_SUMMARY_FIELDS = ("as_of", "oldest_quote_at", "portfolio_value", "cost_basis", "total_pnl", "total_pnl_pct", "day_pnl", "day_pnl_pct", "holdings_count", "valuation_status")
POSITION_FIELDS = ("symbol", "name", "asset_class", "quantity", "average_cost", "purchase_date", "notes", "price", "change_pct", "quote_observed_at", "quote_source", "valuation_status", "market_value", "unrealized_pnl", "unrealized_pnl_pct", "portfolio_weight")
RISK_FIELDS = ("card_id", "risk_type", "severity", "score", "title", "summary", "symbols", "impact", "next_step")
REVIEW_FIELDS = ("action_id", "priority", "status", "title", "symbols", "reason", "impact", "next_step")
EXPOSURE_FIELDS = ("cluster", "cluster_name", "symbols", "market_value", "weight", "risk", "summary")
CORRELATION_FIELDS = ("symbol_a", "symbol_b", "correlation", "lookback_days", "as_of", "risk")
WATCHLIST_FIELDS = ("symbol", "name", "asset_class", "watch_state", "notes", "rating", "quality_score", "value_signal", "action", "next_action", "freshness", "source_count")
PREOPEN_FIELDS = ("brief_date", "generated_at", "session", "status", "headline", "macro_regime", "narrative", "opening_scenario", "qqq_path", "qqq_forecast", "key_events", "watch_items", "risks", "backtest", "source_models", "error")
MARKET_MODEL_FIELDS = ("stable_key", "category", "score", "weight", "posture", "evidence", "portfolio_effect", "next_action", "source", "observed_at", "as_of")
MARKET_ASSET_FIELDS = ("symbol", "name", "asset_class", "group_name", "as_of", "observed_at", "price", "change_pct", "return_1d", "return_1m", "return_ytd", "return_1y", "sma_20_up", "sma_50_up", "sma_200_up", "sma_50_gt_200", "volatility", "trend", "signal", "source")
MARKET_REFERENCE_FIELDS = ("symbol", "as_of", "valuation_metric", "current_value", "percentile", "history", "source")
CATALYST_FIELDS = ("id", "symbol", "starts_at", "event_date", "event", "expected_impact", "notes", "event_scope", "event_kind", "importance", "verification_status", "source", "source_name", "source_url")
EARNINGS_FIELDS = ("id", "symbol", "starts_at", "event", "event_date", "report_date", "fiscal_period", "estimate", "actual", "surprise_pct", "time", "importance", "verification_status", "status", "source", "source_url")
DAILY_BRIEF_FIELDS = ("stable_key", "category", "priority", "symbol", "headline", "summary", "action", "score", "structure", "expiration", "entry_price", "max_loss", "expected_value", "blockers", "next_step")
DECISION_FIELDS = ("stable_key", "category", "priority", "symbol", "headline", "summary", "action", "score", "readiness_status", "structure", "expiration", "entry_price", "max_loss", "expected_value", "blockers", "next_action")
RADAR_SUMMARY_FIELDS = ("publication_cutoff", "latest_complete_quote_time", "latest_snapshot_time", "market_session", "strategy_version", "strategy_revision", "shortlist_count", "symbols_considered", "symbols_with_chains", "contracts_evaluated", "ready_count", "setup_count")
RADAR_FIELDS = ("ticker", "state", "stage", "rank_score", "advisory_action", "structure", "expiration", "underlying_price", "bid", "ask", "mid", "spread_pct", "open_interest", "volume", "iv", "delta", "suggested_limit", "max_loss", "secured_cash", "break_even", "probability_profit", "probability_semantics", "expected_value", "lower_95_expected_value", "risk_adjusted_expectancy", "data_readiness", "execution_ready", "blockers", "catalyst_start", "catalyst_end", "invalidation", "next_evidence", "quote_observed_at", "analysis_cutoff")
THESIS_FIELDS = ("symbol", "status", "owned", "watched", "thesis", "why_owned_watched", "invalidation", "contradiction_flags", "needs_review", "review_reason", "last_reviewed", "latest_source_evidence_at", "latest_price", "latest_quote_at", "structured_fields_missing")
RESEARCH_FIELDS = ("symbol", "ticker", "packet_id", "generated_at", "published_at", "created_at", "status", "title", "summary", "thesis", "countercase", "catalysts", "risks", "invalidation", "evidence_refs", "source", "source_url", "metadata")
MEMO_FIELDS = ("symbol", "ticker", "created_at", "updated_at", "status", "title", "summary", "thesis", "countercase", "action", "invalidation", "evidence_refs")
SOURCE_CONSENSUS_FIELDS = ("source_id", "source_name", "content_type", "items_count", "tickers_count", "net_consensus", "bullish_symbols", "bearish_symbols", "latest_at", "recommendation")
OWNERSHIP_FIELDS = ("symbol", "trader_name", "filer_name", "issuer", "event_date", "filed_date", "value_thousands", "source_url", "accession_number")
FEED_FIELDS = ("id", "title", "thesis", "antithesis", "invalidation", "source", "source_family", "source_type", "date", "symbols", "sentiment", "direction", "confidence", "evidence_refs", "source_url")
QUOTE_FIELDS = ("symbol", "observed_at", "price", "change_pct", "change_abs", "currency", "source")
FUNDAMENTAL_FIELDS = ("symbol", "metric_set", "period_end", "filed_at", "observed_at", "values", "source")
TECHNICAL_FIELDS = ("symbol", "as_of", "observed_at", "close", "return_5d", "return_20d", "return_60d", "sma_20", "sma_50", "sma_200", "rsi_14", "atr_pct", "relative_strength", "trend", "values", "source")
VALUATION_FIELDS = ("symbol", "metric_set", "period_end", "observed_at", "values", "source")
ESTIMATE_FIELDS = ("symbol", "metric_set", "period_end", "observed_at", "values", "source")
OPTIONS_TICKER_FIELDS = ("symbol", "as_of", "source", "status", "nearest_expiry", "nearest_dte", "atm_iv", "iv_regime", "expected_move", "expected_move_pct", "skew_signal", "put_call_iv_skew", "spread_quality", "liquidity_score", "hedge_summary", "income_summary", "unavailable_signals", "raw")
