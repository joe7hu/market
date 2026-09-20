"""Publication-lineage enrichment for bounded panel payloads."""

from __future__ import annotations

from typing import Any, Mapping

from investment_panel.infrastructure.postgres.analysis import current_option_publication_rows


def published_tables(
    runtime: Any,
    requested: tuple[str, ...],
    *,
    row_limits: Mapping[str, int] | None = None,
    total_counts: dict[str, int] | None = None,
    symbols: set[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Read the current item for each requested model with its publication lineage."""

    if not requested:
        return {}
    market_names = set(requested) & {
        "market_state_snapshot", "coverage_matrix", "market_valuation_reference_charts",
        "market_environment_assets", "market_environment_model",
    }
    generic_names = tuple(name for name in requested if name not in market_names)
    with runtime.snapshot() as connection:
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
        params: list[Any] = [list(generic_names), list(generic_names)]
        source_filter = ""
        source_table = "published_rows"
        if symbols is not None:
            source_filter = """
                , filtered_rows AS (
                    SELECT *
                    FROM published_rows
                    WHERE COALESCE(
                        UPPER(COALESCE(
                            payload->>'ticker',
                            payload->>'symbol',
                            payload->>'underlying',
                            payload->'ticker_decision'->>'ticker'
                        )),
                        ''
                    ) = ANY(%s)
                    OR COALESCE(
                        payload->>'ticker',
                        payload->>'symbol',
                        payload->>'underlying',
                        payload->'ticker_decision'->>'ticker'
                    ) IS NULL
                )
            """
            source_table = "filtered_rows"
            params.append(sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}))
        query += source_filter
        limits_by_model = {
            name: max(1, int(limit))
            for name, limit in (row_limits or {}).items()
            if name in generic_names and limit > 0
        }
        if limits_by_model:
            query += f"""
                , model_counts AS (
                    SELECT model_name, count(*) AS total_count
                    FROM {source_table}
                    GROUP BY model_name
                )
                , ranked_rows AS (
                    SELECT {source_table}.*,
                           row_number() OVER (PARTITION BY model_name ORDER BY rank) AS row_number
                    FROM {source_table}
                )
                SELECT requested_limit.model_name, ranked_rows.payload, ranked_rows.publication_id,
                       ranked_rows.published_at, ranked_rows.rank,
                       COALESCE(model_counts.total_count, 0) AS total_count
                FROM unnest(%s::text[], %s::integer[]) AS requested_limit(model_name, row_limit)
                LEFT JOIN model_counts
                  ON model_counts.model_name = requested_limit.model_name
                LEFT JOIN ranked_rows
                  ON requested_limit.model_name = ranked_rows.model_name
                WHERE ranked_rows.model_name IS NULL
                   OR requested_limit.row_limit IS NULL
                   OR ranked_rows.row_number <= requested_limit.row_limit
                ORDER BY requested_limit.model_name, ranked_rows.rank
            """
            params.extend((list(generic_names), [limits_by_model.get(name) for name in generic_names]))
        else:
            query += f" SELECT model_name, payload, publication_id, published_at, rank FROM {source_table} ORDER BY model_name, rank"
        rows = connection.execute(query, params).fetchall() if generic_names else []
        option_rows = (
            current_option_publication_rows(
                connection,
                scope="options-radar",
                model_name="option_radar_opportunity",
            )
            if "option_radar_opportunity" in requested
            else None
        )
        market_rows = []
        if market_names:
            market_symbol_filter = ""
            market_params: list[Any] = [sorted(market_names)]
            if symbols is not None:
                market_symbol_filter = """
                    AND (
                        COALESCE(UPPER(COALESCE(
                            item.payload->>'ticker', item.payload->>'symbol', item.payload->>'underlying',
                            item.payload->'ticker_decision'->>'ticker'
                        )), '') = ANY(%s)
                        OR COALESCE(
                            item.payload->>'ticker', item.payload->>'symbol', item.payload->>'underlying',
                            item.payload->'ticker_decision'->>'ticker'
                        ) IS NULL
                    )
                """
                market_params.append(sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}))
            market_params.extend((sorted(market_names), [(row_limits or {}).get(name) for name in sorted(market_names)]))
            market_rows = connection.execute(
                """WITH latest AS (
                    SELECT publication.id, publication.published_at FROM app.publication publication
                    JOIN analysis.run run ON run.id = publication.analysis_run_id
                    WHERE publication.scope = 'market' AND publication.status = 'published'
                      AND publication.published_at <= now() AND run.status = 'succeeded'
                    ORDER BY publication.published_at DESC, publication.id DESC LIMIT 1
                ), ranked AS (
                    SELECT item.model_name, item.payload, latest.id::text AS publication_id,
                           latest.published_at, item.rank,
                           count(*) OVER (PARTITION BY item.model_name) AS total_count,
                           row_number() OVER (PARTITION BY item.model_name ORDER BY item.rank) AS row_number
                    FROM latest JOIN app.publication_content_item item ON item.publication_id = latest.id
                    WHERE item.model_name = ANY(%s)
                    """ + market_symbol_filter + """
                ) SELECT ranked.* FROM ranked
                  JOIN unnest(%s::text[], %s::integer[]) AS bounds(model_name, row_limit) USING (model_name)
                  WHERE bounds.row_limit IS NULL OR ranked.row_number <= bounds.row_limit
                  ORDER BY model_name, rank""",
                market_params,
            ).fetchall()
    output: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        model_name = str(row["model_name"])
        output.setdefault(model_name, [])
        if total_counts is not None and "total_count" in row:
            total_counts[model_name] = int(row["total_count"] or 0)
        if row.get("payload") is None:
            continue
        payload = dict(row["payload"] or {})
        if model_name in {"trade_plan", "outcome_attribution"} or "publication_id" not in payload:
            payload["publication_id"] = str(row["publication_id"])
        published_at = row["published_at"]
        if published_at is not None:
            payload.setdefault("publication_published_at", published_at.isoformat())
        output[model_name].append(payload)
    if market_names:
        # Apply bounds only after pinning all models to the same publication.
        for name in market_names:
            selected = [row for row in market_rows if row["model_name"] == name]
            if total_counts is not None:
                total_counts[name] = int(selected[0]["total_count"]) if selected else 0
            bound = (row_limits or {}).get(name)
            output[name] = [
                {**dict(row["payload"] or {}), "publication_id": row["publication_id"],
                 "publication_published_at": row["published_at"].isoformat()}
                for row in (selected[:bound] if bound else selected)
            ]
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
