"""Source registry materialization."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from investment_panel.core.db import json_dumps, query_rows
from investment_panel.core.source_ingestion.definitions import SOURCE_DEFINITIONS
from investment_panel.core.source_ingestion.utils import slug

def ensure_source_registry(con: Any) -> None:
    now = datetime.now(UTC)
    for source in SOURCE_DEFINITIONS:
        con.execute(
            """
            INSERT OR REPLACE INTO source_registry
            (source_id, source_name, source_family, source_kind, origin, enabled, ingestion_mode,
             raw_access, source_url, notes, config, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, coalesce((SELECT created_at FROM source_registry WHERE source_id = ?), ?), ?)
            """,
            [
                source["source_id"],
                source["source_name"],
                source["source_family"],
                source["source_kind"],
                source["origin"],
                source["enabled"],
                source["ingestion_mode"],
                source["raw_access"],
                source.get("source_url") or "",
                source["notes"],
                json_dumps(source.get("config") or {}),
                source["source_id"],
                now,
                now,
            ],
        )
    for source_id, source_name, source_family, source_kind in _dynamic_sources(con):
        con.execute(
            """
            INSERT OR IGNORE INTO source_registry
            (source_id, source_name, source_family, source_kind, origin, enabled, ingestion_mode,
             raw_access, source_url, notes, config, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'market', true, 'existing_table_sync', 'local_row', '', 'Discovered from existing Market rows.', '{}', ?, ?)
            """,
            [source_id, source_name, source_family, source_kind, now, now],
        )


def _dynamic_sources(con: Any) -> list[tuple[str, str, str, str]]:
    sources: dict[str, tuple[str, str, str, str]] = {}
    for row in query_rows(con, "SELECT DISTINCT provider FROM provider_runs WHERE provider IS NOT NULL"):
        provider = str(row.get("provider") or "")
        sources[slug(provider)] = (slug(provider), provider, "provider", "provider_run")
    for row in query_rows(con, "SELECT DISTINCT provider FROM news_items WHERE provider IS NOT NULL"):
        provider = str(row.get("provider") or "")
        sources[slug(provider)] = (slug(provider), provider, "news", "news")
    for row in query_rows(con, "SELECT DISTINCT source FROM market_screener_rows WHERE source IS NOT NULL"):
        provider = str(row.get("source") or "")
        sources[slug(provider)] = (slug(provider), provider, "market_data", "screener")
    for table, source_family, source_kind in [
        ("crypto_fundamentals", "market_data", "crypto_fundamental"),
        ("earnings_events", "events", "earnings_event"),
        ("analyst_estimates", "estimates", "analyst_estimate"),
    ]:
        for row in query_rows(con, f"SELECT DISTINCT source FROM {table} WHERE source IS NOT NULL"):
            provider = str(row.get("source") or "")
            sources[slug(provider)] = (slug(provider), provider, source_family, source_kind)
    return sorted(sources.values())
