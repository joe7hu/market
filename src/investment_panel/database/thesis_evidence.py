"""Evidence readers for PostgreSQL-owned thesis monitor rows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


_DEFAULT_CUTOFF = object()


def thesis_source_evidence(
    connection: Any,
    symbols: list[str],
    *,
    max_per_symbol: int = 12,
    cutoff: Any = _DEFAULT_CUTOFF,
) -> dict[str, list[dict[str, Any]]]:
    if not symbols:
        return {}
    if cutoff is _DEFAULT_CUTOFF:
        cutoff = datetime.now(UTC)
    if cutoff is None:
        return {str(symbol): [] for symbol in symbols}
    rows = connection.execute(
        """
        WITH evidence_rows AS (
            SELECT regexp_replace(upper(instrument.symbol), '[.]+$', '') AS symbol,
                   item.source_id,
                   CASE WHEN source.kind = 'news' THEN lower(source.name) ELSE source.name END AS source_name,
                   CASE WHEN source.family IN ('social', 'private_graph') THEN 'thesis' ELSE source.family END AS source_family,
                   item.kind AS source_type, item.title,
                   COALESCE(signal.thesis, item.summary, item.title) AS summary,
                   COALESCE(signal.sentiment, 'neutral') AS sentiment,
                   COALESCE(signal.observed_at, item.observed_at) AS observed_at,
                   COALESCE(item.url, 'source_item:' || item.id) AS reference,
                   row_number() OVER (
                       PARTITION BY regexp_replace(upper(instrument.symbol), '[.]+$', ''),
                                    CASE WHEN source.kind = 'news' THEN lower(source.name) ELSE source.id END
                       ORDER BY COALESCE(signal.observed_at, item.observed_at) DESC, item.id DESC
                   ) AS source_rank
            FROM raw.content_item_instrument link
            JOIN raw.content_item item ON item.id = link.content_item_id
            JOIN ingest.run ingest_run ON ingest_run.id = item.ingest_run_id
            JOIN catalog.instrument instrument ON instrument.id = link.instrument_id
            JOIN ingest.source source ON source.id = item.source_id
            LEFT JOIN LATERAL (
                SELECT signal.thesis, signal.sentiment, signal.observed_at
                FROM analysis.source_signal signal
                WHERE signal.content_item_id = item.id AND signal.instrument_id = instrument.id
                  AND COALESCE(signal.available_at, signal.observed_at) <= %s
                  AND signal.observed_at <= %s
                ORDER BY signal.observed_at DESC LIMIT 1
            ) signal ON true
            WHERE regexp_replace(upper(instrument.symbol), '[.]+$', '') = ANY(%s)
              AND source.enabled
              AND source.operational_state = 'active'
              AND ingest_run.status IN ('succeeded', 'partial')
              AND ingest_run.finished_at IS NOT NULL
              AND ingest_run.finished_at <= %s
              AND item.observed_at <= %s
              AND COALESCE(item.published_at, item.observed_at) <= %s
        ), balanced AS (
            SELECT evidence_rows.*,
                   row_number() OVER (PARTITION BY symbol ORDER BY observed_at DESC, source_id, reference) AS symbol_rank
            FROM evidence_rows WHERE source_rank <= 2
        )
        SELECT symbol, source_id, source_name, source_family, source_type,
               title, summary, sentiment, observed_at, reference
        FROM balanced WHERE symbol_rank <= %s ORDER BY symbol, observed_at DESC
        """,
        [cutoff, cutoff, symbols, cutoff, cutoff, cutoff, max(1, int(max_per_symbol))],
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw_row in rows:
        item = dict(raw_row)
        grouped.setdefault(str(item["symbol"]), []).append(item)
    return grouped


def assessments_by_revision(connection: Any, revision_ids: list[Any]) -> dict[int, list[dict[str, Any]]]:
    clean_ids = [int(value) for value in revision_ids if value is not None]
    if not clean_ids:
        return {}
    rows = connection.execute(
        """
        SELECT thesis_revision_id, evidence_reference, evidence_title, evidence_date,
               stance, materiality, affected_pillar_ids, confidence, rationale, created_at
        FROM app.thesis_evidence_assessment
        WHERE thesis_revision_id = ANY(%s)
        ORDER BY created_at DESC
        """,
        [clean_ids],
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        grouped.setdefault(int(item["thesis_revision_id"]), []).append(item)
    return grouped
