"""Bounded PostgreSQL context assembly for option-agent requests."""

from __future__ import annotations

from typing import Any

from investment_panel.database.thesis_evidence import thesis_source_evidence


def ticker_context(connection: Any, symbol: str) -> dict[str, Any]:
    state = connection.execute(
        """
        SELECT position.quantity, position.average_cost, position.notes AS portfolio_notes,
               thesis.thesis, quote.price, quote.observed_at AS quote_observed_at
        FROM catalog.instrument instrument
        LEFT JOIN app.portfolio_position position ON position.instrument_id = instrument.id
        LEFT JOIN app.thesis thesis ON thesis.instrument_id = instrument.id AND thesis.status = 'current'
        LEFT JOIN LATERAL (
            SELECT price, observed_at FROM raw.quote
            WHERE instrument_id = instrument.id ORDER BY observed_at DESC LIMIT 1
        ) quote ON true
        WHERE instrument.symbol = %s
        """,
        [symbol],
    ).fetchone()
    option = connection.execute(
        """
        SELECT item.payload
        FROM app.publication publication
        JOIN app.publication_item item ON item.publication_id = publication.id
        WHERE publication.scope = 'options-radar' AND publication.status = 'published'
          AND item.model_name = 'option_radar_opportunity'
          AND coalesce(item.payload->>'ticker', item.payload->>'symbol') = %s
        ORDER BY publication.published_at DESC NULLS LAST, item.rank LIMIT 1
        """,
        [symbol],
    ).fetchone()
    published = connection.execute(
        """
        SELECT DISTINCT ON (item.model_name) item.model_name, item.payload
        FROM app.publication publication
        JOIN app.publication_item item ON item.publication_id = publication.id
        WHERE publication.status = 'published'
          AND coalesce(item.payload->>'symbol', item.payload->>'ticker', item.payload->>'underlying') = %s
          AND item.model_name NOT IN ('candidate_event', 'option_features', 'option_snapshot')
        ORDER BY item.model_name, publication.published_at DESC NULLS LAST, item.rank
        LIMIT 24
        """,
        [symbol],
    ).fetchall()
    catalysts = connection.execute(
        """
        SELECT catalyst.starts_at, catalyst.title, catalyst.expected_impact, catalyst.notes
        FROM app.catalyst catalyst
        JOIN catalog.instrument instrument ON instrument.id = catalyst.instrument_id
        WHERE instrument.symbol = %s AND catalyst.starts_at >= now()
        ORDER BY catalyst.starts_at LIMIT 5
        """,
        [symbol],
    ).fetchall()
    return {
        "portfolio": dict(state) if state else {},
        "option_opportunity": dict(option["payload"] or {}) if option else {},
        "published_models": {str(row["model_name"]): dict(row["payload"] or {}) for row in published},
        "catalysts": [dict(row) for row in catalysts],
        "source_evidence": thesis_source_evidence(connection, [symbol]).get(symbol, [])[:12],
    }
