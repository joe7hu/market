"""Make current-price reads point-in-time safe and bound publication history.

Revision ID: 20260812_0026
Revises: 20260803_0025
"""

from __future__ import annotations

from alembic import op


revision = "20260812_0026"
down_revision = "20260803_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A single published generation is an authority invariant.  Older runtimes
    # could leave more than one row published after an interrupted deploy; keep
    # the newest row and preserve every older generation as auditable history.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY scope
                       ORDER BY published_at DESC NULLS LAST, created_at DESC, id DESC
                   ) AS position
            FROM app.publication
            WHERE status = 'published'
        )
        UPDATE app.publication publication
        SET status = 'superseded'
        FROM ranked
        WHERE publication.id = ranked.id AND ranked.position > 1
        """
    )
    # These tables are large in the production runtime.  The DDL must not hold
    # a write lock over a trading session, so keep the transaction-scoped data
    # repair above and create the two authority indexes concurrently in their
    # own autocommit block.  ``publication_item`` already has the efficient
    # ``(publication_id, model_name, rank)`` index.  The reader below begins
    # from the one current publication per scope, so a second 20M-row index
    # keyed by model would be redundant and can exceed the host disk budget.
    # The quote and price-bar panel indexes likewise already exist from 0016.
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_app_publication_one_published_scope
            ON app.publication (scope)
            WHERE status = 'published'
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_app_publication_scope_status_created
            ON app.publication (scope, status, published_at DESC, created_at DESC)
            INCLUDE (id, analysis_run_id)
            """
        )
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
                UNION ALL
                SELECT * FROM raw.quote_history
            ) fact
            CROSS JOIN LATERAL (
                SELECT min(price_run.finished_at) AS confirmed_at
                FROM raw.quote_confirmation confirmation
                JOIN ingest.run price_run ON price_run.id = confirmation.ingest_run_id
                WHERE confirmation.fact_id = fact.id
                  AND confirmation.fact_available_at = fact.available_at
                  AND price_run.status IN ('succeeded', 'partial')
                  AND price_run.finished_at IS NOT NULL
            ) confirmation
            WHERE fact.available_at <= p_as_of
              AND confirmation.confirmed_at <= p_as_of
              AND (p_instrument_ids IS NULL OR fact.instrument_id = ANY(p_instrument_ids))
            ORDER BY fact.instrument_id, fact.source_id, fact.observed_at, fact.available_at DESC
        ),
        confirmed_price_bar AS (
            SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.interval, fact.observed_at)
                   fact.*, confirmation.confirmed_at
            FROM (
                SELECT * FROM raw.price_bar
                UNION ALL
                SELECT * FROM raw.price_bar_history
            ) fact
            CROSS JOIN LATERAL (
                SELECT min(price_run.finished_at) AS confirmed_at
                FROM raw.price_bar_confirmation confirmation
                JOIN ingest.run price_run ON price_run.id = confirmation.ingest_run_id
                WHERE confirmation.fact_id = fact.id
                  AND confirmation.fact_available_at = fact.available_at
                  AND price_run.status IN ('succeeded', 'partial')
                  AND price_run.finished_at IS NOT NULL
            ) confirmation
            WHERE fact.available_at <= p_as_of
              AND confirmation.confirmed_at <= p_as_of
              AND (p_instrument_ids IS NULL OR fact.instrument_id = ANY(p_instrument_ids))
            ORDER BY fact.instrument_id, fact.source_id, fact.interval, fact.observed_at, fact.available_at DESC
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
              AND quote.available_at <= p_as_of
              AND effective.observed_at <= p_as_of
              AND (p_instrument_ids IS NULL OR quote.instrument_id = ANY(p_instrument_ids))

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
                  AND prior.available_at <= p_as_of
                ORDER BY prior.trading_date DESC, prior.available_at DESC, prior.observed_at DESC
                LIMIT 1
            ) previous ON true
            WHERE bar.interval = '1d'
              AND bar.close > 0
              AND bar.available_at <= p_as_of
              AND ((bar.trading_date::timestamp + time '16:00')
                   AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York')) <= p_as_of
              AND (p_instrument_ids IS NULL OR bar.instrument_id = ANY(p_instrument_ids))
        )
        SELECT DISTINCT ON (instrument_id)
               instrument_id, price, change_pct, change_abs, currency, source_id,
               observed_at, available_at, valuation_status, source_kind, trading_date
        FROM candidates
        -- "Current" is an information-time decision.  A daily bar carries a
        -- nominal 16:00 close for session semantics, but it must never outrank
        -- a later available intraday broker quote just because its nominal
        -- observation timestamp is greater.
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
    op.execute("DROP FUNCTION IF EXISTS raw.current_price_at(TIMESTAMPTZ, BIGINT[])")
    op.execute("DROP INDEX IF EXISTS app.ix_app_publication_scope_status_created")
    op.execute("DROP INDEX IF EXISTS app.uq_app_publication_one_published_scope")
