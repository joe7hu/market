"""Watchlist resolution and instrument promotion."""

from __future__ import annotations
from typing import Any
from investment_panel.core.db import json_dumps, query_rows, upsert_instrument
from investment_panel.core.instruments import infer_asset_class, normalize_symbol

from investment_panel.core.decision.constants import STATIC_SOURCES, SYMBOL_RE



def watchlist_from_config(config: Any | None) -> list[dict[str, Any]]:
    if config is None:
        return []
    if isinstance(config, list):
        return config
    if isinstance(config, dict):
        return list(config.get("watchlist") or [])
    return list(getattr(config, "watchlist", []) or [])




def manual_watchlist_rows(con: Any, include_excluded: bool = False) -> list[dict[str, Any]]:
    where = "" if include_excluded else "WHERE COALESCE(watch_state, 'watched') != 'excluded'"
    return query_rows(
        con,
        f"""
        SELECT symbol, name, asset_class, COALESCE(watch_state, 'watched') AS watch_state, notes, created_at, updated_at
        FROM manual_watchlist
        {where}
        ORDER BY symbol
        """,
    )




def effective_watchlist(con: Any, config_watchlist: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in config_watchlist or []:
        symbol = normalize_symbol(str(item.get("symbol") or ""))
        if not symbol or not SYMBOL_RE.match(symbol):
            continue
        merged[symbol] = {
            **item,
            "symbol": symbol,
            "name": item.get("name") or symbol,
            "asset_class": item.get("asset_class") or infer_asset_class(symbol),
            "source": item.get("source") or "config_watchlist",
        }
    for row in manual_watchlist_rows(con, include_excluded=True):
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        if not symbol or not SYMBOL_RE.match(symbol):
            continue
        if row.get("watch_state") == "excluded":
            merged.pop(symbol, None)
            continue
        existing = merged.get(symbol, {})
        merged[symbol] = {
            **existing,
            "symbol": symbol,
            "name": row.get("name") or existing.get("name") or symbol,
            "asset_class": row.get("asset_class") or existing.get("asset_class") or infer_asset_class(symbol),
            "source": "manual_watchlist" if not existing else existing.get("source") or "config_watchlist",
            "notes": row.get("notes") or existing.get("notes"),
        }
    return sorted(merged.values(), key=lambda row: row["symbol"])




def ensure_watchlist_instruments(con: Any, watchlist: list[dict[str, Any]]) -> int:
    count = 0
    for item in watchlist:
        symbol = normalize_symbol(str(item.get("symbol") or ""))
        if not symbol or not SYMBOL_RE.match(symbol):
            continue
        upsert_instrument_preserving(
            con,
            {
                "symbol": symbol,
                "name": item.get("name") or symbol,
                "asset_class": item.get("asset_class") or infer_asset_class(symbol),
                "sector": item.get("sector"),
                "industry": item.get("industry"),
                "category": item.get("category") or "watchlist",
                "source": item.get("source") or "watchlist",
            },
        )
        count += 1
    return count




def promote_universe_instruments(con: Any, universe: list[dict[str, Any]]) -> int:
    promoted = 0
    for row in universe:
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        asset_class = str(row.get("asset_class") or infer_asset_class(symbol))
        if not symbol or not SYMBOL_RE.match(symbol) or asset_class not in {"equity", "etf", "crypto"}:
            continue
        counts = row.get("source_counts") if isinstance(row.get("source_counts"), dict) else {}
        source = next((key for key, value in counts.items() if key not in STATIC_SOURCES and int(value or 0) > 0), "discovered_universe")
        upsert_instrument_preserving(
            con,
            {
                "symbol": symbol,
                "name": row.get("name") or symbol,
                "asset_class": asset_class,
                "category": "universe",
                "source": source,
            },
        )
        promoted += 1
    return promoted




def upsert_instrument_preserving(con: Any, instrument: dict[str, Any]) -> None:
    symbol = normalize_symbol(str(instrument.get("symbol") or ""))
    existing_rows = query_rows(con, "SELECT symbol, name, asset_class, sector, industry, category, source FROM instruments WHERE symbol = ?", [symbol])
    existing = existing_rows[0] if existing_rows else {}
    name = instrument.get("name") or existing.get("name") or symbol
    if existing.get("name") and name == symbol:
        name = existing.get("name")
    upsert_instrument(
        con,
        {
            "symbol": symbol,
            "name": name,
            "asset_class": instrument.get("asset_class") or existing.get("asset_class") or infer_asset_class(symbol),
            "sector": instrument.get("sector") or existing.get("sector"),
            "industry": instrument.get("industry") or existing.get("industry"),
            "category": existing.get("category") or instrument.get("category"),
            "source": existing.get("source") or instrument.get("source"),
        },
    )
