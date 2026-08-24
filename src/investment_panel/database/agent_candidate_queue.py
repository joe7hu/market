"""Read the current unique-symbol option candidates for agent work."""

from __future__ import annotations

from typing import Any

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


def current_candidate_payloads(runtime: DatabaseRuntime, *, limit: int) -> list[dict[str, Any]]:
    with runtime.read(JOB_PROFILE) as connection:
        rows = connection.execute(
            """
            WITH latest AS (
                SELECT id FROM app.publication
                WHERE scope = 'options-radar' AND status = 'published'
                ORDER BY published_at DESC NULLS LAST, created_at DESC LIMIT 1
            ), ranked AS (
                SELECT item.payload, item.rank,
                       NULLIF(item.payload->>'research_rank', '')::integer AS research_rank,
                       coalesce(item.payload->>'ticker', item.payload->>'symbol') AS symbol
                FROM app.publication_content_item item
                JOIN latest ON latest.id = item.publication_id
                WHERE item.model_name = 'option_radar_opportunity'
            ), unique_symbols AS (
                SELECT DISTINCT ON (symbol) payload, rank, research_rank, symbol
                FROM ranked WHERE symbol IS NOT NULL AND symbol <> ''
                ORDER BY symbol, research_rank NULLS LAST, rank NULLS LAST
            )
            SELECT payload FROM unique_symbols
            ORDER BY research_rank NULLS LAST, symbol LIMIT %s
            """,
            [limit],
        ).fetchall()
    return [dict(row["payload"] or {}) for row in rows]
