"""Crypto market/category data adapters."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from investment_panel.core.db import json_dumps


COINGECKO_IDS = {
    "BTC-USD": "bitcoin",
    "ETH-USD": "ethereum",
    "SOL-USD": "solana",
}


def fetch_coingecko_markets(symbols: list[str]) -> list[dict[str, Any]]:
    ids = [COINGECKO_IDS[symbol] for symbol in symbols if symbol in COINGECKO_IDS]
    if not ids:
        return []
    with httpx.Client(timeout=20.0) as client:
        response = client.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ",".join(ids),
                "order": "market_cap_desc",
                "per_page": len(ids),
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h,7d,30d",
            },
        )
        response.raise_for_status()
        return response.json()


def fetch_coingecko_categories() -> list[dict[str, Any]]:
    with httpx.Client(timeout=20.0) as client:
        response = client.get("https://api.coingecko.com/api/v3/coins/categories")
        response.raise_for_status()
        return response.json()


def fetch_defillama_protocols() -> list[dict[str, Any]]:
    with httpx.Client(timeout=45.0) as client:
        response = client.get("https://api.llama.fi/protocols")
        response.raise_for_status()
        return response.json()


def upsert_crypto_fundamentals(con: Any, rows: list[dict[str, Any]]) -> int:
    reverse = {value: key for key, value in COINGECKO_IDS.items()}
    today = date.today().isoformat()
    count = 0
    for row in rows:
        symbol = reverse.get(row.get("id"))
        if not symbol:
            continue
        metrics = {
            "market_cap": row.get("market_cap"),
            "fdv": row.get("fully_diluted_valuation"),
            "volume_24h": row.get("total_volume"),
            "market_cap_rank": row.get("market_cap_rank"),
            "price_change_24h": row.get("price_change_percentage_24h"),
            "price_change_7d": row.get("price_change_percentage_7d_in_currency"),
            "price_change_30d": row.get("price_change_percentage_30d_in_currency"),
        }
        con.execute(
            """
            INSERT OR REPLACE INTO crypto_fundamentals (symbol, date, metrics, source)
            VALUES (?, ?, ?, ?)
            """,
            [symbol, today, json_dumps(metrics), "coingecko"],
        )
        count += 1
    return count
