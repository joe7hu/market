"""One monitored population for collection, features, decisions and health.

An explicit database exclusion overrides configuration unless the instrument
is owned. A zero position is not ownership. Benchmarks are added by consumers,
not silently presented to the user as monitored investment recommendations.
"""
from __future__ import annotations
from typing import Any, Iterable, Mapping


def merge_monitored_universe(
    stored: Iterable[Mapping[str, Any]], configured: Iterable[Mapping[str, Any]],
) -> list[dict[str, str]]:
    output: dict[str, dict[str, str]] = {}
    excluded: set[str] = set()
    for row in stored:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        if row.get("watch_state") == "excluded" and not row.get("is_owned"):
            excluded.add(symbol)
            continue
        output[symbol] = {"symbol": symbol, "asset_class": str(row.get("asset_class") or "equity")}
    for row in configured:
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol and symbol not in excluded and symbol not in output and row.get("watch_state") != "excluded":
            output[symbol] = {"symbol": symbol, "asset_class": str(row.get("asset_class") or ("crypto" if symbol.endswith("-USD") else "equity"))}
    return sorted(output.values(), key=lambda row: row["symbol"])


def monitored_universe(runtime: Any, configured: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    with runtime.read() as connection:
        rows = connection.execute(
            """SELECT instrument.symbol, instrument.asset_class,
                      coalesce(position.quantity, 0) <> 0 AS is_owned, watchlist.watch_state
               FROM catalog.instrument instrument
               LEFT JOIN app.portfolio_position position ON position.instrument_id = instrument.id
               LEFT JOIN app.watchlist_item watchlist ON watchlist.instrument_id = instrument.id
               WHERE coalesce(position.quantity, 0) <> 0 OR watchlist.instrument_id IS NOT NULL
               ORDER BY instrument.symbol"""
        ).fetchall()
    return merge_monitored_universe(rows, configured)


__all__ = ["merge_monitored_universe", "monitored_universe"]
