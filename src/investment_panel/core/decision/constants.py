"""Decision thresholds, market hours, and source-set constants."""

from __future__ import annotations
import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo



SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,14}$")


INTRADAY_STALE_HOURS = 4


ARCO_STALE_DAYS = 7


DAILY_STALE_DAYS = 1


FILING_STALE_DAYS = 120


MARKET_TZ = ZoneInfo("America/New_York")


MARKET_OPEN = time(9, 30)


MARKET_CLOSE = time(16, 0)


STATIC_SOURCES = {"config_watchlist", "manual_watchlist", "config", "instrument", "instruments", "candidate"}


PRIMARY_EVIDENCE_SOURCES = {
    "arco_thesis",
    "news",
    "public_disclosure_transaction",
    "13f_holding",
    "13f",
    "analyst_estimate",
    "earnings",
    "earnings_setup",
    "tradingview_alert",
}


DAILY_ANALYSIS_SOURCES = {"technical", "sepa", "liquidity", "correlation", "valuation", "earnings_setup", "options_payoff"}


FRESHNESS_ORDER = {"failed": 0, "stale": 1, "missing": 1, "unknown": 2, "documentation": 3, "not_applicable": 3, "fresh": 4}
