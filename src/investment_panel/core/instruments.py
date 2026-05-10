"""Instrument universe construction."""

from __future__ import annotations

import re
from typing import Any


DEFAULT_WATCHLIST = [
    {"symbol": "SPY", "name": "S&P 500 ETF", "asset_class": "etf", "category": "market"},
    {"symbol": "QQQ", "name": "Nasdaq 100 ETF", "asset_class": "etf", "category": "market"},
    {"symbol": "NVDA", "name": "NVIDIA", "asset_class": "equity", "category": "ai-infrastructure"},
    {"symbol": "TSLA", "name": "Tesla", "asset_class": "equity", "category": "ai-robotics"},
    {"symbol": "COIN", "name": "Coinbase", "asset_class": "equity", "category": "crypto-infrastructure"},
    {"symbol": "BTC-USD", "name": "Bitcoin", "asset_class": "crypto", "category": "crypto-major"},
    {"symbol": "ETH-USD", "name": "Ethereum", "asset_class": "crypto", "category": "crypto-major"},
    {"symbol": "SOL-USD", "name": "Solana", "asset_class": "crypto", "category": "crypto-major"},
]

CASHTAG_RE = re.compile(r"(?<![A-Z0-9])\$([A-Z][A-Z0-9.]{0,9})(?![A-Z0-9])")


def normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    aliases = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
    return aliases.get(normalized, normalized)


def universe_from_config_and_arco(config_watchlist: list[dict[str, Any]], arco_items: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for item in DEFAULT_WATCHLIST + list(config_watchlist or []):
        symbol = normalize_symbol(str(item.get("symbol", "")))
        if not symbol:
            continue
        seen[symbol] = {
            "symbol": symbol,
            "name": item.get("name") or symbol,
            "asset_class": item.get("asset_class") or infer_asset_class(symbol),
            "sector": item.get("sector"),
            "industry": item.get("industry"),
            "category": item.get("category"),
            "source": item.get("source") or "config",
            "cik": item.get("cik"),
        }
    for item in arco_items or []:
        for symbol in symbols_from_text("\n".join(str(item.get(key, "")) for key in ("text", "title", "summary", "claim", "bet"))):
            seen.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "name": symbol,
                    "asset_class": infer_asset_class(symbol),
                    "sector": None,
                    "industry": None,
                    "category": "arco-mentioned",
                    "source": "arco",
                    "cik": None,
                },
            )
    return sorted(seen.values(), key=lambda row: row["symbol"])


def symbols_from_text(text: str) -> list[str]:
    symbols = [normalize_symbol(match.group(1)) for match in CASHTAG_RE.finditer(text or "")]
    return sorted(set(symbols))


def infer_asset_class(symbol: str) -> str:
    if symbol.endswith("-USD"):
        return "crypto"
    if symbol in {"SPY", "QQQ", "IWM"}:
        return "etf"
    return "equity"
