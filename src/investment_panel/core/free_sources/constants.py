"""Radar DTE, option-scan, and yfinance throttle constants."""

from __future__ import annotations
import re


RADAR_MIN_DTE = 365


RADAR_MAX_DTE = 900


RADAR_MAX_EXPIRIES_PER_SYMBOL = 2


RADAR_STRIKES_AROUND_SPOT = 24

RADAR_CALL_STRIKE_OTM_LO = 1.0

RADAR_BASELINE_CALL_STRIKE_OTM_HI = 1.6

RADAR_LOTTERY_CALL_STRIKE_OTM_LO = 1.6


RADAR_CALL_STRIKE_OTM_HI = 3.0


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
