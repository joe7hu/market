"""Auto-split from core/disclosures.py — see ARCHITECTURE.md."""
from __future__ import annotations

from typing import Any
from investment_panel.core.prices import fetch_prices, upsert_prices

from investment_panel.core.disclosures.coerce import days_since


def ensure_disclosure_symbol_prices(
    con: Any,
    trader_names: list[str] | None = None,
    lookback_days: int = 900,
    mode: str = "online",
) -> dict[str, Any]:
    params: list[Any] = []
    filter_sql = ""
    if trader_names:
        placeholders = ", ".join(["?"] * len(trader_names))
        filter_sql = f" AND trader_name IN ({placeholders})"
        params.extend(trader_names)
    rows = con.execute(
        f"""
        SELECT symbol, min(event_date) AS earliest_event_date
        FROM disclosures
        WHERE source_type = 'public_disclosure_transaction'
          AND symbol IS NOT NULL
          {filter_sql}
        GROUP BY symbol
        ORDER BY symbol
        """,
        params,
    ).fetchall()
    fetched = 0
    price_rows = 0
    price_errors: dict[str, str] = {}
    for symbol, earliest_event_date in rows:
        earliest_needed = str(earliest_event_date)[:10]
        existing = con.execute(
            "SELECT min(date), count(*) FROM prices_daily WHERE symbol = ?",
            [symbol],
        ).fetchone()
        if existing and existing[1] and str(existing[0]) <= earliest_needed:
            continue
        symbol_lookback_days = max(lookback_days, days_since(earliest_needed) + 14)
        try:
            frame = fetch_prices(symbol, symbol_lookback_days, mode)
        except Exception as exc:
            price_errors[symbol] = f"{type(exc).__name__}: {exc}"
            continue
        price_rows += upsert_prices(con, frame)
        fetched += 1
    return {"price_symbols_fetched": fetched, "price_rows_ingested": price_rows, "price_errors": price_errors}


def price_on_or_before(con: Any, symbol: str, as_of: Any) -> float | None:
    row = con.execute(
        """
        SELECT close FROM prices_daily
        WHERE symbol = ? AND date <= ?
        ORDER BY date DESC
        LIMIT 1
        """,
        [symbol, str(as_of)[:10]],
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def latest_price_for_symbol(con: Any, symbol: str) -> float | None:
    row = con.execute(
        "SELECT close FROM prices_daily WHERE symbol = ? ORDER BY date DESC LIMIT 1",
        [symbol],
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None
