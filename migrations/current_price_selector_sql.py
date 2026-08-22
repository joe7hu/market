"""SQL builders for the point-in-time current-price selector.

The selector must stay available during a forward migration and its rollback.
Keeping the two variants here lets a migration replace only the confirmation
lookup while preserving the externally visible function contract.
"""

from __future__ import annotations


def current_price_selector_sql(
    *,
    use_availability_projection: bool,
    include_legacy_fallback: bool = True,
) -> str:
    """Return the bounded selector SQL for the requested confirmation source."""

    quote_lookup = _confirmation_lookup(
        "quote", use_availability_projection, include_legacy_fallback
    )
    bar_lookup = _confirmation_lookup(
        "price_bar", use_availability_projection, include_legacy_fallback
    )
    return f"""
        CREATE OR REPLACE FUNCTION raw.current_price_for_instruments(
            p_as_of TIMESTAMPTZ,
            p_instrument_ids BIGINT[]
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
                WHERE instrument_id = ANY(p_instrument_ids)
                UNION ALL
                SELECT * FROM raw.quote_history
                WHERE instrument_id = ANY(p_instrument_ids)
            ) fact
            {quote_lookup}
            WHERE fact.available_at <= p_as_of
            ORDER BY fact.instrument_id, fact.source_id, fact.observed_at,
                     fact.available_at DESC
        ),
        confirmed_price_bar AS (
            SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.interval, fact.observed_at)
                   fact.*, confirmation.confirmed_at
            FROM (
                SELECT * FROM raw.price_bar
                WHERE instrument_id = ANY(p_instrument_ids)
                UNION ALL
                SELECT * FROM raw.price_bar_history
                WHERE instrument_id = ANY(p_instrument_ids)
            ) fact
            {bar_lookup}
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


def optimized_current_price_selector_sql(
    *,
    use_availability_projection: bool,
    include_legacy_fallback: bool = True,
) -> str:
    """Return the selector after reducing daily-bar work to selected rows.

    ``current_price_for_instruments`` is called by interactive APIs.  Its
    previous form calculated a previous close for every historical daily bar
    before it chose one current candidate.  A one-symbol request could then
    make thousands of correlated scans.  Keep the same point-in-time fact and
    confirmation rules, but calculate a previous close only for the selected
    daily-bar candidate for each instrument.
    """

    quote_lookup = _confirmation_lookup(
        "quote", use_availability_projection, include_legacy_fallback
    )
    bar_lookup = _confirmation_lookup(
        "price_bar", use_availability_projection, include_legacy_fallback
    )
    return f"""
        CREATE OR REPLACE FUNCTION raw.current_price_for_instruments(
            p_as_of TIMESTAMPTZ,
            p_instrument_ids BIGINT[]
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
        WITH confirmed_quote AS MATERIALIZED (
            SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.observed_at)
                   fact.*, confirmation.confirmed_at
            FROM (
                SELECT * FROM raw.quote
                WHERE instrument_id = ANY(p_instrument_ids)
                UNION ALL
                SELECT * FROM raw.quote_history
                WHERE instrument_id = ANY(p_instrument_ids)
            ) fact
            {quote_lookup}
            WHERE fact.available_at <= p_as_of
            ORDER BY fact.instrument_id, fact.source_id, fact.observed_at,
                     fact.available_at DESC
        ),
        confirmed_daily_bar AS MATERIALIZED (
            SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.trading_date)
                   fact.*, confirmation.confirmed_at
            FROM (
                SELECT * FROM raw.price_bar
                WHERE instrument_id = ANY(p_instrument_ids) AND interval = '1d'
                UNION ALL
                SELECT * FROM raw.price_bar_history
                WHERE instrument_id = ANY(p_instrument_ids) AND interval = '1d'
            ) fact
            {bar_lookup}
            WHERE fact.available_at <= p_as_of
            ORDER BY fact.instrument_id, fact.source_id, fact.trading_date,
                     fact.available_at DESC, fact.observed_at DESC
        ),
        quote_candidates AS MATERIALIZED (
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
                   END AS trading_date,
                   false AS is_price_bar
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
        ),
        bar_candidates AS MATERIALIZED (
            SELECT bar.instrument_id,
                   bar.close AS price,
                   NULL::double precision AS change_pct,
                   NULL::double precision AS change_abs,
                   'USD'::text AS currency,
                   bar.source_id,
                   ((bar.trading_date::timestamp + time '16:00')
                       AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York')) AS observed_at,
                   bar.available_at,
                   bar.confirmed_at,
                   'daily_close'::text AS valuation_status,
                   source.kind AS source_kind,
                   bar.trading_date,
                   true AS is_price_bar
            FROM confirmed_daily_bar bar
            JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
            JOIN ingest.source source ON source.id = bar.source_id
            WHERE bar.close > 0
              AND ((bar.trading_date::timestamp + time '16:00')
                   AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York')) <= p_as_of
        ),
        selected AS MATERIALIZED (
            SELECT DISTINCT ON (candidate.instrument_id) candidate.*
            FROM (
                SELECT * FROM quote_candidates
                UNION ALL
                SELECT * FROM bar_candidates
            ) candidate
            ORDER BY candidate.instrument_id,
                     candidate.confirmed_at DESC,
                     candidate.observed_at DESC,
                     candidate.available_at DESC,
                     CASE candidate.valuation_status WHEN 'market_quote' THEN 0 ELSE 1 END,
                     candidate.source_id
        )
        SELECT selected.instrument_id,
               selected.price,
               CASE
                   WHEN selected.is_price_bar AND previous.close > 0
                       THEN (selected.price / previous.close - 1) * 100
                   ELSE selected.change_pct
               END AS change_pct,
               CASE
                   WHEN selected.is_price_bar AND previous.close IS NOT NULL
                       THEN selected.price - previous.close
                   ELSE selected.change_abs
               END AS change_abs,
               selected.currency,
               selected.source_id,
               selected.observed_at,
               selected.available_at,
               selected.valuation_status,
               selected.source_kind,
               selected.trading_date
        FROM selected
        LEFT JOIN LATERAL (
            SELECT prior.close
            FROM confirmed_daily_bar prior
            WHERE selected.is_price_bar
              AND prior.instrument_id = selected.instrument_id
              AND prior.trading_date < selected.trading_date
            ORDER BY prior.trading_date DESC, prior.available_at DESC,
                     prior.observed_at DESC
            LIMIT 1
        ) previous ON true
        $$
    """


def _confirmation_lookup(
    kind: str,
    use_availability_projection: bool,
    include_legacy_fallback: bool = True,
) -> str:
    if not use_availability_projection:
        return f"""
            CROSS JOIN LATERAL (
                SELECT price_run.finished_at AS confirmed_at
                FROM raw.{kind}_confirmation confirmation
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
        """
    legacy_fallback = "" if not include_legacy_fallback else f"""
            UNION ALL
            SELECT price_run.finished_at AS confirmed_at
            FROM raw.{kind}_confirmation legacy
            JOIN ingest.run price_run ON price_run.id = legacy.ingest_run_id
            WHERE legacy.fact_id = fact.id
              AND legacy.fact_available_at = fact.available_at
              AND price_run.status IN ('succeeded', 'partial')
              AND price_run.finished_at IS NOT NULL
              AND price_run.finished_at <= p_as_of
              AND NOT EXISTS (
                  SELECT 1
                  FROM raw.{kind}_fact_availability availability
                  WHERE availability.fact_id = fact.id
                    AND availability.fact_available_at = fact.available_at
              )
"""
    return f"""
        CROSS JOIN LATERAL (
            SELECT price_run.finished_at AS confirmed_at
            FROM raw.{kind}_fact_availability availability
            JOIN ingest.run price_run
              ON price_run.id = availability.ingest_run_id
             AND price_run.status IN ('succeeded', 'partial')
             AND price_run.finished_at IS NOT NULL
             AND price_run.finished_at <= p_as_of
            WHERE availability.fact_id = fact.id
              AND availability.fact_available_at = fact.available_at

{legacy_fallback}
            ORDER BY confirmed_at
            LIMIT 1
        ) confirmation
    """
