"""Publication-lineage enrichment for bounded panel payloads."""

from __future__ import annotations

from typing import Any, Mapping

from investment_panel.database.analysis import current_option_publication_rows


def published_tables(
    runtime: Any,
    requested: tuple[str, ...],
    *,
    row_limits: Mapping[str, int] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Read the current item for each requested model with its publication lineage."""

    if not requested:
        return {}
    with runtime.read() as connection:
        query = """
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
            ), published_rows AS (
                SELECT model_name, payload, publication_id, published_at, rank
                FROM compact_current
                UNION ALL
                SELECT item.model_name, item.payload, publication.id::text AS publication_id,
                       publication.published_at, item.rank
                FROM latest
                JOIN app.publication_item item
                  ON item.publication_id = latest.id AND item.model_name = latest.model_name
                JOIN current_publication publication ON publication.id = latest.id
            )
        """
        params: list[Any] = [list(requested), list(requested)]
        limited = [(name, max(1, int(limit))) for name, limit in (row_limits or {}).items() if name in requested and limit > 0]
        if limited:
            query += """
                , ranked_rows AS (
                    SELECT published_rows.*,
                           row_number() OVER (PARTITION BY model_name ORDER BY rank) AS row_number
                    FROM published_rows
                )
                SELECT ranked_rows.model_name, ranked_rows.payload, ranked_rows.publication_id,
                       ranked_rows.published_at, ranked_rows.rank
                FROM ranked_rows
                JOIN unnest(%s::text[], %s::integer[]) AS requested_limit(model_name, row_limit)
                  ON requested_limit.model_name = ranked_rows.model_name
                WHERE ranked_rows.row_number <= requested_limit.row_limit
                ORDER BY ranked_rows.model_name, ranked_rows.rank
            """
            params.extend(([name for name, _ in limited], [limit for _, limit in limited]))
        else:
            query += " SELECT model_name, payload, publication_id, published_at, rank FROM published_rows ORDER BY model_name, rank"
        rows = connection.execute(query, params).fetchall()
        option_rows = (
            current_option_publication_rows(
                connection,
                scope="options-radar",
                model_name="option_radar_opportunity",
            )
            if "option_radar_opportunity" in requested
            else None
        )
    output: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        payload = dict(row["payload"] or {})
        if str(row["model_name"]) in {"trade_plan", "outcome_attribution"} or "publication_id" not in payload:
            payload["publication_id"] = str(row["publication_id"])
        published_at = row["published_at"]
        if published_at is not None:
            payload.setdefault("publication_published_at", published_at.isoformat())
        output.setdefault(str(row["model_name"]), []).append(payload)
    if option_rows is not None:
        output["option_radar_opportunity"] = []
        for row in option_rows:
            payload = dict(row["payload"] or {})
            if "publication_id" not in payload:
                payload["publication_id"] = str(row["publication_id"])
            if row["published_at"] is not None:
                payload.setdefault("publication_published_at", row["published_at"].isoformat())
            output["option_radar_opportunity"].append(payload)
    return output
