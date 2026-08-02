"""PostgreSQL broker snapshots, advisory recommendations, and paper orders."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from psycopg.types.json import Jsonb

from investment_panel.database.instruments import canonical_symbol, reconcile_instrument
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


class BrokerRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def sync_snapshot(self, snapshot: Any) -> dict[str, Any]:
        status = snapshot.status
        ingestion = IngestionRepository(self.runtime)
        ingestion.register_source(
            status.provider,
            name=status.provider.upper(),
            family="broker",
            kind="broker_account",
            capabilities={capability: True for capability in status.capabilities},
        )
        if status.status == "disabled":
            ingestion.set_source_enabled(status.provider, False)
        account_ids: dict[str, int] = {}
        with ingestion.run(status.provider, "broker_sync", started_at=_aware(status.checked_at)) as ingestion_run:
            run_id = ingestion_run.id
            with self.runtime.transaction(JOB_PROFILE) as connection:
                connection.execute(
                    "INSERT INTO app.setting (key, value, updated_at) VALUES (%s, %s, now()) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                    [
                        f"broker_status:{status.provider}",
                        Jsonb({
                            "provider": status.provider, "checked_at": _aware(status.checked_at).isoformat(),
                            "status": status.status, "health": status.health, "detail": status.detail,
                            "account_id": status.account_id, "account_mode": status.account_mode,
                            "last_data_at": _iso(status.last_data_at), "latency_ms": status.latency_ms,
                            "capabilities": list(status.capabilities),
                        }),
                    ],
                )
                if status.status == "ok":
                    for account in snapshot.accounts:
                        account_key = str(account.get("account_id") or status.account_id or "UNKNOWN")
                        row = connection.execute(
                            """
                            INSERT INTO raw.broker_account_snapshot
                                (source_id, ingest_run_id, account_key, observed_at, currency,
                                 net_liquidation, buying_power, cash_balance, details)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (source_id, account_key, observed_at) DO UPDATE
                            SET details = EXCLUDED.details RETURNING id
                            """,
                            [
                                status.provider, run_id, account_key,
                                _aware(account.get("updated_at") or status.last_data_at or status.checked_at),
                                account.get("currency") or "USD", account.get("net_liquidation"),
                                account.get("buying_power"), account.get("cash"), Jsonb(_jsonable(dict(account.get("raw") or account))),
                            ],
                        ).fetchone()
                        account_ids[account_key] = int(row["id"])
                    for position in snapshot.positions:
                        account_key = str(position.get("account_id") or status.account_id or "UNKNOWN")
                        account_snapshot_id = account_ids.get(account_key)
                        if account_snapshot_id is None:
                            continue
                        try:
                            symbol = canonical_symbol(position.get("symbol"))
                        except ValueError:
                            continue
                        instrument_id = reconcile_instrument(
                            connection,
                            symbol,
                            name=position.get("name") or symbol,
                            asset_class=position.get("asset_class"),
                            category="broker-position",
                        )
                        connection.execute(
                            """
                            INSERT INTO raw.broker_position_snapshot
                                (account_snapshot_id, instrument_id, quantity, average_cost,
                                 market_price, market_value, unrealized_pnl, details)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (account_snapshot_id, instrument_id) DO UPDATE
                            SET quantity = EXCLUDED.quantity, average_cost = EXCLUDED.average_cost,
                                market_price = EXCLUDED.market_price, market_value = EXCLUDED.market_value,
                                unrealized_pnl = EXCLUDED.unrealized_pnl, details = EXCLUDED.details
                            """,
                            [
                                account_snapshot_id, instrument_id, position.get("quantity") or 0,
                                position.get("average_cost") or position.get("avg_cost"), position.get("market_price"),
                                position.get("market_value"), position.get("unrealized_pnl"),
                                Jsonb(_jsonable(dict(position.get("raw") or position))),
                            ],
                        )
                    for activity_type, rows in (("order", snapshot.orders), ("fill", snapshot.fills)):
                        for activity in rows:
                            activity_key = str(activity.get(f"{activity_type}_id") or activity.get("order_id") or "")
                            if not activity_key:
                                continue
                            raw_symbol = activity.get("symbol")
                            instrument_id = None
                            if raw_symbol:
                                try:
                                    instrument_id = reconcile_instrument(
                                        connection, raw_symbol, category="broker-activity"
                                    )
                                except ValueError:
                                    instrument_id = None
                            connection.execute(
                                """
                                INSERT INTO raw.broker_activity
                                    (source_id, ingest_run_id, account_key, activity_key, activity_type,
                                     instrument_id, occurred_at, side, quantity, price, status, details)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (source_id, activity_key, activity_type) DO UPDATE
                                SET status = EXCLUDED.status, details = EXCLUDED.details
                                """,
                                [
                                    status.provider, run_id, activity.get("account_id") or status.account_id or "UNKNOWN",
                                    activity_key, activity_type, instrument_id,
                                    _aware(activity.get("filled_at") or activity.get("submitted_at") or activity.get("updated_at") or status.checked_at),
                                    activity.get("side"), activity.get("quantity"), activity.get("price") or activity.get("limit_price"),
                                    activity.get("status"), Jsonb(_jsonable(dict(activity.get("raw") or activity))),
                                ],
                            )
            quote_count = ingestion.store_quotes(run_id, status.provider, snapshot.market_snapshots)
            final_status = "succeeded" if status.status == "ok" else "partial" if snapshot.market_snapshots else "skipped"
            ingestion_run.finish(
                final_status,
                item_count=len(snapshot.accounts) + len(snapshot.positions) + len(snapshot.orders) + len(snapshot.fills) + quote_count,
                instrument_count=len(snapshot.positions),
                failure_detail=None if status.status == "ok" else status.detail,
                summary={"provider_status": status.status, "scanner_signal_count": len(snapshot.scanner_signals)},
            )
        return {
            "provider": status.provider,
            "status": status.status,
            "accounts": len(snapshot.accounts),
            "positions": len(snapshot.positions),
            "market_snapshots": len(snapshot.market_snapshots),
            "run_id": str(run_id),
        }

def broker_status_rows(runtime: DatabaseRuntime) -> list[dict[str, Any]]:
    with runtime.read() as connection:
        rows = connection.execute(
            "SELECT value FROM app.setting WHERE key LIKE 'broker_status:%' ORDER BY key"
        ).fetchall()
    return [dict(row["value"]) for row in rows]


def _aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _iso(value: Any) -> str | None:
    return _aware(value).isoformat() if value else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return _aware(value).isoformat()
    return value
