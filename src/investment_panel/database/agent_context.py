"""Bounded PostgreSQL context assembly for option-agent requests."""

from __future__ import annotations

from typing import Any

from investment_panel.database.thesis_evidence import thesis_source_evidence


_DUPLICATE_MODELS = {
    "candidates", "daily_brief", "decision_queue",
    "opportunities_ranked", "option_radar_opportunity", "symbol_decision_snapshots",
}
_VERBOSE_KEYS = {
    "raw", "payload", "chart", "chart_1y", "volume_1m_bars", "atr_pct_1m_points",
    "contracts", "chain", "alternatives", "candidate_rows", "source_text",
}


def ticker_context(
    connection: Any,
    symbol: str,
    *,
    context_sources: dict[str, bool] | None = None,
    cutoff: Any | None = None,
) -> dict[str, Any]:
    enabled = context_sources or {}

    if cutoff is None:
        return {
            "context_status": {"cutoff": None, "cutoff_available": False},
            "portfolio": {},
            "option_opportunity": {},
            "published_models": {},
            "catalysts": [],
            "source_evidence": [],
        }

    def include(key: str) -> bool:
        return bool(enabled.get(key, True))

    state = connection.execute(
        """
        SELECT position.quantity, position.average_cost, position.notes AS portfolio_notes,
               thesis.thesis, quote.price, quote.observed_at AS quote_observed_at
        FROM catalog.instrument instrument
        LEFT JOIN app.portfolio_position position ON position.instrument_id = instrument.id
        LEFT JOIN app.thesis thesis ON thesis.instrument_id = instrument.id AND thesis.status = 'current'
        LEFT JOIN LATERAL (
            SELECT quote.price, quote.observed_at
            FROM raw.quote quote
            JOIN ingest.run quote_run ON quote_run.id = quote.ingest_run_id
            JOIN LATERAL (
                SELECT 1
                FROM raw.quote_fact_availability availability
                JOIN ingest.run confirmation_run ON confirmation_run.id = availability.ingest_run_id
                WHERE availability.fact_id = quote.id
                  AND availability.fact_available_at = quote.available_at
                  AND confirmation_run.status IN ('succeeded', 'partial')
                  AND confirmation_run.finished_at IS NOT NULL
                  AND confirmation_run.finished_at <= %s
                ORDER BY confirmation_run.finished_at, confirmation_run.id
                LIMIT 1
            ) quote_confirmation ON true
            WHERE quote.instrument_id = instrument.id
              AND CAST(%s AS timestamptz) IS NOT NULL
              AND quote.observed_at <= %s AND quote.available_at <= %s
              AND quote_run.status IN ('succeeded', 'partial')
              AND quote_run.finished_at IS NOT NULL AND quote_run.finished_at <= %s
            ORDER BY quote.observed_at DESC, quote.available_at DESC, quote.source_id, quote.id DESC
            LIMIT 1
        ) quote ON true
        WHERE instrument.symbol = %s
        """,
        [cutoff, cutoff, cutoff, cutoff, cutoff, symbol],
    ).fetchone()
    option = connection.execute(
        """
        SELECT item.payload
        FROM app.publication publication
        JOIN app.publication_content_item item ON item.publication_id = publication.id
        JOIN analysis.run publication_run ON publication_run.id = publication.analysis_run_id
        WHERE publication.scope = 'options-radar' AND publication.status = 'published'
          AND item.model_name = 'option_radar_opportunity'
          AND coalesce(item.payload->>'ticker', item.payload->>'symbol') = %s
          AND CAST(%s AS timestamptz) IS NOT NULL AND publication.published_at <= %s
          AND publication_run.status IN ('succeeded', 'partial')
          AND publication_run.finished_at IS NOT NULL AND publication_run.finished_at <= %s
        ORDER BY publication.published_at DESC NULLS LAST, publication.id DESC, item.rank, item.stable_key
        LIMIT 1
        """,
        [symbol, cutoff, cutoff, cutoff],
    ).fetchone()
    published = connection.execute(
        """
        SELECT DISTINCT ON (item.model_name) item.model_name, item.payload
        FROM app.publication publication
        JOIN app.publication_content_item item ON item.publication_id = publication.id
        JOIN analysis.run publication_run ON publication_run.id = publication.analysis_run_id
        WHERE publication.status = 'published'
          AND coalesce(item.payload->>'symbol', item.payload->>'ticker', item.payload->>'underlying') = %s
          AND item.model_name <> ALL(%s)
          AND CAST(%s AS timestamptz) IS NOT NULL AND publication.published_at <= %s
          AND publication_run.status IN ('succeeded', 'partial')
          AND publication_run.finished_at IS NOT NULL AND publication_run.finished_at <= %s
        ORDER BY item.model_name, publication.published_at DESC NULLS LAST,
                 publication.id DESC, item.rank, item.stable_key
        LIMIT 24
        """,
        [symbol, sorted(_DUPLICATE_MODELS | {"candidate_event", "option_features", "option_snapshot"}), cutoff, cutoff, cutoff],
    ).fetchall()
    catalysts = connection.execute(
        """
        SELECT catalyst.starts_at, catalyst.title, catalyst.expected_impact, catalyst.notes
        FROM app.catalyst catalyst
        JOIN catalog.instrument instrument ON instrument.id = catalyst.instrument_id
        JOIN LATERAL (
            SELECT event_version.available_at, event_version.source_id,
                   event_version.title, event_version.starts_at
            FROM raw.market_event_version event_version
            JOIN ingest.run event_run ON event_run.id = event_version.ingest_run_id
            WHERE event_version.market_event_id = catalyst.market_event_id
              AND (catalyst.source_id IS NULL OR event_version.source_id = catalyst.source_id)
              AND event_version.available_at <= %s
              AND event_version.starts_at >= %s
              AND event_run.status IN ('succeeded', 'partial')
              AND event_run.finished_at IS NOT NULL
              AND event_run.finished_at <= %s
            ORDER BY event_version.available_at DESC, event_version.source_id, event_version.id DESC
            LIMIT 1
        ) event_lineage ON true
        WHERE instrument.symbol = %s
          AND CAST(%s AS timestamptz) IS NOT NULL
          AND catalyst.created_at <= %s
          AND (catalyst.superseded_at IS NULL OR catalyst.superseded_at > %s)
          AND catalyst.starts_at >= %s
          AND catalyst.title = event_lineage.title
          AND catalyst.starts_at = event_lineage.starts_at
        ORDER BY catalyst.starts_at, event_lineage.available_at DESC, event_lineage.source_id,
                 catalyst.version, catalyst.id
        LIMIT 5
        """,
        [cutoff, cutoff, cutoff, symbol, cutoff, cutoff, cutoff, cutoff],
    ).fetchall()
    evidence = thesis_source_evidence(connection, [symbol], max_per_symbol=24, cutoff=cutoff).get(symbol, [])
    return {
        "context_status": {
            "cutoff": cutoff.isoformat() if cutoff is not None else None,
            "cutoff_available": cutoff is not None,
        },
        "portfolio": _bounded_value(dict(state)) if state and include("portfolio") else {},
        "option_opportunity": option_opportunity_context(dict(option["payload"] or {})) if option else {},
        "published_models": {
            str(row["model_name"]): _bounded_value(dict(row["payload"] or {}))
            for row in published
            if _model_enabled(str(row["model_name"]), include)
        },
        "catalysts": [dict(row) for row in catalysts] if include("catalysts") else [],
        "source_evidence": [
            _bounded_value(item)
            for item in evidence
            if _evidence_enabled(item, include)
        ][:8],
    }


def option_opportunity_context(payload: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "ticker", "symbol", "decision_id", "state", "recommendation_state",
        "ranking_version", "research_rank", "trade_rank",
        "trade_rank_unavailable_reason", "execution_quality_score",
        "structure", "expiration", "strike", "option_type", "underlying_price",
        "dte", "iv", "delta", "spread_pct", "volume", "open_interest",
        "entry_price", "buy_under", "max_loss", "secured_cash", "max_profit",
        "break_even", "probability_profit", "expected_value", "risk_adjusted_expectancy",
        "tail_cvar", "data_confidence", "execution_confidence", "top_reasons",
        "blockers", "quality_status", "primary_edge", "catalyst_start", "catalyst_end",
        "thesis_payload", "details",
    }
    return _bounded_value({key: payload[key] for key in keys if key in payload})


def _model_enabled(model_name: str, include: Any) -> bool:
    normalized = model_name.lower()
    if any(term in normalized for term in ("technical", "sepa", "relative_strength")):
        return include("technicals")
    if any(term in normalized for term in ("fundamental", "valuation", "earnings", "dcf")):
        return include("fundamentals")
    if any(term in normalized for term in ("ownership", "disclosure", "13f", "trader")):
        return include("ownership")
    return True


def _evidence_enabled(item: dict[str, Any], include: Any) -> bool:
    family = str(item.get("source_family") or "").lower()
    if family in {"social", "private_graph", "thesis"}:
        return include("social_signals")
    return include("news")


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
        return value[:600]
    return value
