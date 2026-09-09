"""Canonical instrument universe for thesis monitoring."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable


def monitored_thesis_rows(
    connection: Any,
    *,
    symbols: Iterable[str] | None = None,
    include_current_prices: bool = True,
    as_of: Any | None = None,
) -> list[dict[str, Any]]:
    """Load the monitored universe, optionally bounded to explicit symbols."""

    normalized = sorted({str(symbol).strip().upper() for symbol in symbols or () if str(symbol).strip()})
    if symbols is not None and not normalized:
        return []
    symbol_filter = " AND instrument.symbol = ANY(%s)" if normalized else ""
    reference = as_of if as_of is not None else datetime.now(UTC)
    thesis_join = (
        """
            LEFT JOIN LATERAL (
                SELECT thesis.id, thesis.revision, thesis.thesis,
                       thesis.author_kind, thesis.change_rationale,
                       thesis.last_assessed_at, thesis.last_human_reviewed_at,
                       thesis.created_at, thesis.updated_at
                FROM app.thesis thesis
                WHERE thesis.instrument_id = instrument.id
                  AND thesis.created_at <= parameters.as_of
                  AND (thesis.updated_at <= parameters.as_of OR thesis.status = 'superseded')
                  AND (
                      thesis.status <> 'superseded'
                      OR NOT EXISTS (
                          SELECT 1
                          FROM app.thesis successor
                          WHERE successor.superseded_revision_id = thesis.id
                            AND successor.created_at <= parameters.as_of
                      )
                  )
                ORDER BY thesis.created_at DESC, thesis.revision DESC, thesis.id DESC
                LIMIT 1
            ) thesis ON true
        """
        if as_of is not None else
        """
            LEFT JOIN app.thesis thesis
              ON thesis.instrument_id = instrument.id AND thesis.status = 'current'
        """
    )
    watch_filter = (
        "watch.instrument_id IS NOT NULL AND watch.watch_state IN ('owned', 'watched', 'watching') "
        "AND watch.created_at <= parameters.as_of AND watch.updated_at <= parameters.as_of"
        if as_of is not None else
        "watch.instrument_id IS NOT NULL AND watch.watch_state IN ('owned', 'watched', 'watching')"
    )
    historical_position = (
        """
                OR EXISTS (
                    SELECT 1
                    FROM app.portfolio_transaction portfolio_tx
                    WHERE portfolio_tx.instrument_id = instrument.id
                      AND portfolio_tx.executed_at <= parameters.as_of
                      AND portfolio_tx.created_at <= parameters.as_of
                )
        """
        if as_of is not None else ""
    )
    option_policy_temporal = (
        " AND option_policy.created_at <= parameters.as_of"
        " AND COALESCE(option_policy.activated_at, option_policy.created_at) <= parameters.as_of"
        " AND (option_policy.paused_at IS NULL OR option_policy.paused_at > parameters.as_of)"
        " AND (option_policy.expires_at IS NULL OR option_policy.expires_at > parameters.as_of)"
        if as_of is not None else ""
    )
    position_join = (
        "position.instrument_id = instrument.id"
        if as_of is None else "FALSE"
    )

    price_cte = """
        current_prices AS MATERIALIZED (
            SELECT quote.*
            FROM parameters
            CROSS JOIN LATERAL raw.current_price_at(
                parameters.as_of,
                ARRAY(SELECT instrument_id FROM monitored)::bigint[]
            ) quote
        )
    """ if include_current_prices else """
        current_prices AS (
            SELECT NULL::bigint AS instrument_id,
                   NULL::double precision AS price,
                   NULL::timestamptz AS observed_at,
                   NULL::timestamptz AS available_at
            WHERE false
        )
    """
    rows = connection.execute(
        f"""
        WITH parameters AS (
            SELECT %s::timestamptz AS as_of
        ), monitored AS MATERIALIZED (
            SELECT instrument.id AS instrument_id, instrument.symbol,
                   thesis.id AS revision_id, thesis.revision, thesis.thesis,
                   thesis.author_kind, thesis.change_rationale,
                   thesis.last_assessed_at, thesis.last_human_reviewed_at,
                   thesis.created_at, thesis.updated_at,
                   (position.instrument_id IS NOT NULL) AS owned,
                   ({watch_filter}) AS watched,
                   (option_policy.instrument_id IS NOT NULL) AS options_underwriting,
                   position.quantity, position.average_cost,
                   position.updated_at AS position_updated_at,
                   watch.created_at AS watchlist_created_at,
                   watch.updated_at AS watchlist_updated_at
            FROM catalog.instrument instrument
            CROSS JOIN parameters
            {thesis_join}
            LEFT JOIN app.portfolio_position position ON {position_join}
            LEFT JOIN app.watchlist_item watch ON watch.instrument_id = instrument.id
            LEFT JOIN app.option_history_policy option_policy
              ON option_policy.instrument_id = instrument.id
             AND option_policy.profile = 'history_full'
             AND option_policy.collection_tier = 'core'
             AND option_policy.requested_state = 'on'
             AND option_policy.effective_state = 'active'
             {option_policy_temporal}
            WHERE (
                position.instrument_id IS NOT NULL
                OR ({watch_filter})
                OR option_policy.instrument_id IS NOT NULL
                OR thesis.id IS NOT NULL
                {historical_position}
            ){symbol_filter}
        ), {price_cte}
        SELECT monitored.*, quote.price AS latest_price,
               quote.observed_at AS latest_quote_at,
               quote.available_at AS latest_quote_available_at,
               catalyst.starts_at AS next_catalyst_at, catalyst.title AS next_catalyst,
               run.status AS latest_automation_status, run.error AS latest_automation_error,
               run.started_at AS latest_automation_started_at
        FROM monitored
        CROSS JOIN parameters
        LEFT JOIN current_prices quote ON quote.instrument_id = monitored.instrument_id
        LEFT JOIN LATERAL (
            SELECT starts_at, title FROM app.catalyst
            WHERE instrument_id = monitored.instrument_id
              AND created_at <= parameters.as_of
              AND (
                  status = 'current'
                  OR (status = 'superseded' AND superseded_at > parameters.as_of)
              )
              AND starts_at >= parameters.as_of
            ORDER BY starts_at ASC LIMIT 1
        ) catalyst ON true
        LEFT JOIN LATERAL (
            SELECT status, error, started_at FROM app.thesis_automation_run
            WHERE instrument_id = monitored.instrument_id
              AND started_at <= parameters.as_of
              AND created_at <= parameters.as_of
            ORDER BY started_at DESC LIMIT 1
        ) run ON true
        ORDER BY monitored.symbol
        """,
        [reference, normalized] if normalized else [reference],
    ).fetchall()
    return [dict(row) for row in rows]
