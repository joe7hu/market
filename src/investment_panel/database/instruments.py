"""Canonical instrument identity and catalog reconciliation."""

from __future__ import annotations

from typing import Any

from investment_panel.core.instruments import normalize_symbol, resolved_asset_class
from investment_panel.core.market_time import market_timezone_for_symbol


def canonical_symbol(value: Any) -> str:
    """Return the one catalog symbol accepted by every PostgreSQL writer."""
    symbol = normalize_symbol(str(value or ""))
    if not symbol or len(symbol) > 15 or not all(character.isalnum() or character in ".-" for character in symbol):
        raise ValueError("symbol must be a valid ticker")
    return symbol


def instrument_identity(
    symbol: Any,
    *,
    name: Any = None,
    asset_class: Any = None,
    category: Any = None,
) -> dict[str, str | None]:
    """Normalize identity metadata before it reaches the catalog."""
    canonical = canonical_symbol(symbol)
    supplied_class = str(asset_class or "").strip().lower() or None
    return {
        "symbol": canonical,
        "name": str(name or canonical).strip() or canonical,
        "asset_class": resolved_asset_class(canonical, supplied_class),
        "category": str(category).strip() if category not in (None, "") else None,
        "market_timezone": market_timezone_for_symbol(canonical),
    }


def reconcile_instrument(
    connection: Any,
    symbol: Any,
    *,
    name: Any = None,
    asset_class: Any = None,
    category: Any = None,
) -> int:
    """Resolve an instrument and improve placeholder metadata without downgrading it."""
    identity = instrument_identity(
        symbol,
        name=name,
        asset_class=asset_class,
        category=category,
    )
    row = connection.execute(
        """
        INSERT INTO catalog.instrument
            (symbol, name, asset_class, category, market_timezone)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (symbol) DO UPDATE
        SET name = CASE
                WHEN catalog.instrument.name IS NULL
                  OR catalog.instrument.name = ''
                  OR catalog.instrument.name = catalog.instrument.symbol
                THEN EXCLUDED.name ELSE catalog.instrument.name END,
            asset_class = CASE
                WHEN catalog.instrument.asset_class IN ('unknown', 'equity')
                  AND EXCLUDED.asset_class <> 'unknown'
                THEN EXCLUDED.asset_class ELSE catalog.instrument.asset_class END,
            category = CASE
                WHEN catalog.instrument.category IS NULL
                  OR catalog.instrument.category IN ('option-discovery', 'option-history')
                THEN COALESCE(EXCLUDED.category, catalog.instrument.category)
                ELSE catalog.instrument.category END,
            market_timezone = EXCLUDED.market_timezone,
            updated_at = now()
        RETURNING id
        """,
        [
            identity["symbol"],
            identity["name"],
            identity["asset_class"],
            identity["category"],
            identity["market_timezone"],
        ],
    ).fetchone()
    return int(row["id"])


__all__ = ["canonical_symbol", "instrument_identity", "reconcile_instrument"]
