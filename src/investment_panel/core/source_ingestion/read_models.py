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


def ticker_source_signal_rows(con: Any, symbol: str | None = None, limit: int | None = 300) -> list[dict[str, Any]]:
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
    groups: dict[str, dict[str, Any]] = {}
    for row in ticker_source_signal_rows(con, limit=None):
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        group = groups.setdefault(
            symbol,
            {
                "symbol": symbol,
                "signals": 0,
                "sources": set(),
                "sentiments": {},
                "needs_market_context": 0,
                "confidence_values": [],
                "latest": {},
            },
        )
        group["signals"] += 1
        source_name = str(row.get("source_name") or row.get("source_id") or "").strip()
        if source_name:
            group["sources"].add(source_name)
        sentiment = str(row.get("sentiment") or "neutral").lower()
        group["sentiments"][sentiment] = group["sentiments"].get(sentiment, 0) + 1
        if row.get("needs_market_context"):
            group["needs_market_context"] += 1
        confidence = row.get("confidence")
        if isinstance(confidence, (int, float)):
            group["confidence_values"].append(float(confidence))
        observed_at = str(row.get("observed_at") or "")
        latest_at = str(group["latest"].get("observed_at") or "")
        if observed_at >= latest_at:
            group["latest"] = row

    rankings: list[dict[str, Any]] = []
    for group in groups.values():
        sentiments = group["sentiments"]
        bullish = int(sentiments.get("bullish", 0))
        bearish = int(sentiments.get("bearish", 0))
        neutral = int(sum(sentiments.values()) - bullish - bearish)
        source_names = sorted(group["sources"])
        confidence_values = group["confidence_values"]
        avg_confidence = round(sum(confidence_values) / len(confidence_values), 3) if confidence_values else None
        latest = group["latest"]
        signal_count = int(group["signals"])
        source_count = len(source_names)
        net_consensus = bullish - bearish
        rank_score = signal_count * 10 + source_count * 5 + net_consensus
        rankings.append(
            _compact_row(
                {
                    "symbol": group["symbol"],
                    "rank_score": rank_score,
                    "signal_count": signal_count,
                    "source_count": source_count,
                    "net_consensus": net_consensus,
                    "bullish_count": bullish,
                    "bearish_count": bearish,
                    "neutral_count": neutral,
                    "avg_confidence": avg_confidence,
                    "latest_at": latest.get("observed_at"),
                    "latest_source": latest.get("source_name") or latest.get("source_id"),
                    "latest_title": latest.get("title") or latest.get("thesis"),
                    "source_names": source_names[:8],
                    "needs_market_context_count": int(group["needs_market_context"]),
                }
            )
        )

    return sorted(
        rankings,
        key=lambda row: (
            row.get("rank_score") or 0,
            row.get("signal_count") or 0,
            row.get("source_count") or 0,
            str(row.get("latest_at") or ""),
        ),
        reverse=True,
    )[:limit]


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


def _first_url(value: Any) -> str:
    refs = value if isinstance(value, list) else []
    for ref in refs:
        text = str(ref or "").strip()
        if text.startswith(("http://", "https://", "file://", "/")):
            return text
    return ""


def _compact_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value not in EMPTY_VALUES}
