"""Effective portfolio and broker-account health."""

from __future__ import annotations
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from investment_panel.core.db import json_dumps, query_rows, upsert_instrument

from investment_panel.core.decision.coerce import parse_dt



def effective_portfolio_by_symbol(con: Any) -> dict[str, dict[str, Any]]:
    """Use healthy IBKR positions as source of truth; otherwise preserve manual rows."""

    health = broker_account_health(con)
    if health.get("usable"):
        return {
            str(row.get("symbol") or "").upper(): {
                "symbol": row.get("symbol"),
                "quantity": row.get("quantity"),
                "avg_cost": row.get("average_cost"),
                "average_cost": row.get("average_cost"),
                "market_value": row.get("market_value"),
                "source": row.get("provider") or "ibkr",
            }
            for row in query_rows(
                con,
                """
                SELECT provider, symbol, quantity, average_cost, market_value
                FROM broker_positions
                WHERE provider = 'ibkr'
                """,
            )
        }
    stale_rows = query_rows(
        con,
        """
        SELECT provider, symbol, quantity, average_cost, market_value
        FROM broker_positions
        WHERE provider = 'ibkr'
        """,
    )
    if stale_rows:
        return {
            str(row.get("symbol") or "").upper(): {
                "symbol": row.get("symbol"),
                "quantity": row.get("quantity"),
                "avg_cost": row.get("average_cost"),
                "average_cost": row.get("average_cost"),
                "market_value": row.get("market_value"),
                "source": "ibkr_stale",
            }
            for row in stale_rows
        }
    return {
        str(row.get("symbol") or "").upper(): {**row, "source": "manual"}
        for row in query_rows(con, "SELECT symbol, quantity, avg_cost FROM portfolio_positions")
    }




def broker_account_health(con: Any) -> dict[str, Any]:
    rows = query_rows(con, "SELECT provider, checked_at, status, detail, last_data_at FROM broker_provider_status WHERE provider = 'ibkr' LIMIT 1")
    if not rows:
        return {"status": "missing", "usable": True, "detail": "IBKR broker source has not synced."}
    row = rows[0]
    status = str(row.get("status") or "missing")
    observed = parse_dt(row.get("last_data_at") or row.get("checked_at"))
    if observed and datetime.now(UTC) - observed > timedelta(minutes=15) and status == "ok":
        status = "stale_data"
    return {"status": status, "usable": status == "ok", "detail": row.get("detail")}
