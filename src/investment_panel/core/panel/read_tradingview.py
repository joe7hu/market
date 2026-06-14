"""TradingView personal-context read accessors (search, watchlists, alerts, chart state)."""

from __future__ import annotations
from typing import Any
from investment_panel.core.db import query_rows

from investment_panel.core.panel.coerce import decode_fields



def tradingview_symbol_search(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, query, observed_at, symbol, description, instrument_type,
               exchange, country, currency, source, raw
        FROM tradingview_symbol_search
        ORDER BY observed_at DESC, query, symbol
        LIMIT 300
        """,
    )
    return [decode_fields(row, ("raw",)) for row in rows]




def tradingview_watchlists(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, observed_at, name, color, symbol_count, symbols, source, raw
        FROM tradingview_watchlists
        ORDER BY observed_at DESC, color NULLS LAST, name
        LIMIT 200
        """,
    )
    return [decode_fields(row, ("symbols", "raw")) for row in rows]




def tradingview_alerts(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, observed_at, name, symbol, alert_type, condition, value,
               active, status, fired_at, source, raw
        FROM tradingview_alerts
        ORDER BY observed_at DESC, fired_at DESC NULLS LAST, symbol
        LIMIT 300
        """,
    )
    return [decode_fields(row, ("raw",)) for row in rows]




def tradingview_chart_state(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT id, observed_at, layout_id, symbol, interval, url, source, raw
        FROM tradingview_chart_state
        ORDER BY observed_at DESC
        LIMIT 50
        """,
    )
    return [decode_fields(row, ("raw",)) for row in rows]
