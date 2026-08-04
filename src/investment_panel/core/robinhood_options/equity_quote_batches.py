"""Bounded Robinhood equity-quote batches for recovery detection."""

from __future__ import annotations

import time
from typing import Any, Protocol

from investment_panel.core.coercion import to_finite_float as as_float


class EquityQuoteClient(Protocol):
    def get_equity_quotes(self, symbols: list[str]) -> dict[str, Any]: ...


def fetch_equity_quotes(
    client: EquityQuoteClient,
    symbols: list[str],
    *,
    deadline: float | None = None,
    regular_session_only: bool = False,
) -> list[dict[str, Any]]:
    """Fetch at most 20 symbols per request, honoring the collection deadline."""

    rows: list[dict[str, Any]] = []
    values = [symbol.upper() for symbol in symbols if symbol]
    for start in range(0, len(values), 20):
        if deadline is not None and time.monotonic() > deadline:
            return rows
        payload = client.get_equity_quotes(values[start : start + 20])
        for result in _payload_list(payload, "results"):
            quote = dict(result.get("quote") or {})
            symbol = str(quote.get("symbol") or "").upper()
            if not symbol:
                continue
            price = as_float(quote.get("last_trade_price")) if regular_session_only else _latest_price(quote)
            observed_at = quote.get("venue_last_trade_time") if regular_session_only else (
                quote.get("venue_last_non_reg_trade_time") or quote.get("venue_last_trade_time")
            )
            rows.append({
                "symbol": symbol,
                "time": observed_at,
                "close": price,
                "option_spot": as_float(quote.get("last_trade_price")) or price,
                "change": _quote_change_pct(quote, price),
                "change_abs": _quote_change_abs(quote, price),
                "currency": "USD",
                "source": "robinhood",
                "raw": quote,
            })
    return rows


def _payload_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    data = payload.get("data")
    rows = (data if isinstance(data, dict) else payload).get(key)
    return [dict(row) for row in rows] if isinstance(rows, list) else []


def _latest_price(quote: dict[str, Any]) -> float | None:
    regular = as_float(quote.get("last_trade_price"))
    extended = as_float(quote.get("last_non_reg_trade_price"))
    if extended is None:
        return regular
    if regular is None:
        return extended
    return extended if str(quote.get("venue_last_non_reg_trade_time") or "") > str(quote.get("venue_last_trade_time") or "") else regular


def _quote_change_abs(quote: dict[str, Any], current: float | None) -> float | None:
    previous = as_float(quote.get("adjusted_previous_close") or quote.get("previous_close"))
    return current - previous if current is not None and previous is not None else None


def _quote_change_pct(quote: dict[str, Any], current: float | None) -> float | None:
    previous = as_float(quote.get("adjusted_previous_close") or quote.get("previous_close"))
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous * 100
