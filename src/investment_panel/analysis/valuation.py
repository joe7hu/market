"""Simple valuation model over stored fundamentals and quotes."""

from __future__ import annotations

from datetime import date
from typing import Any

from investment_panel.core.db import json_dumps, query_rows


def store_valuation_models(con: Any, symbols: list[str]) -> int:
    today = date.today().isoformat()
    count = 0
    for symbol in symbols:
        con.execute("DELETE FROM valuation_models WHERE symbol = ? AND method = 'fundamental_proxy'", [symbol])
        fundamentals = query_rows(
            con,
            "SELECT metrics FROM equity_fundamentals WHERE symbol = ? ORDER BY period_end DESC LIMIT 1",
            [symbol],
        )
        if not fundamentals:
            continue
        metrics = parse_json(fundamentals[0]["metrics"])
        if metrics.get("status") != "ok":
            continue
        price = latest_price(con, symbol)
        revenue = as_float(metrics.get("revenue"))
        growth = as_float(metrics.get("revenue_growth")) or 0.03
        margin = as_float(metrics.get("net_margin")) or 0.08
        if price is None or revenue is None or revenue <= 0:
            continue
        if not metrics_pass_sanity_checks(growth, margin):
            continue
        multiplier = max(0.35, min(2.5, 1 + growth * 2 + margin))
        fair_value_proxy = price * multiplier
        upside_pct = (multiplier - 1) * 100
        con.execute(
            """
            INSERT OR REPLACE INTO valuation_models
            (symbol, as_of, method, fair_value, upside_pct, assumptions, diagnostics)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                symbol,
                today,
                "fundamental_proxy",
                fair_value_proxy,
                upside_pct,
                json_dumps({"revenue_growth": growth, "net_margin": margin, "current_price": price}),
                json_dumps(
                    {
                        "confidence": "low",
                        "note": "Proxy valuation; upside is percentage points and excludes rows with implausible fundamentals.",
                    }
                ),
            ],
        )
        count += 1
    return count


def metrics_pass_sanity_checks(growth: float, margin: float) -> bool:
    """Reject bad fact mappings before they become misleading valuation outputs."""

    return -0.8 <= growth <= 2.0 and -0.75 <= margin <= 0.65


def latest_price(con: Any, symbol: str) -> float | None:
    quote = query_rows(con, "SELECT price FROM quotes_intraday WHERE symbol = ? ORDER BY observed_at DESC LIMIT 1", [symbol])
    if quote and quote[0].get("price") is not None:
        return as_float(quote[0]["price"])
    daily = query_rows(con, "SELECT close FROM prices_daily WHERE symbol = ? ORDER BY date DESC LIMIT 1", [symbol])
    return as_float(daily[0]["close"]) if daily else None


def parse_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    import json

    try:
        return json.loads(value)
    except Exception:
        return {}


def as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
