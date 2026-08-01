"""Bounded PostgreSQL context assembly for option-agent requests."""

from __future__ import annotations

from typing import Any

from investment_panel.database.thesis_evidence import thesis_source_evidence


_DUPLICATE_MODELS = {
    "agent_recommendations", "candidates", "daily_brief", "decision_queue",
    "opportunities_ranked", "option_radar_opportunity", "symbol_decision_snapshots",
}
_VERBOSE_KEYS = {
    "raw", "payload", "chart", "chart_1y", "volume_1m_bars", "atr_pct_1m_points",
    "contracts", "chain", "alternatives", "candidate_rows", "source_text",
}


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
          AND item.model_name <> ALL(%s)
        ORDER BY item.model_name, publication.published_at DESC NULLS LAST, item.rank
        LIMIT 24
        """,
        [symbol, sorted(_DUPLICATE_MODELS | {"candidate_event", "option_features", "option_snapshot"})],
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
        "option_opportunity": _bounded_value(dict(option["payload"] or {})) if option else {},
        "published_models": {str(row["model_name"]): _bounded_value(dict(row["payload"] or {})) for row in published},
        "catalysts": [dict(row) for row in catalysts],
        "source_evidence": [
            _bounded_value(item) for item in thesis_source_evidence(connection, [symbol]).get(symbol, [])[:12]
        ],
    }


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return None
    if isinstance(value, dict):
        return {
            str(key): bounded
            for key, item in value.items()
            if str(key) not in _VERBOSE_KEYS
            if (bounded := _bounded_value(item, depth=depth + 1)) not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [_bounded_value(item, depth=depth + 1) for item in value[:12]]
    if isinstance(value, str):
        return value[:1000]
    return value
