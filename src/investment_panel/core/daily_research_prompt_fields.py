"""Field allowlists for the daily research handoff."""

DAILY_RESEARCH_TABLES = (
    "preopen_daily_brief",
    "daily_brief",
    "portfolio",
    "portfolio_summary",
    "portfolio_risk_cards",
    "review_actions",
    "exposure_clusters",
    "universe_screen",
    "manual_watchlist",
    "quotes",
    "fundamentals",
    "technicals",
    "analyst_estimates",
    "options_ticker_signals",
    "thesis_monitor",
    "decision_queue",
    "catalysts",
    "earnings",
    "market_environment_model",
    "market_environment_assets",
    "option_radar_opportunity",
)

DAILY_RESEARCH_QUERY_LIMITS = {
    "quotes": 100,
    "fundamentals": 400,
    "technicals": 100,
    "analyst_estimates": 100,
    "options_ticker_signals": 100,
    "catalysts": 200,
    "earnings": 200,
}

DAILY_RESEARCH_SYMBOL_TABLES = frozenset(
    {
        "quotes",
        "fundamentals",
        "technicals",
        "analyst_estimates",
        "options_ticker_signals",
        "catalysts",
        "earnings",
    }
)

DAILY_RESEARCH_MACRO_SYMBOLS = frozenset(
    {
        "SPY",
        "QQQ",
        "IWM",
        "DIA",
        "VIX",
        "VXX",
        "TLT",
        "UUP",
        "GLD",
        "USO",
        "BTC-USD",
        "ETH-USD",
    }
)
