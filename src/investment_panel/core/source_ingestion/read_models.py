"""Read models over canonical source tables."""

from __future__ import annotations

from typing import Any

from investment_panel.core.db import query_rows
from investment_panel.core.source_ingestion.canonical import ensure_canonical_sources
from investment_panel.core.source_ingestion.utils import decode_row, source_row_freshness

def source_registry_rows(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT
            r.*,
            latest.finished_at AS latest_run_at,
            latest.status AS latest_run_status,
            latest.failure_detail AS latest_failure_detail,
            coalesce(items.items_count, 0) AS items_count,
            coalesce(signals.tickers_count, 0) AS tickers_count,
            coalesce(signals.signals_count, 0) AS signals_count,
            coalesce(signals.needs_market_context_count, 0) AS needs_market_context_count
        FROM source_registry r
        LEFT JOIN (
            SELECT source_id, finished_at, status, failure_detail
            FROM source_runs
            QUALIFY row_number() OVER (PARTITION BY source_id ORDER BY finished_at DESC NULLS LAST, started_at DESC NULLS LAST) = 1
        ) latest ON latest.source_id = r.source_id
        LEFT JOIN (
            SELECT source_id, count(*) AS items_count FROM source_items GROUP BY source_id
        ) items ON items.source_id = r.source_id
        LEFT JOIN (
            SELECT source_id, count(*) AS signals_count, count(DISTINCT symbol) AS tickers_count,
                   sum(CASE WHEN needs_market_context THEN 1 ELSE 0 END) AS needs_market_context_count
            FROM ticker_source_signals
            GROUP BY source_id
        ) signals ON signals.source_id = r.source_id
        ORDER BY r.enabled DESC, items_count DESC, r.source_name
        """,
    )
    return [decode_row(row) | {"freshness": source_row_freshness(row)} for row in rows]


def source_item_rows(con: Any, source_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql = """
        SELECT i.*, r.source_name
        FROM source_items i
        LEFT JOIN source_registry r ON r.source_id = i.source_id
    """
    params: list[Any] = []
    if source_id:
        sql += " WHERE i.source_id = ?"
        params.append(source_id)
    sql += " ORDER BY i.observed_at DESC NULLS LAST, i.published_at DESC NULLS LAST LIMIT ?"
    params.append(limit)
    return [decode_row(row) for row in query_rows(con, sql, params)]


def source_run_rows(con: Any, source_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql = """
        SELECT runs.*, registry.source_name, registry.source_family
        FROM source_runs runs
        LEFT JOIN source_registry registry ON registry.source_id = runs.source_id
    """
    params: list[Any] = []
    if source_id:
        sql += " WHERE runs.source_id = ?"
        params.append(source_id)
    sql += " ORDER BY runs.finished_at DESC NULLS LAST, runs.started_at DESC NULLS LAST LIMIT ?"
    params.append(limit)
    return [decode_row(row) for row in query_rows(con, sql, params)]


def ticker_source_signal_rows(con: Any, symbol: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
    sql = """
        SELECT s.*, r.source_name, r.source_family, i.title, i.url
        FROM ticker_source_signals s
        LEFT JOIN source_registry r ON r.source_id = s.source_id
        LEFT JOIN source_items i ON i.id = s.source_item_id
    """
    params: list[Any] = []
    if symbol:
        sql += " WHERE upper(s.symbol) = ?"
        params.append(symbol.upper())
    sql += " ORDER BY s.observed_at DESC NULLS LAST LIMIT ?"
    params.append(limit)
    return [decode_row(row) for row in query_rows(con, sql, params)]


def source_detail_payload(con: Any, source_id: str) -> dict[str, Any]:
    ensure_canonical_sources(con)
    source = next((row for row in source_registry_rows(con) if row.get("source_id") == source_id), None)
    if not source:
        return {"source_id": source_id, "found": False, "items": [], "signals": []}
    return {
        "source": source,
        "runs": source_run_rows(con, source_id, limit=25),
        "items": source_item_rows(con, source_id, limit=100),
        "signals": [row for row in ticker_source_signal_rows(con, limit=500) if row.get("source_id") == source_id],
    }
