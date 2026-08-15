"""Publication-lineage enrichment for bounded panel payloads."""

from __future__ import annotations

from typing import Any


def published_tables(runtime: Any, requested: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
    """Read the current item for each requested model with its publication lineage."""

    if not requested:
        return {}
    with runtime.read() as connection:
        rows = connection.execute(
            """
            WITH compact_latest AS MATERIALIZED (
                SELECT DISTINCT ON (item.model_name)
                       item.model_name, item.publication_id, publication.published_at
                FROM app.current_publication_item item
                JOIN app.publication publication ON publication.id = item.publication_id
                WHERE item.model_name = ANY(%s) AND publication.status = 'published'
                ORDER BY item.model_name, publication.published_at DESC NULLS LAST, item.publication_id DESC
            ), compact_current AS MATERIALIZED (
                SELECT item.model_name, payload.payload,
                       item.publication_id::text AS publication_id, publication.published_at, item.rank
                FROM compact_latest latest
                JOIN app.current_publication_item item
                  ON item.publication_id = latest.publication_id AND item.model_name = latest.model_name
                JOIN app.publication_payload payload ON payload.content_hash = item.content_hash
                JOIN app.publication publication ON publication.id = item.publication_id
            ), current_publication AS MATERIALIZED (
                SELECT id, published_at
                FROM app.publication
                WHERE status = 'published'
            ), latest AS (
                SELECT DISTINCT ON (item.model_name)
                       item.model_name, publication.id, publication.published_at
                FROM current_publication publication
                JOIN app.publication_item item ON item.publication_id = publication.id
                WHERE item.model_name = ANY(%s)
                  AND NOT EXISTS (SELECT 1 FROM compact_current compact WHERE compact.model_name = item.model_name)
                ORDER BY item.model_name, publication.published_at DESC, publication.id DESC
            )
            SELECT model_name, payload, publication_id, published_at, rank
            FROM compact_current
            UNION ALL
            SELECT item.model_name, item.payload, publication.id::text AS publication_id, publication.published_at, item.rank
            FROM latest
            JOIN app.publication_item item
              ON item.publication_id = latest.id AND item.model_name = latest.model_name
            JOIN current_publication publication ON publication.id = latest.id
            ORDER BY model_name, rank
            """,
            [list(requested), list(requested)],
        ).fetchall()
    output: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        payload = dict(row["payload"] or {})
        payload.setdefault("publication_id", str(row["publication_id"]))
        published_at = row["published_at"]
        if published_at is not None:
            payload.setdefault("publication_published_at", published_at.isoformat())
        output.setdefault(str(row["model_name"]), []).append(payload)
    return output
