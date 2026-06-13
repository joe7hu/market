"""Small SQL and option-contract helpers for options radar refreshes."""

from __future__ import annotations

from typing import Any

from investment_panel.core.options_radar_coerce import _coalesce_number, _normalize_symbol, _number
from investment_panel.core.source_ingestion.utils import stable_id


def _symbol_filter(symbols: list[str] | None, *, table_alias: str, column: str = "symbol") -> dict[str, Any]:
    clean = [_normalize_symbol(symbol) for symbol in symbols or [] if symbol]
    if not clean:
        return {"sql": "", "params": []}
    placeholders = ", ".join(["?"] * len(clean))
    return {"sql": f"AND {table_alias}.{column} IN ({placeholders})", "params": clean}


def _source_filter(source: str | None, *, table_alias: str, column: str = "source") -> dict[str, Any]:
    if not source:
        return {"sql": "", "params": []}
    return {"sql": f"AND {table_alias}.{column} = ?", "params": [source]}


def _contract_id(ticker: str, expiration: Any, strike: float | None, option_type: str, provider_symbol: Any) -> str:
    if provider_symbol:
        return str(provider_symbol)
    return f"{ticker}:{expiration}:{strike:g}:{option_type}" if strike is not None else stable_id(ticker, expiration, option_type)


def _premium_mid(row: dict[str, Any], raw: dict[str, Any]) -> float | None:
    mid = _number(row.get("mid")) or _coalesce_number(raw, "mid", "mark")
    if mid is not None:
        return mid
    bid = _number(row.get("bid")) or _coalesce_number(raw, "bid")
    ask = _number(row.get("ask")) or _coalesce_number(raw, "ask")
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def _spread_pct(bid: float | None, ask: float | None, mid: float | None) -> float | None:
    if bid is None or ask is None or mid is None or mid <= 0:
        return None
    return max(0.0, (ask - bid) / mid)


def _required_move_pct(option_type: str, underlying: float | None, required_price: float) -> float | None:
    if underlying is None or underlying <= 0:
        return None
    if option_type == "put":
        return max(0.0, (underlying - required_price) / underlying)
    return max(0.0, (required_price - underlying) / underlying)


def _diff(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def _bounded_abs_delta(value: Any) -> float | None:
    delta = _number(value)
    if delta is None:
        return None
    return max(0.0, min(1.0, abs(delta)))
