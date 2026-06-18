"""TradingView symbol identity helpers."""

from __future__ import annotations

import json
from typing import Any


def best_tradingview_symbol(symbol: str, rows: list[dict[str, Any]]) -> str:
    normalized = str(symbol or "").strip().upper()
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for index, row in enumerate(rows):
        exchange = _text(row.get("exchange"))
        row_symbol = _text(row.get("symbol") or row.get("ticker"))
        raw = _dict_from_value(row.get("raw"))
        raw_symbol = _text(raw.get("symbol"))
        explicit = raw_symbol if ":" in raw_symbol else row_symbol
        if not explicit:
            continue
        candidate_symbol = explicit.split(":")[-1].upper()
        if candidate_symbol != normalized:
            continue
        if ":" in explicit:
            tv_symbol = explicit.upper()
        elif exchange:
            tv_symbol = f"{exchange}:{row_symbol}".upper()
        else:
            continue
        if ":" not in tv_symbol:
            continue
        instrument_type = _text(row.get("instrument_type") or row.get("type") or raw.get("type")).lower()
        type_rank = 0 if instrument_type in {"stock", "dr", "fund", "etf"} else 10
        exchange_rank = {
            "NYSE": 0,
            "NASDAQ": 1,
            "AMEX": 2,
            "NYSEARCA": 3,
            "ARCA": 3,
            "OTC": 8,
            "BOATS": 20,
            "CBOE": 30,
            "FINRA": 40,
        }.get(exchange.upper(), 15)
        candidates.append(((type_rank, exchange_rank, index), tv_symbol))
    return min(candidates, default=((0, 0, 0), ""))[1]


def primary_exchange(tradingview_symbol: str) -> str:
    value = str(tradingview_symbol or "").strip().upper()
    return value.split(":", 1)[0] if ":" in value else ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _dict_from_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
