"""PostgreSQL read models for source catalog and ingestion diagnostics."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from investment_panel.database.runtime import DatabaseRuntime


class SourceRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def detail(self, source_id: str) -> dict[str, Any]:
        with self.runtime.read() as connection:
            source = connection.execute(
                "SELECT * FROM ingest.source WHERE id = %s", [source_id]
            ).fetchone()
            runs = connection.execute(
                "SELECT id::text AS run_id, capability, started_at, finished_at, status, "
                "item_count, instrument_count AS ticker_count, failure_detail, summary "
                "FROM ingest.run WHERE source_id = %s ORDER BY started_at DESC LIMIT 100",
                [source_id],
            ).fetchall()
            items = connection.execute(
                "SELECT id::text, source_key, kind, title, url, author, published_at, "
                "observed_at, summary, metadata FROM raw.content_item "
                "WHERE source_id = %s ORDER BY observed_at DESC LIMIT 200",
                [source_id],
            ).fetchall()
            signals = connection.execute(
                "SELECT signal.id, instrument.symbol, signal.observed_at, signal.signal_type, "
                "signal.sentiment, signal.direction, signal.confidence, signal.thesis, "
                "signal.antithesis, signal.invalidation, signal.details "
                "FROM analysis.source_signal signal "
                "JOIN raw.content_item item ON item.id = signal.content_item_id "
                "JOIN catalog.instrument instrument ON instrument.id = signal.instrument_id "
                "WHERE item.source_id = %s ORDER BY signal.observed_at DESC LIMIT 200",
                [source_id],
            ).fetchall()
        return {
            "source": {"source_id": source_id, "found": source is not None, **(dict(source) if source else {})},
            "runs": [dict(row) for row in runs],
            "items": [dict(row) for row in items],
            "signals": [dict(row) for row in signals],
            "status": {"ready": True, "source": "postgresql"},
        }

    def catalog(self) -> dict[str, Any]:
        with self.runtime.read() as connection:
            rows = connection.execute(
                "SELECT source.id AS source_id, source.name, source.family, source.kind, "
                "source.origin, source.enabled, source.ingestion_mode, source.source_url, "
                "source.capabilities, source.updated_at, run.status AS latest_status, "
                "run.finished_at AS latest_finished_at, run.failure_detail AS latest_failure_detail "
                "FROM ingest.source source LEFT JOIN LATERAL ("
                "SELECT status, finished_at, failure_detail FROM ingest.run "
                "WHERE source_id = source.id ORDER BY started_at DESC LIMIT 1"
                ") run ON true ORDER BY source.family, source.id"
            ).fetchall()
        families: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            families[str(row["family"])].append(dict(row))
        return {
            "categories": sorted(families),
            "families": dict(families),
            "rows": [dict(row) for row in rows],
            "generated_from": "postgresql.ingest.source",
            "status": {"ready": True, "source": "postgresql"},
        }

    def audit(self) -> dict[str, Any]:
        with self.runtime.read() as connection:
            counts = connection.execute(
                "SELECT count(*) FILTER (WHERE enabled) AS active, "
                "count(*) FILTER (WHERE NOT enabled) AS disabled FROM ingest.source"
            ).fetchone()
            failures = connection.execute(
                "SELECT source_id, capability, started_at, finished_at, status, failure_detail "
                "FROM ingest.run WHERE status IN ('failed', 'partial') "
                "ORDER BY started_at DESC LIMIT 100"
            ).fetchall()
        failure_rows = [dict(row) for row in failures]
        return {
            "status": "ok",
            "database": "postgresql",
            "active_sources": int(counts["active"] or 0),
            "disabled_sources": int(counts["disabled"] or 0),
            "source_failures": failure_rows,
            "broker_rows": [],
            "failures": failure_rows,
        }
