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
) -> dict[str, Any]:
    enabled = context_sources or {}

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
    evidence = thesis_source_evidence(connection, [symbol], max_per_symbol=24).get(symbol, [])
    return {
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
