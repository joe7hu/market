"""PostgreSQL broker snapshots, advisory recommendations, and paper orders."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.analysis import AnalysisRepository
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
        run_id = ingestion.start_run(status.provider, "broker_sync", started_at=_aware(status.checked_at))
        account_ids: dict[str, int] = {}
        try:
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
                        symbol = str(position.get("symbol") or "").strip().upper()
                        if not symbol:
                            continue
                        instrument = connection.execute(
                            "INSERT INTO catalog.instrument (symbol, name, asset_class, category) "
                            "VALUES (%s, %s, %s, 'broker-position') "
                            "ON CONFLICT (symbol) DO UPDATE SET updated_at = now() RETURNING id",
                            [symbol, position.get("name") or symbol, position.get("asset_class") or "equity"],
                        ).fetchone()
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
                                account_snapshot_id, instrument["id"], position.get("quantity") or 0,
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
                            symbol = str(activity.get("symbol") or "").strip().upper()
                            instrument_id = None
                            if symbol:
                                instrument_id = connection.execute(
                                    "INSERT INTO catalog.instrument (symbol, name, asset_class, category) "
                                    "VALUES (%s, %s, 'equity', 'broker-activity') "
                                    "ON CONFLICT (symbol) DO UPDATE SET updated_at = now() RETURNING id",
                                    [symbol, symbol],
                                ).fetchone()["id"]
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
            ingestion.finish_run(
                run_id,
                final_status,
                item_count=len(snapshot.accounts) + len(snapshot.positions) + len(snapshot.orders) + len(snapshot.fills) + quote_count,
                instrument_count=len(snapshot.positions),
                failure_detail=None if status.status == "ok" else status.detail,
                summary={"provider_status": status.status, "scanner_signal_count": len(snapshot.scanner_signals)},
            )
        except Exception as exc:
            ingestion.finish_run(run_id, "failed", failure_detail=f"{type(exc).__name__}: {exc}")
            raise
        return {
            "provider": status.provider,
            "status": status.status,
            "accounts": len(snapshot.accounts),
            "positions": len(snapshot.positions),
            "market_snapshots": len(snapshot.market_snapshots),
            "run_id": str(run_id),
        }

    def build_recommendations(self, *, code_version: str = "working-tree") -> list[dict[str, Any]]:
        with self.runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT ON (decision.instrument_id)
                       decision.id, decision.decision_key, decision.as_of, decision.state,
                       decision.score, decision.reasons, decision.blockers,
                       instrument.symbol, option_decision.buy_under, option_decision.tier
                FROM analysis.decision decision
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                LEFT JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
                ORDER BY decision.instrument_id, decision.as_of DESC, decision.score DESC NULLS LAST
                """
            ).fetchall()
        recommendations = [
            {
                "recommendation_id": str(row["id"]),
                "id": str(row["id"]),
                "symbol": row["symbol"],
                "as_of": row["as_of"],
                "action": "review_entry" if row["state"] in {"FIRE", "SETUP"} else "monitor",
                "status": "actionable" if not row["blockers"] else "blocked",
                "actionability_score": row["score"],
                "thesis": "; ".join(row["reasons"] or []),
                "blockers": list(row["blockers"] or []),
                "paper_order_preview": {"side": "BUY", "order_type": "limit", "limit_price": row["buy_under"], "quantity": 1},
                "authority": "advisory_only",
                "tier": row["tier"],
            }
            for row in rows
        ]
        analysis = AnalysisRepository(self.runtime)
        run_id = analysis.start_run(
            "broker-recommendations",
            input_cutoff=datetime.now(UTC),
            code_version=code_version,
            inputs={"decision_ids": [row["recommendation_id"] for row in recommendations]},
        )
        publication_id = analysis.publish(
            run_id,
            "broker",
            {"agent_recommendations": recommendations},
            complete_run_summary={"recommendations": len(recommendations)},
        )
        return [{**row, "publication_id": str(publication_id)} for row in recommendations]

    def stage_paper_order(self, recommendation_id: str) -> dict[str, Any]:
        with self.runtime.transaction() as connection:
            row = connection.execute(
                """
                SELECT item.payload
                FROM app.publication publication
                JOIN app.publication_item item ON item.publication_id = publication.id
                WHERE publication.scope = 'broker' AND publication.status = 'published'
                  AND item.model_name = 'agent_recommendations'
                  AND (item.payload->>'recommendation_id' = %s OR item.payload->>'id' = %s)
                LIMIT 1
                """,
                [recommendation_id, recommendation_id],
            ).fetchone()
            if row is None:
                raise ValueError(f"recommendation not found: {recommendation_id}")
            recommendation = dict(row["payload"])
            symbol = str(recommendation["symbol"])
            instrument = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = %s", [symbol]).fetchone()
            blockers = list(recommendation.get("blockers") or [])
            preview = dict(recommendation.get("paper_order_preview") or {})
            status = "blocked" if blockers else "staged"
            order = connection.execute(
                """
                INSERT INTO app.paper_order
                    (decision_id, instrument_id, side, quantity, limit_price, status, policy_result)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                [
                    UUID(recommendation_id), instrument["id"], preview.get("side") or "BUY",
                    preview.get("quantity") or 1, preview.get("limit_price"), status,
                    Jsonb({"blockers": blockers, "authority": "paper_only", "preview": preview}),
                ],
            ).fetchone()
        return {"id": str(order["id"]), "status": status, "symbol": symbol, "blockers": blockers, "preview": preview}


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
