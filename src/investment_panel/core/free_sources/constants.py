"""Free-source scan limits, throttles, and compatibility re-exports."""

from __future__ import annotations
import re

from investment_panel.core.option_scan import (
    RADAR_BASELINE_CALL_STRIKE_OTM_HI,
    RADAR_CALL_STRIKE_OTM_HI,
    RADAR_CALL_STRIKE_OTM_LO,
    RADAR_LOTTERY_CALL_STRIKE_OTM_LO,
    RADAR_MAX_DTE,
    RADAR_MAX_EXPIRIES_PER_SYMBOL,
    RADAR_MIN_DTE,
    RADAR_STRIKES_AROUND_SPOT,
)


OPTION_SCAN_LIMIT = 80


# Stop the option scan after this many consecutive symbols fail with upstream
# rate limits, so a saturated limiter cannot stretch the run across the whole
# universe.
OPTION_RATE_LIMIT_CIRCUIT_BREAKER = 4


# Gap between yfinance option calls. The chains and liquidity jobs BOTH hit
# Yahoo and share one per-IP limiter; without spacing, either job's burst
# saturates it and every call 429s (which then also starves the other job). A
# small gap keeps the combined burst rate under the limit so calls get through.
YFINANCE_OPTION_THROTTLE_SECONDS = 0.4


_RATE_LIMIT_HINT = re.compile(r"\b429\b|too many requests|rate limit", re.IGNORECASE)
