"""Read models over canonical source tables."""

from __future__ import annotations

from typing import Any

from investment_panel.core.db import query_rows
from investment_panel.core.source_ingestion.canonical import ensure_canonical_sources
from investment_panel.core.source_ingestion.utils import decode_row, source_row_freshness

EMPTY_VALUES = (None, "", [], {})


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
    return [_compact_row(decode_row(row) | {"freshness": source_row_freshness(row)}) for row in rows]


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
    return [_compact_row(decode_row(row)) for row in query_rows(con, sql, params)]


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
    rows = []
    for row in query_rows(con, sql, params):
        decoded = decode_row(row)
        decoded["source_name"] = decoded.get("source_name") or decoded.get("source_id")
        decoded["source_family"] = decoded.get("source_family") or decoded.get("capability") or "source_run"
        rows.append(_compact_row(decoded))
    return rows


def ticker_source_signal_rows(
    con: Any,
    symbol: str | None = None,
    source_id: str | None = None,
    limit: int | None = 300,
) -> list[dict[str, Any]]:
    sql = """
        SELECT s.*, r.source_name, r.source_family, i.title, i.url
        FROM ticker_source_signals s
        LEFT JOIN source_registry r ON r.source_id = s.source_id
        LEFT JOIN source_items i ON i.id = s.source_item_id
    """
    params: list[Any] = []
    filters: list[str] = []
    if symbol:
        filters.append("upper(s.symbol) = ?")
        params.append(symbol.upper())
    if source_id:
        filters.append("s.source_id = ?")
        params.append(source_id)
    if filters:
        sql += " WHERE " + " AND ".join(filters)
    sql += " ORDER BY s.observed_at DESC NULLS LAST"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = []
    for row in query_rows(con, sql, params):
        decoded = decode_row(row)
        if not decoded.get("url"):
            decoded["url"] = _first_url(decoded.get("evidence_refs"))
        rows.append(_compact_row(decoded))
    return rows


def source_ticker_ranking_rows(con: Any, limit: int = 250) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        WITH joined AS (
            SELECT
                upper(s.symbol) AS symbol,
                s.observed_at,
                lower(coalesce(s.sentiment, 'neutral')) AS sentiment,
                s.confidence,
                s.needs_market_context,
                coalesce(r.source_name, s.source_id) AS source_name,
                coalesce(i.title, s.thesis) AS title
            FROM ticker_source_signals s
            LEFT JOIN source_registry r ON r.source_id = s.source_id
            LEFT JOIN source_items i ON i.id = s.source_item_id
            WHERE s.symbol IS NOT NULL AND trim(s.symbol) <> ''
        ),
        grouped AS (
            SELECT
                symbol,
                count(*) AS signal_count,
                count(DISTINCT source_name) AS source_count,
                sum(CASE WHEN sentiment = 'bullish' THEN 1 ELSE 0 END) AS bullish_count,
                sum(CASE WHEN sentiment = 'bearish' THEN 1 ELSE 0 END) AS bearish_count,
                sum(CASE WHEN sentiment NOT IN ('bullish', 'bearish') THEN 1 ELSE 0 END) AS neutral_count,
                avg(confidence) AS avg_confidence,
                sum(CASE WHEN needs_market_context THEN 1 ELSE 0 END) AS needs_market_context_count,
                string_agg(DISTINCT source_name, '||' ORDER BY source_name) AS source_names
            FROM joined
            GROUP BY symbol
        ),
        latest AS (
            SELECT symbol, observed_at, source_name, title
            FROM joined
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY observed_at DESC NULLS LAST) = 1
        )
        SELECT
            g.symbol,
            (g.signal_count * 10 + g.source_count * 5 + (g.bullish_count - g.bearish_count)) AS rank_score,
            g.signal_count,
            g.source_count,
            (g.bullish_count - g.bearish_count) AS net_consensus,
            g.bullish_count,
            g.bearish_count,
            g.neutral_count,
            round(g.avg_confidence, 3) AS avg_confidence,
            latest.observed_at AS latest_at,
            latest.source_name AS latest_source,
            latest.title AS latest_title,
            g.source_names,
            g.needs_market_context_count
        FROM grouped g
        LEFT JOIN latest ON latest.symbol = g.symbol
        ORDER BY rank_score DESC, signal_count DESC, source_count DESC, latest_at DESC NULLS LAST
        LIMIT ?
        """,
        [limit],
    )
    rankings: list[dict[str, Any]] = []
    for row in rows:
        decoded = dict(row)
        source_names = str(decoded.get("source_names") or "")
        decoded["source_names"] = [name for name in source_names.split("||") if name][:8]
        rankings.append(_compact_row(decoded))
    return rankings


def source_detail_payload(con: Any, source_id: str, ensure_sources: bool = True) -> dict[str, Any]:
    if ensure_sources:
        ensure_canonical_sources(con)
    source = next((row for row in source_registry_rows(con) if row.get("source_id") == source_id), None)
    if not source:
        return {"source_id": source_id, "found": False, "items": [], "signals": []}
    return {
        "source": source,
        "runs": source_run_rows(con, source_id, limit=25),
        "items": source_item_rows(con, source_id, limit=100),
        "signals": ticker_source_signal_rows(con, source_id=source_id, limit=500),
    }


def _first_url(value: Any) -> str:
    refs = value if isinstance(value, list) else []
    for ref in refs:
        text = str(ref or "").strip()
        if text.startswith(("http://", "https://", "file://", "/")):
            return text
    return ""


def _compact_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value not in EMPTY_VALUES}
