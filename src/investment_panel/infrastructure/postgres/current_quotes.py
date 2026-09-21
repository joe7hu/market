"""Bounded API-facing current-quote reads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable


def current_quote_rows(
    connection: Any,
    *,
    symbols: Iterable[str] | None = None,
    limit: int | None = None,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read current quotes, passing a concrete instrument set to the PIT selector.

    A symbol-filtered request must not first materialize the full universe and
    then filter it in Python or an outer SQL query.
    """

    normalized = sorted({str(symbol).strip().upper() for symbol in symbols or () if str(symbol).strip()})
    # ``None`` requests the intentionally broad market read. An explicitly
    # empty portfolio or watchlist requests no instruments.
    if symbols is not None and not normalized:
        return []
    cutoff = as_of or datetime.now(UTC)
    if normalized:
        identifiers = [
            int(row["id"])
            for row in connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = ANY(%s)", [normalized]
            ).fetchall()
        ]
        if not identifiers:
            return []
        selector = "raw.current_price_at(%s, %s::bigint[])"
        parameters: list[Any] = [cutoff, identifiers]
    else:
        selector = "raw.current_price_at(%s, NULL::bigint[])"
        parameters = [cutoff]
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
