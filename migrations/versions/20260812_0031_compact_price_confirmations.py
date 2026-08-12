"""Scope current-price confirmation reads to the requested instruments.

Revision ID: 20260812_0031
Revises: 20260812_0030
"""

from __future__ import annotations

from alembic import op


revision = "20260812_0031"
down_revision = "20260812_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Retention of the legacy 54M-row confirmation table is deliberately a
    # bounded maintenance operation, not one long migration transaction.  This
    # function fix is immediate: it filters fact rows before confirmation
    # lookup, so one ticker or portfolio call never scans every stored bar.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION raw.current_price_at(
            p_as_of TIMESTAMPTZ,
            p_instrument_ids BIGINT[] DEFAULT NULL
        )
        RETURNS TABLE (
            instrument_id BIGINT,
            price DOUBLE PRECISION,
            change_pct DOUBLE PRECISION,
            change_abs DOUBLE PRECISION,
            currency TEXT,
            source_id TEXT,
            observed_at TIMESTAMPTZ,
            available_at TIMESTAMPTZ,
            valuation_status TEXT,
            source_kind TEXT,
            trading_date DATE
        )
        LANGUAGE SQL
        STABLE
        AS $$
        WITH confirmed_quote AS (
            SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.observed_at)
                   fact.*, confirmation.confirmed_at
            FROM (
                SELECT * FROM raw.quote
                WHERE p_instrument_ids IS NULL OR instrument_id = ANY(p_instrument_ids)
                UNION ALL
                SELECT * FROM raw.quote_history
                WHERE p_instrument_ids IS NULL OR instrument_id = ANY(p_instrument_ids)
            ) fact
            CROSS JOIN LATERAL (
                SELECT price_run.finished_at AS confirmed_at
                FROM raw.quote_confirmation confirmation
                JOIN ingest.run price_run ON price_run.id = confirmation.ingest_run_id
                WHERE confirmation.fact_id = fact.id
                  AND confirmation.fact_available_at = fact.available_at
                  AND price_run.status IN ('succeeded', 'partial')
                  AND price_run.finished_at IS NOT NULL
                  AND price_run.finished_at <= p_as_of
                ORDER BY price_run.finished_at, confirmation.confirmed_at,
                         confirmation.ingest_run_id
                LIMIT 1
            ) confirmation
            WHERE fact.available_at <= p_as_of
            ORDER BY fact.instrument_id, fact.source_id, fact.observed_at,
                     fact.available_at DESC
        ),
        confirmed_price_bar AS (
            SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.interval, fact.observed_at)
                   fact.*, confirmation.confirmed_at
            FROM (
                SELECT * FROM raw.price_bar
                WHERE p_instrument_ids IS NULL OR instrument_id = ANY(p_instrument_ids)
                UNION ALL
                SELECT * FROM raw.price_bar_history
                WHERE p_instrument_ids IS NULL OR instrument_id = ANY(p_instrument_ids)
            ) fact
            CROSS JOIN LATERAL (
                SELECT price_run.finished_at AS confirmed_at
                FROM raw.price_bar_confirmation confirmation
                JOIN ingest.run price_run ON price_run.id = confirmation.ingest_run_id
                WHERE confirmation.fact_id = fact.id
                  AND confirmation.fact_available_at = fact.available_at
                  AND price_run.status IN ('succeeded', 'partial')
                  AND price_run.finished_at IS NOT NULL
                  AND price_run.finished_at <= p_as_of
                ORDER BY price_run.finished_at, confirmation.confirmed_at,
                         confirmation.ingest_run_id
                LIMIT 1
            ) confirmation
            WHERE fact.available_at <= p_as_of
            ORDER BY fact.instrument_id, fact.source_id, fact.interval,
                     fact.observed_at, fact.available_at DESC
        ),
        candidates AS (
            SELECT quote.instrument_id,
                   quote.price,
                   quote.change_pct,
                   quote.change_abs,
                   quote.currency,
                   quote.source_id,
                   effective.observed_at,
                   quote.available_at,
                   quote.confirmed_at,
                   CASE
                       WHEN source.kind IN ('daily_bars', 'daily_quote') THEN 'daily_close'::text
                       ELSE 'market_quote'::text
                   END AS valuation_status,
                   source.kind AS source_kind,
                   CASE
                       WHEN source.kind IN ('daily_bars', 'daily_quote')
                           THEN (quote.observed_at AT TIME ZONE 'UTC')::date
                       ELSE (quote.observed_at AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York'))::date
                   END AS trading_date
            FROM confirmed_quote quote
            JOIN catalog.instrument instrument ON instrument.id = quote.instrument_id
            JOIN ingest.source source ON source.id = quote.source_id
            CROSS JOIN LATERAL (
                VALUES (
                    CASE
                        WHEN source.kind IN ('daily_bars', 'daily_quote')
                            THEN ((quote.observed_at AT TIME ZONE 'UTC')::date::timestamp + time '16:00')
                                 AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York')
                        ELSE quote.observed_at
                    END
                )
            ) AS effective(observed_at)
            WHERE quote.price > 0
              AND effective.observed_at <= p_as_of

            UNION ALL

            SELECT bar.instrument_id,
                   bar.close AS price,
                   CASE WHEN previous.close > 0 THEN (bar.close / previous.close - 1) * 100 END AS change_pct,
                   CASE WHEN previous.close IS NOT NULL THEN bar.close - previous.close END AS change_abs,
                   'USD'::text AS currency,
                   bar.source_id,
                   ((bar.trading_date::timestamp + time '16:00')
                       AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York')) AS observed_at,
                   bar.available_at,
                   bar.confirmed_at,
                   'daily_close'::text AS valuation_status,
                   source.kind AS source_kind,
                   bar.trading_date
            FROM confirmed_price_bar bar
            JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
            JOIN ingest.source source ON source.id = bar.source_id
            LEFT JOIN LATERAL (
                SELECT prior.close
                FROM confirmed_price_bar prior
                WHERE prior.instrument_id = bar.instrument_id
                  AND prior.interval = '1d'
                  AND prior.trading_date < bar.trading_date
                ORDER BY prior.trading_date DESC, prior.available_at DESC,
                         prior.observed_at DESC
                LIMIT 1
            ) previous ON true
            WHERE bar.interval = '1d'
              AND bar.close > 0
              AND ((bar.trading_date::timestamp + time '16:00')
                   AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York')) <= p_as_of
        )
        SELECT DISTINCT ON (instrument_id)
               instrument_id, price, change_pct, change_abs, currency, source_id,
               observed_at, available_at, valuation_status, source_kind, trading_date
        FROM candidates
        ORDER BY instrument_id,
                 confirmed_at DESC,
                 observed_at DESC,
                 available_at DESC,
                 CASE valuation_status WHEN 'market_quote' THEN 0 ELSE 1 END,
                 source_id
        $$
        """
    )


def downgrade() -> None:
    # Revision 0026 still owns the base function and remains safe after this
    # bounded-reader refinement.  Its downgrade removes the function.
    pass
