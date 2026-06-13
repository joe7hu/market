"""Broker read-model accessors."""

from __future__ import annotations
from typing import Any, Protocol
from investment_panel.core.db import db, init_db, json_dumps, query_rows

from investment_panel.core.brokers.coerce import parse_json
from investment_panel.core.brokers.ibkr import ibkr_health



def effective_portfolio_rows(con: Any) -> list[dict[str, Any]]:
    health = ibkr_health(con)
    if health["usable"]:
        return [
            {
                "symbol": row.get("symbol"),
                "quantity": row.get("quantity"),
                "avg_cost": row.get("average_cost"),
                "average_cost": row.get("average_cost"),
                "market_price": row.get("market_price"),
                "market_value": row.get("market_value"),
                "unrealized_pnl": row.get("unrealized_pnl"),
                "source": "ibkr",
                "provider": row.get("provider"),
                "account_id": row.get("account_id"),
                "updated_at": row.get("updated_at"),
                "asset_class": row.get("asset_class"),
            }
            for row in query_rows(
                con,
                """
                SELECT provider, account_id, symbol, asset_class, quantity, average_cost,
                       market_price, market_value, unrealized_pnl, updated_at
                FROM broker_positions
                WHERE provider = 'ibkr'
                ORDER BY symbol
                """,
            )
        ]
    stale_rows = query_rows(
        con,
        """
        SELECT provider, account_id, symbol, asset_class, quantity, average_cost,
               market_price, market_value, unrealized_pnl, updated_at
        FROM broker_positions
        WHERE provider = 'ibkr'
        ORDER BY symbol
        """,
    )
    if stale_rows:
        return [
            {
                "symbol": row.get("symbol"),
                "quantity": row.get("quantity"),
                "avg_cost": row.get("average_cost"),
                "average_cost": row.get("average_cost"),
                "market_price": row.get("market_price"),
                "market_value": row.get("market_value"),
                "unrealized_pnl": row.get("unrealized_pnl"),
                "source": "ibkr_stale",
                "provider": row.get("provider"),
                "account_id": row.get("account_id"),
                "updated_at": row.get("updated_at"),
                "asset_class": row.get("asset_class"),
            }
            for row in stale_rows
        ]
    return [
        {**row, "source": "manual"}
        for row in query_rows(
            con,
            """
            SELECT symbol, quantity, avg_cost, avg_cost AS average_cost, purchase_date,
                   CASE
                       WHEN purchase_date IS NULL THEN NULL
                       ELSE date_diff('day', purchase_date, current_date)
                   END AS holding_days,
                   CASE
                       WHEN purchase_date IS NULL THEN 'unknown'
                       WHEN date_diff('day', purchase_date, current_date) > 365 THEN 'long_term'
                       ELSE 'short_term'
                   END AS tax_lot_term,
                   notes
            FROM portfolio_positions
            ORDER BY symbol
            """,
        )
    ]




def broker_status_rows(con: Any) -> list[dict[str, Any]]:
    return [_compact_empty_fields(decode_broker_row(row)) for row in query_rows(con, "SELECT * FROM broker_provider_status ORDER BY provider")]




def _compact_empty_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}




def broker_accounts(con: Any) -> list[dict[str, Any]]:
    return query_rows(con, "SELECT * FROM broker_accounts ORDER BY provider, account_id")




def broker_positions(con: Any) -> list[dict[str, Any]]:
    return query_rows(con, "SELECT * FROM broker_positions ORDER BY provider, account_id, symbol")




def broker_market_snapshots(con: Any) -> list[dict[str, Any]]:
    return query_rows(
        con,
        """
        SELECT *
        FROM broker_market_snapshots
        QUALIFY row_number() OVER (PARTITION BY provider, symbol ORDER BY observed_at DESC) = 1
        ORDER BY provider, symbol
        """,
    )




def broker_scanner_signals(con: Any) -> list[dict[str, Any]]:
    return [decode_broker_row(row) for row in query_rows(con, "SELECT * FROM broker_scanner_signals ORDER BY observed_at DESC, rank ASC NULLS LAST LIMIT 200")]




def agent_recommendations(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(con, "SELECT * FROM broker_agent_recommendations ORDER BY status DESC, actionability_score DESC LIMIT 100")
    return [decode_broker_row(row) for row in rows]




def paper_orders(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(con, "SELECT * FROM broker_paper_orders ORDER BY created_at DESC LIMIT 100")
    return [decode_broker_row(row) for row in rows]




def decode_broker_row(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for key in ("capabilities", "raw", "metrics", "sizing", "portfolio_impact", "evidence", "blockers", "data_freshness", "paper_order_preview", "policy_checks", "preview", "audit_trail"):
        if key in decoded:
            decoded[key] = parse_json(decoded[key])
    return decoded
