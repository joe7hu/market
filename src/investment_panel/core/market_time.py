"""Resolve the market calendar used by canonical price symbols."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from investment_panel.core.prices import YAHOO_SYMBOL_ALIASES


SUFFIX_TIMEZONES = {
    "AX": "Australia/Sydney", "DE": "Europe/Berlin", "HK": "Asia/Hong_Kong",
    "KS": "Asia/Seoul", "KW": "Asia/Kuwait", "L": "Europe/London",
    "NS": "Asia/Kolkata", "PA": "Europe/Paris", "QA": "Asia/Qatar",
    "SR": "Asia/Riyadh", "ST": "Europe/Stockholm", "SZ": "Asia/Shanghai",
    "T": "Asia/Tokyo", "V": "America/Toronto", "VI": "Europe/Vienna",
}
EXACT_TIMEZONES = {
    "^HSI": "Asia/Hong_Kong", "^KS11": "Asia/Seoul", "^N225": "Asia/Tokyo",
    "^NSEI": "Asia/Kolkata",
}
UTC_CANONICAL_SYMBOLS = {
    "BNBUSD", "BTCUSD", "ETHUSD", "HYPEUSD", "SOLUSD", "XLMUSD", "XRPUSD",
}


def market_timezone_for_symbol(symbol: str) -> str:
    canonical = str(symbol or "").strip().upper()
    provider_symbol = YAHOO_SYMBOL_ALIASES.get(canonical, canonical)
    if canonical in UTC_CANONICAL_SYMBOLS or provider_symbol.endswith(("-USD", "=X")):
        return "UTC"
    if provider_symbol in EXACT_TIMEZONES:
        return EXACT_TIMEZONES[provider_symbol]
    suffix = provider_symbol.rsplit(".", 1)[1] if "." in provider_symbol else ""
    return SUFFIX_TIMEZONES.get(suffix, "America/New_York")


def current_market_date(symbol: str, reference: datetime) -> date:
    return reference.astimezone(ZoneInfo(market_timezone_for_symbol(symbol))).date()
