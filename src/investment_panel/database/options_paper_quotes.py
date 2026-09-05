"""Point-in-time option quote packages for paper execution.

The ticket is immutable.  These helpers only obtain a later, confirmed quote
package to decide whether that ticket could conservatively fill or exit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def latest_option_legs(
    connection: Any,
    *,
    ticket_legs: list[dict[str, Any]],
    as_of: datetime,
) -> list[dict[str, Any]]:
    """Return one latest available complete-capture quote for every ticket leg.

    A quote is usable only after its snapshot/generation completed and became
    available.  The caller still applies the 120-second, size, OI, and skew
    execution policy to the result.
    """

    contract_ids = [_contract_id(leg) for leg in ticket_legs]
    if not contract_ids or any(value is None for value in contract_ids):
        return []
    rows = connection.execute(
        """
        SELECT DISTINCT ON (quote.contract_id)
               quote.contract_id, contract.option_type, contract.strike::double precision AS strike,
               quote.bid, quote.ask, quote.bid_size, quote.ask_size,
               quote.open_interest, quote.volume, quote.observed_at,
               quote.available_at, contract.expiration, contract.multiplier
        FROM raw.option_quote quote
        JOIN catalog.option_contract contract ON contract.id = quote.contract_id
        JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
        LEFT JOIN raw.option_capture_generation generation
          ON generation.id = quote.capture_generation_id
        WHERE quote.contract_id = ANY(%s::bigint[])
          AND quote.available_at <= %s
          AND snapshot.capture_state IN ('complete', 'partial')
          AND (
            quote.capture_generation_id IS NULL
            OR (
              generation.capture_state IN ('complete', 'partial')
              AND generation.capture_finished_at IS NOT NULL
              AND generation.capture_finished_at <= %s
            )
          )
        ORDER BY quote.contract_id, quote.available_at DESC, quote.observed_at DESC, quote.id DESC
        """,
        [[int(value) for value in contract_ids if value is not None], as_of, as_of],
    ).fetchall()
    by_contract = {int(row["contract_id"]): dict(row) for row in rows}
    normalized: list[dict[str, Any]] = []
    for ticket_leg, contract_id in zip(ticket_legs, contract_ids):
        quote = by_contract.get(int(contract_id or 0))
        if quote is None:
            return []
        normalized.append({
            "contract_id": str(contract_id),
            "option_type": str(ticket_leg.get("option_type") or quote["option_type"]),
            "side": str(ticket_leg.get("side") or "buy"),
            "strike": float(quote["strike"]) if quote["strike"] is not None else None,
            "bid": _number(quote.get("bid")),
            "ask": _number(quote.get("ask")),
            "bid_size": _integer(quote.get("bid_size")),
            "ask_size": _integer(quote.get("ask_size")),
            "open_interest": _integer(quote.get("open_interest")),
            "volume": _integer(quote.get("volume")),
            # Information-time freshness, not a provider's nominal timestamp.
            "quote_time": _utc(quote.get("available_at")),
            "observed_at": _utc(quote.get("observed_at")),
            "expiration": quote.get("expiration"),
            "multiplier": _number(quote.get("multiplier")),
        })
    return normalized


def package_price(legs: list[dict[str, Any]], *, phase: str) -> float | None:
    """Return a conservative positive package amount for entry or exit.

    ``entry`` is the debit paid for a long/debit order or credit received for a
    short/credit order.  ``exit`` is the value received for a debit order or
    debit paid to close a credit order.  A missing leg returns ``None``.
    """

    if phase not in {"entry", "exit"} or not legs:
        return None
    signed_cash = 0.0
    for leg in legs:
        bid, ask = _number(leg.get("bid")), _number(leg.get("ask"))
        if bid is None or ask is None or bid <= 0 or ask < bid:
            return None
        is_short = str(leg.get("side") or "").lower() in {"short", "sell"}
        if phase == "entry":
            signed_cash += -bid if is_short else ask
        else:
            signed_cash += ask if is_short else -bid
    # Debit tickets pay a positive entry amount, whereas credit tickets receive
    # a positive credit.  The same convention applies inversely at exit.
    return round(abs(signed_cash), 6)


def is_credit_structure(structure: str) -> bool:
    return str(structure or "").lower() in {"cash_secured_put", "credit_spread", "short_option"}


def _contract_id(leg: dict[str, Any]) -> int | None:
    try:
        value = int(str(leg.get("contract_id") or ""))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _number(value: Any) -> float | None:
    try:
        result = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return result


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
