"""Canonical instrument universe for thesis monitoring."""

from __future__ import annotations

from typing import Any, Iterable


def monitored_thesis_rows(
    connection: Any,
    *,
    symbols: Iterable[str] | None = None,
    include_current_prices: bool = True,
) -> list[dict[str, Any]]:
    """Load the monitored universe, optionally bounded to explicit symbols."""

    normalized = sorted({str(symbol).strip().upper() for symbol in symbols or () if str(symbol).strip()})
    if symbols is not None and not normalized:
        return []
    symbol_filter = " AND instrument.symbol = ANY(%s)" if normalized else ""

    price_cte = """
        current_prices AS MATERIALIZED (
            SELECT *
            FROM raw.current_price_at(
                now(),
                ARRAY(SELECT instrument_id FROM monitored)::bigint[]
            )
        )
    """ if include_current_prices else """
        current_prices AS (
            SELECT NULL::bigint AS instrument_id,
                   NULL::double precision AS price,
                   NULL::timestamptz AS observed_at
            WHERE false
        )
    """
    rows = connection.execute(
        f"""
        WITH monitored AS MATERIALIZED (
            SELECT instrument.id AS instrument_id, instrument.symbol,
                   thesis.id AS revision_id, thesis.revision, thesis.thesis,
                   thesis.author_kind, thesis.change_rationale,
                   thesis.last_assessed_at, thesis.last_human_reviewed_at,
                   thesis.created_at, thesis.updated_at,
                   (position.instrument_id IS NOT NULL) AS owned,
                   (watch.instrument_id IS NOT NULL AND watch.watch_state <> 'excluded') AS watched,
                   (option_policy.instrument_id IS NOT NULL) AS options_underwriting,
                   position.quantity, position.average_cost
            FROM catalog.instrument instrument
            LEFT JOIN app.thesis thesis ON thesis.instrument_id = instrument.id AND thesis.status = 'current'
            LEFT JOIN app.portfolio_position position ON position.instrument_id = instrument.id
            LEFT JOIN app.watchlist_item watch ON watch.instrument_id = instrument.id
            LEFT JOIN app.option_history_policy option_policy
              ON option_policy.instrument_id = instrument.id
             AND option_policy.profile = 'history_full'
             AND option_policy.collection_tier = 'core'
             AND option_policy.requested_state = 'on'
             AND option_policy.effective_state = 'active'
            WHERE (
                position.instrument_id IS NOT NULL
                OR (watch.instrument_id IS NOT NULL AND watch.watch_state <> 'excluded')
                OR option_policy.instrument_id IS NOT NULL
                OR thesis.id IS NOT NULL
            ){symbol_filter}
        ), {price_cte}
        SELECT monitored.*, quote.price AS latest_price,
               quote.observed_at AS latest_quote_at,
               catalyst.starts_at AS next_catalyst_at, catalyst.title AS next_catalyst,
               run.status AS latest_automation_status, run.error AS latest_automation_error,
               run.started_at AS latest_automation_started_at
        FROM monitored
        LEFT JOIN current_prices quote ON quote.instrument_id = monitored.instrument_id
        LEFT JOIN LATERAL (
            SELECT starts_at, title FROM app.catalyst
            WHERE instrument_id = monitored.instrument_id AND status = 'current' AND starts_at >= now()
            ORDER BY starts_at ASC LIMIT 1
        ) catalyst ON true
        LEFT JOIN LATERAL (
            SELECT status, error, started_at FROM app.thesis_automation_run
            WHERE instrument_id = monitored.instrument_id ORDER BY started_at DESC LIMIT 1
        ) run ON true
        ORDER BY monitored.symbol
        """,
        [normalized] if normalized else [],
    ).fetchall()
    return [dict(row) for row in rows]
