"""Small DB query/id helpers (symbol & source filters, contract ids)."""

from __future__ import annotations

from typing import Any

from investment_panel.core.db import (query_rows)
from investment_panel.core.source_ingestion.utils import (stable_id)
from investment_panel.core.options_radar.coerce import (_iso, _json, _normalize_symbol, _number)
from investment_panel.core.options_radar.constants import (DEFAULT_STRATEGY_PARAMETERS)
from investment_panel.core.options_radar.registration import (register_default_strategy)

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


def _strategy_parameters(con: Any, strategy_version: str) -> dict[str, Any]:
    rows = query_rows(con, "SELECT parameters FROM option_strategy_versions WHERE strategy_version = ?", [strategy_version])
    if not rows:
        register_default_strategy(con, strategy_version)
        return dict(DEFAULT_STRATEGY_PARAMETERS)
    return {**DEFAULT_STRATEGY_PARAMETERS, **_json(rows[0].get("parameters"))}


def _compact_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_time": _iso(row.get("snapshot_time")),
        "ticker": _normalize_symbol(row.get("ticker")),
        "contract_id": row.get("contract_id"),
        "underlying_price": _number(row.get("underlying_price")),
        "expiration": str(row.get("expiration")),
        "strike": _number(row.get("strike")),
        "option_type": row.get("option_type"),
        "mid": _number(row.get("mid")),
        "iv": _number(row.get("iv")),
        "delta": _number(row.get("delta")),
        "spread_pct": _number(row.get("spread_pct")),
        "volume": _number(row.get("volume")),
        "open_interest": _number(row.get("open_interest")),
    }
