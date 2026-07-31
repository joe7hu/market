"""Bounded PostgreSQL pages for review-only panel collections."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.agents import AgentRepository
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.panel_models import AGENT_MODELS, MODEL_ALIASES, QUERY_POLICIES


MAX_REVIEW_SNAPSHOT_ROWS = 5_000


def load_postgres_table_page(
    config: dict[str, Any],
    table_name: str,
    *,
    limit: int,
    snapshot_at: datetime,
    after: tuple[Any, str] | None = None,
) -> tuple[list[dict[str, Any]], int, tuple[Any, str] | None]:
    runtime = runtime_for_config(config)
    if table_name in AGENT_MODELS:
        after_time = after[0] if after else None
        if isinstance(after_time, str):
            after_time = datetime.fromisoformat(after_time)
        rows, total = AgentRepository(runtime).rows_page(
            table_name,
            limit=limit,
            created_before=snapshot_at,
            after_created_at=after_time,
            after_id=after[1] if after else None,
        )
        next_after = (rows[-1]["created_at"], rows[-1]["request_id"]) if len(rows) == limit else None
        return rows, total, next_after
    policy = QUERY_POLICIES.get(MODEL_ALIASES.get(table_name) or table_name)
    if policy is None or policy.custom_loader:
        raise ValueError(f"model does not support bounded paging: {table_name}")
    if after:
        snapshot_id, offset_text = after
        try:
            snapshot_uuid = UUID(str(snapshot_id))
            offset = int(offset_text)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid review snapshot cursor") from exc
        if offset < 0 or offset > MAX_REVIEW_SNAPSHOT_ROWS:
            raise ValueError("invalid review snapshot cursor")
        with runtime.read() as connection:
            stored = connection.execute(
                """
                SELECT rows FROM app.review_page_snapshot
                WHERE id = %s AND model_name = %s AND expires_at > now()
                """,
                [snapshot_uuid, table_name],
            ).fetchone()
        if stored is None:
            raise ValueError("review snapshot cursor expired")
        snapshot_rows = list(stored["rows"] or [])
    else:
        with runtime.transaction() as connection:
            connection.execute(
                "DELETE FROM app.review_page_snapshot WHERE expires_at <= now()"
            )
            snapshot_rows = [
                _jsonable(dict(row))
                for row in connection.execute(
                    f"SELECT * FROM ({policy.query}) AS bounded_model LIMIT %s",
                    [MAX_REVIEW_SNAPSHOT_ROWS + 1],
                ).fetchall()
            ]
            if len(snapshot_rows) > MAX_REVIEW_SNAPSHOT_ROWS:
                raise ValueError(
                    "learning collection exceeds the bounded review snapshot limit"
                )
            stored = connection.execute(
                """
                INSERT INTO app.review_page_snapshot
                    (model_name, expires_at, rows)
                VALUES (%s, now() + interval '30 minutes', %s)
                RETURNING id
                """,
                [table_name, Jsonb(snapshot_rows)],
            ).fetchone()
        snapshot_id = str(stored["id"])
        offset = 0
    page = snapshot_rows[offset:offset + limit]
    next_offset = offset + len(page)
    next_after = (
        (str(snapshot_id), str(next_offset))
        if next_offset < len(snapshot_rows)
        else None
    )
    return page, len(snapshot_rows), next_after


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    return value
