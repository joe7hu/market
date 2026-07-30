"""Canonical instrument universe for thesis monitoring."""

from __future__ import annotations

from typing import Any


def monitored_thesis_rows(connection: Any) -> list[dict[str, Any]]:
    """Load every instrument whose ownership, watch, option, or thesis state needs monitoring."""

    rows = connection.execute(
        """
        SELECT instrument.id AS instrument_id, instrument.symbol, thesis.id AS revision_id,
               thesis.revision, thesis.thesis, thesis.author_kind, thesis.change_rationale,
               thesis.last_assessed_at, thesis.last_human_reviewed_at, thesis.created_at,
               thesis.updated_at,
               (position.instrument_id IS NOT NULL) AS owned,
               (watch.instrument_id IS NOT NULL AND watch.watch_state <> 'excluded') AS watched,
               (option_policy.instrument_id IS NOT NULL) AS options_underwriting,
               position.quantity, position.average_cost,
               quote.price AS latest_price, quote.observed_at AS latest_quote_at,
               catalyst.starts_at AS next_catalyst_at, catalyst.title AS next_catalyst,
               run.status AS latest_automation_status, run.error AS latest_automation_error,
               run.started_at AS latest_automation_started_at
        FROM catalog.instrument instrument
        LEFT JOIN app.thesis thesis ON thesis.instrument_id = instrument.id AND thesis.status = 'current'
        LEFT JOIN app.portfolio_position position ON position.instrument_id = instrument.id
        LEFT JOIN app.watchlist_item watch ON watch.instrument_id = instrument.id
        LEFT JOIN app.option_history_policy option_policy
          ON option_policy.instrument_id = instrument.id
         AND option_policy.collection_tier = 'core'
         AND option_policy.requested_state = 'on'
         AND option_policy.effective_state = 'active'
        LEFT JOIN LATERAL (
            SELECT price, observed_at FROM raw.confirmed_quote quote
            WHERE quote.instrument_id = instrument.id AND quote.available_at <= now()
            ORDER BY observed_at DESC LIMIT 1
        ) quote ON true
        LEFT JOIN LATERAL (
            SELECT starts_at, title FROM app.catalyst
            WHERE instrument_id = instrument.id AND starts_at >= now()
            ORDER BY starts_at ASC LIMIT 1
        ) catalyst ON true
        LEFT JOIN LATERAL (
            SELECT status, error, started_at FROM app.thesis_automation_run
            WHERE instrument_id = instrument.id ORDER BY started_at DESC LIMIT 1
        ) run ON true
        WHERE position.instrument_id IS NOT NULL
           OR (watch.instrument_id IS NOT NULL AND watch.watch_state <> 'excluded')
           OR option_policy.instrument_id IS NOT NULL
           OR thesis.id IS NOT NULL
        ORDER BY instrument.symbol
        """
    ).fetchall()
    return [dict(row) for row in rows]
