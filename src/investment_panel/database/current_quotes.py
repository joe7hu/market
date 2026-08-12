"""Bounded API-facing current-quote reads."""

from __future__ import annotations

from typing import Any, Iterable


def current_quote_rows(
    connection: Any,
    *,
    symbols: Iterable[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read current quotes, passing a concrete instrument set to the PIT selector.

    A symbol-filtered request must not first materialize the full universe and
    then filter it in Python or an outer SQL query.
    """

    normalized = sorted({str(symbol).strip().upper() for symbol in symbols or () if str(symbol).strip()})
    if normalized:
        identifiers = [
            int(row["id"])
            for row in connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = ANY(%s)", [normalized]
            ).fetchall()
        ]
        if not identifiers:
            return []
        selector = "raw.current_price_at(now(), %s::bigint[])"
        parameters: list[Any] = [identifiers]
    else:
        selector = "raw.current_price_at(now(), NULL::bigint[])"
        parameters = []
    bounded = "" if limit is None else " LIMIT %s"
    if limit is not None:
        parameters.append(max(1, int(limit)))
    rows = connection.execute(
        f"""
        SELECT instrument.symbol, quote.observed_at, quote.price,
               quote.change_pct, quote.change_abs, quote.currency,
               quote.source_id AS source, quote.available_at
        FROM {selector} quote
        JOIN catalog.instrument instrument ON instrument.id = quote.instrument_id
        ORDER BY quote.available_at DESC, instrument.symbol
        {bounded}
        """,
        parameters,
    ).fetchall()
    return [dict(row) for row in rows]
