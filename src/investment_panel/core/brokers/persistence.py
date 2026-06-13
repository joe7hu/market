"""Broker snapshot and read-model persistence."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4
from investment_panel.core.db import db, init_db, json_dumps, query_rows
from investment_panel.core.instruments import infer_asset_class, normalize_symbol

from investment_panel.core.brokers.coerce import stable_id



def persist_broker_snapshot(con: Any, snapshot: BrokerSnapshot) -> None:
    status = snapshot.status
    con.execute(
        """
        INSERT OR REPLACE INTO broker_provider_status
        (provider, checked_at, status, health, detail, account_id, account_mode,
         session_started_at, last_data_at, latency_ms, capabilities, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            status.provider,
            status.checked_at,
            status.status,
            status.health,
            status.detail,
            status.account_id,
            status.account_mode,
            status.session_started_at,
            status.last_data_at,
            status.latency_ms,
            json_dumps(status.capabilities),
            json_dumps(status.raw),
        ],
    )
    record_source_health(con, f"broker:{status.provider}", status.status, status.detail)
    record_provider_run(con, status.provider, "broker_sync", status.checked_at, status.status, status.detail, status.raw)

    if status.status == "account_mode_mismatch":
        clear_broker_account_read_models(con, status.provider)
        return
    if status.status == "quote_only":
        clear_broker_account_read_models(con, status.provider)
        persist_broker_quote_rows(con, status, snapshot.market_snapshots)
        return
    if status.status != "ok":
        return

    clear_broker_account_read_models(con, status.provider)

    for account in snapshot.accounts:
        con.execute(
            """
            INSERT OR REPLACE INTO broker_accounts
            (provider, account_id, account_mode, currency, cash, buying_power, net_liquidation,
             margin_requirement, excess_liquidity, day_pnl, total_pnl, updated_at, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                status.provider,
                account.get("account_id") or status.account_id or "UNKNOWN",
                account.get("account_mode") or status.account_mode,
                account.get("currency", "USD"),
                account.get("cash"),
                account.get("buying_power"),
                account.get("net_liquidation"),
                account.get("margin_requirement"),
                account.get("excess_liquidity"),
                account.get("day_pnl"),
                account.get("total_pnl"),
                account.get("updated_at") or status.last_data_at or status.checked_at,
                json_dumps(account.get("raw") or account),
            ],
        )
    for position in snapshot.positions:
        symbol = normalize_symbol(str(position.get("symbol") or ""))
        if not symbol:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO broker_positions
            (provider, account_id, symbol, asset_class, quantity, average_cost, market_price,
             market_value, unrealized_pnl, realized_pnl, updated_at, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                status.provider,
                position.get("account_id") or status.account_id or "UNKNOWN",
                symbol,
                position.get("asset_class") or infer_asset_class(symbol),
                position.get("quantity"),
                position.get("average_cost") or position.get("avg_cost"),
                position.get("market_price"),
                position.get("market_value"),
                position.get("unrealized_pnl"),
                position.get("realized_pnl"),
                position.get("updated_at") or status.last_data_at or status.checked_at,
                json_dumps(position.get("raw") or position),
            ],
        )
        con.execute(
            """
            INSERT INTO instruments (symbol, name, asset_class, sector, industry, category, source)
            SELECT ?, ?, ?, NULL, NULL, 'broker-position', ?
            WHERE NOT EXISTS (SELECT 1 FROM instruments WHERE symbol = ?)
            """,
            [symbol, position.get("name") or symbol, position.get("asset_class") or infer_asset_class(symbol), status.provider, symbol],
        )
    for order in snapshot.orders:
        con.execute(
            """
            INSERT OR REPLACE INTO broker_orders
            (provider, account_id, order_id, symbol, side, order_type, quantity, limit_price,
             status, submitted_at, updated_at, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                status.provider,
                order.get("account_id") or status.account_id or "UNKNOWN",
                str(order.get("order_id") or uuid4().hex),
                normalize_symbol(str(order.get("symbol") or "")),
                order.get("side"),
                order.get("order_type"),
                order.get("quantity"),
                order.get("limit_price"),
                order.get("status"),
                order.get("submitted_at"),
                order.get("updated_at") or status.checked_at,
                json_dumps(order.get("raw") or order),
            ],
        )
    for fill in snapshot.fills:
        con.execute(
            """
            INSERT OR REPLACE INTO broker_fills
            (provider, account_id, fill_id, order_id, symbol, side, quantity, price, filled_at, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                status.provider,
                fill.get("account_id") or status.account_id or "UNKNOWN",
                str(fill.get("fill_id") or uuid4().hex),
                str(fill.get("order_id") or ""),
                normalize_symbol(str(fill.get("symbol") or "")),
                fill.get("side"),
                fill.get("quantity"),
                fill.get("price"),
                fill.get("filled_at") or status.checked_at,
                json_dumps(fill.get("raw") or fill),
            ],
        )
    persist_broker_quote_rows(con, status, snapshot.market_snapshots)
    run_id = stable_id(f"{status.provider}:{status.checked_at.isoformat()}:scanner")
    for signal in snapshot.scanner_signals:
        symbol = normalize_symbol(str(signal.get("symbol") or ""))
        if not symbol:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO broker_scanner_signals
            (provider, run_id, symbol, observed_at, signal_type, rank, score, metrics, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                status.provider,
                signal.get("run_id") or run_id,
                symbol,
                signal.get("observed_at") or status.checked_at,
                signal.get("signal_type") or "scanner",
                signal.get("rank"),
                signal.get("score"),
                json_dumps(signal.get("metrics") or {}),
                json_dumps(signal.get("raw") or signal),
            ],
        )




def persist_broker_quote_rows(con: Any, status: ProviderStatus, market_snapshots: list[dict[str, Any]]) -> None:
    for quote in market_snapshots:
        symbol = normalize_symbol(str(quote.get("symbol") or ""))
        if not symbol:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO broker_market_snapshots
            (provider, symbol, observed_at, bid, ask, last, close, volume, entitlement_status, data_status, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                status.provider,
                symbol,
                quote.get("observed_at") or status.last_data_at or status.checked_at,
                quote.get("bid"),
                quote.get("ask"),
                quote.get("last"),
                quote.get("close"),
                quote.get("volume"),
                quote.get("entitlement_status", "ok"),
                quote.get("data_status", "fresh"),
                json_dumps(quote.get("raw") or quote),
            ],
        )
        if quote.get("last") is not None:
            con.execute(
                """
                INSERT OR REPLACE INTO quotes_intraday
                (symbol, observed_at, price, change_pct, change_abs, currency, source, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    symbol,
                    quote.get("observed_at") or status.checked_at,
                    quote.get("last"),
                    quote.get("change_pct"),
                    quote.get("change_abs"),
                    quote.get("currency", "USD"),
                    f"broker:{status.provider}",
                    json_dumps(quote.get("raw") or quote),
                ],
            )




def clear_broker_account_read_models(con: Any, provider: str) -> None:
    con.execute("DELETE FROM broker_accounts WHERE provider = ?", [provider])
    con.execute("DELETE FROM broker_positions WHERE provider = ?", [provider])
    con.execute("DELETE FROM broker_orders WHERE provider = ?", [provider])
    con.execute("DELETE FROM broker_fills WHERE provider = ?", [provider])




def record_source_health(con: Any, source: str, status: str, detail: str) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO source_health (source, checked_at, status, detail, source_url)
        VALUES (?, ?, ?, ?, ?)
        """,
        [source, datetime.now(UTC), status, detail, source],
    )




def record_provider_run(con: Any, provider: str, capability: str, finished_at: datetime, status: str, detail: str, raw: dict[str, Any]) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO provider_runs
        (id, provider, capability, started_at, finished_at, status, detail, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [stable_id(f"{provider}:{capability}:{finished_at.isoformat()}"), provider, capability, finished_at, finished_at, status, detail, json_dumps(raw)],
    )
