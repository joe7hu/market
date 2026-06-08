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
CRYPTO_ALIASES = {
    "BTC": "BTC-USD",
    "BTCUSD": "BTC-USD",
    "ETH": "ETH-USD",
    "ETHUSD": "ETH-USD",
    "SOL": "SOL-USD",
    "SOLUSD": "SOL-USD",
    "BNBUSD": "BNB-USD",
    "HYPEUSD": "HYPE-USD",
    "XLMUSD": "XLM-USD",
    "XRPUSD": "XRP-USD",
}
ETF_SYMBOLS = {
    "AGGY", "ARKK", "ARGT", "BITO", "BOTT", "DBC", "DGRO", "DIAL", "EWZ", "FBND",
    "FCOM", "FDIS", "FEMS", "FQAL", "FREL", "FUTY", "GDX", "GLTR", "IGV", "JEPI",
    "NVD", "NOWL", "NUMG", "PDBC", "SMH", "SOXL", "SOXS", "SPDN", "SPY", "SQQQ",
    "TIP", "TLT", "TQQQ", "TSLG", "TSLL", "TZA", "UVIX",
}
INDEX_SYMBOLS = {"DJI", "HSI", "IXIC", "KOSPI", "NIFTY", "NI225", "SPX", "TASI", "TOPIX"}
FX_SYMBOLS = {"USDJPY", "USDKRW", "USDMYR", "USDPHP", "USDSGD", "USDTHB"}


def normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    return CRYPTO_ALIASES.get(normalized, normalized)


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
    normalized = normalize_symbol(symbol)
    if normalized.endswith("-USD"):
        return "crypto"
    if normalized in ETF_SYMBOLS:
        return "etf"
    if normalized in INDEX_SYMBOLS:
        return "index"
    if normalized in FX_SYMBOLS:
        return "fx"
    if normalized.isdigit():
        return "foreign_equity"
    return "equity"


def resolved_asset_class(symbol: str, supplied: str | None = None) -> str:
    inferred = infer_asset_class(symbol)
    if inferred != "equity":
        return inferred
    return supplied or inferred
