"""Prefer the newest observed, confirmed price fact over a late old replay."""

from alembic import op


revision = "20260921_0030"
down_revision = "20260921_0029"
branch_labels = None
depends_on = None


def _function(*, information_time_first: bool) -> str:
    latest_source_candidates = """
        ), source_latest_candidates AS MATERIALIZED (
            SELECT candidate.*,
                   max(candidate.observed_at) OVER (
                       PARTITION BY candidate.instrument_id, candidate.source_id
                   ) AS source_latest_observed_at
            FROM (SELECT * FROM quote_candidates UNION ALL SELECT * FROM bar_candidates) candidate
    """ if information_time_first else ""
    selected_source = "source_latest_candidates candidate" if information_time_first else "(SELECT * FROM quote_candidates UNION ALL SELECT * FROM bar_candidates) candidate"
    order = """CASE WHEN candidate.observed_at = candidate.source_latest_observed_at
                          THEN candidate.confirmed_at END DESC NULLS LAST,
                     candidate.confirmed_at DESC, candidate.observed_at DESC""" if information_time_first else "candidate.confirmed_at DESC, candidate.observed_at DESC"
    return f"""
        CREATE OR REPLACE FUNCTION raw.current_price_for_instruments(
            p_as_of timestamp with time zone, p_instrument_ids bigint[]
        ) RETURNS TABLE(
            instrument_id bigint, price double precision, change_pct double precision,
            change_abs double precision, currency text, source_id text,
            observed_at timestamp with time zone, available_at timestamp with time zone,
            valuation_status text, source_kind text, trading_date date
        ) LANGUAGE sql STABLE AS $$
        WITH confirmed_quote AS MATERIALIZED (
            SELECT DISTINCT ON (fact.id) fact.*, confirmation.confirmed_at
            FROM (
                SELECT * FROM raw.quote WHERE instrument_id = ANY(p_instrument_ids)
                UNION ALL
                SELECT * FROM raw.quote_history WHERE instrument_id = ANY(p_instrument_ids)
            ) fact
            CROSS JOIN LATERAL (
                SELECT price_run.finished_at AS confirmed_at
                FROM raw.quote_fact_availability availability
                JOIN ingest.run price_run ON price_run.id = availability.ingest_run_id
                    AND price_run.status IN ('succeeded', 'partial')
                    AND price_run.finished_at IS NOT NULL AND price_run.finished_at <= p_as_of
                WHERE availability.fact_id = fact.id
                    AND availability.fact_available_at = fact.available_at
                ORDER BY confirmed_at LIMIT 1
            ) confirmation
            WHERE fact.available_at <= p_as_of
            ORDER BY fact.id, fact.available_at DESC
        ), confirmed_daily_bar AS MATERIALIZED (
            SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.trading_date)
                   fact.*, confirmation.confirmed_at
            FROM (
                SELECT * FROM raw.price_bar
                WHERE instrument_id = ANY(p_instrument_ids) AND interval = '1d'
                UNION ALL
                SELECT * FROM raw.price_bar_history
                WHERE instrument_id = ANY(p_instrument_ids) AND interval = '1d'
            ) fact
            CROSS JOIN LATERAL (
                SELECT price_run.finished_at AS confirmed_at
                FROM raw.price_bar_fact_availability availability
                JOIN ingest.run price_run ON price_run.id = availability.ingest_run_id
                    AND price_run.status IN ('succeeded', 'partial')
                    AND price_run.finished_at IS NOT NULL AND price_run.finished_at <= p_as_of
                WHERE availability.fact_id = fact.id
                    AND availability.fact_available_at = fact.available_at
                ORDER BY confirmed_at LIMIT 1
            ) confirmation
            WHERE fact.available_at <= p_as_of
            ORDER BY fact.instrument_id, fact.source_id, fact.trading_date,
                     fact.available_at DESC, fact.observed_at DESC
        ), daily_clocks AS MATERIALIZED (
            SELECT fact.*, effective.close_at AS effective_close_at
            FROM confirmed_daily_bar fact
            JOIN catalog.instrument instrument ON instrument.id = fact.instrument_id
            CROSS JOIN LATERAL (
                VALUES ((fact.trading_date::timestamp + time '16:00')
                    AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York'))
            ) nominal(close_at)
            CROSS JOIN LATERAL (
                VALUES (CASE
                    WHEN fact.observed_at = nominal.close_at THEN fact.observed_at
                    WHEN instrument.asset_class IN ('equity', 'etf')
                     AND COALESCE(instrument.market_timezone, 'America/New_York') = 'America/New_York'
                     AND (fact.observed_at AT TIME ZONE 'America/New_York')::date = fact.trading_date
                     AND (fact.observed_at AT TIME ZONE 'America/New_York')::time = time '13:00'
                     AND ((extract(month FROM fact.trading_date) = 11
                           AND extract(isodow FROM fact.trading_date) = 5
                           AND extract(day FROM fact.trading_date) BETWEEN 23 AND 29)
                          OR (extract(isodow FROM fact.trading_date) BETWEEN 1 AND 4
                              AND ((extract(month FROM fact.trading_date) = 7 AND extract(day FROM fact.trading_date) = 3)
                                OR (extract(month FROM fact.trading_date) = 12 AND extract(day FROM fact.trading_date) = 24))))
                     THEN fact.observed_at
                    ELSE nominal.close_at
                END)
            ) effective(close_at)
            WHERE effective.close_at <= p_as_of
              AND (instrument.asset_class NOT IN ('equity', 'etf')
                   OR COALESCE(instrument.market_timezone, 'America/New_York') <> 'America/New_York'
                   OR (fact.available_at >= effective.close_at AND fact.confirmed_at >= fact.available_at))
        ), quote_candidates AS MATERIALIZED (
            SELECT quote.instrument_id, quote.price, quote.change_pct, quote.change_abs,
                   quote.currency, quote.source_id, effective.observed_at,
                   quote.available_at, quote.confirmed_at,
                   CASE WHEN source.kind IN ('daily_bars', 'daily_quote') THEN 'daily_close'::text
                        ELSE 'market_quote'::text END AS valuation_status,
                   source.kind AS source_kind,
                   CASE WHEN source.kind IN ('daily_bars', 'daily_quote')
                        THEN (quote.observed_at AT TIME ZONE 'UTC')::date
                        ELSE (quote.observed_at AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York'))::date
                   END AS trading_date,
                   false AS is_price_bar
            FROM confirmed_quote quote
            JOIN catalog.instrument instrument ON instrument.id = quote.instrument_id
            JOIN ingest.source source ON source.id = quote.source_id AND source.enabled AND source.operational_state = 'active'
            LEFT JOIN daily_clocks verified_close
              ON source.kind = 'daily_bars' AND verified_close.instrument_id = quote.instrument_id
             AND verified_close.source_id = quote.source_id AND verified_close.ingest_run_id = quote.ingest_run_id
             AND verified_close.observed_at = quote.observed_at AND verified_close.close = quote.price
            CROSS JOIN LATERAL (
                VALUES (CASE
                    WHEN source.kind = 'daily_bars' AND verified_close.instrument_id IS NOT NULL
                        THEN verified_close.effective_close_at
                    WHEN source.kind IN ('daily_bars', 'daily_quote')
                        THEN ((quote.observed_at AT TIME ZONE 'UTC')::date::timestamp + time '16:00')
                             AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York')
                    ELSE quote.observed_at
                END)
            ) effective(observed_at)
            WHERE quote.price > 0 AND effective.observed_at <= p_as_of
              AND (source.kind <> 'daily_bars' OR instrument.asset_class NOT IN ('equity', 'etf')
                   OR COALESCE(instrument.market_timezone, 'America/New_York') <> 'America/New_York'
                   OR (quote.available_at >= effective.observed_at AND quote.confirmed_at >= quote.available_at))
        ), bar_candidates AS MATERIALIZED (
            SELECT bar.instrument_id, bar.close AS price, NULL::double precision AS change_pct,
                   NULL::double precision AS change_abs, 'USD'::text AS currency, bar.source_id,
                   bar.effective_close_at AS observed_at, bar.available_at, bar.confirmed_at,
                   'daily_close'::text AS valuation_status, source.kind AS source_kind,
                   bar.trading_date, true AS is_price_bar
            FROM daily_clocks bar
            JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
            JOIN ingest.source source ON source.id = bar.source_id AND source.enabled AND source.operational_state = 'active'
            WHERE bar.close > 0 AND bar.effective_close_at <= p_as_of
        {latest_source_candidates}), selected AS MATERIALIZED (
            SELECT DISTINCT ON (candidate.instrument_id) candidate.*
            FROM {selected_source}
            ORDER BY candidate.instrument_id, {order}, candidate.available_at DESC,
                     CASE candidate.valuation_status WHEN 'market_quote' THEN 0 ELSE 1 END,
                     candidate.source_id
        )
        SELECT selected.instrument_id, selected.price,
               CASE WHEN selected.is_price_bar AND previous.close > 0
                    THEN (selected.price / previous.close - 1) * 100 ELSE selected.change_pct END AS change_pct,
               CASE WHEN selected.is_price_bar AND previous.close IS NOT NULL
                    THEN selected.price - previous.close ELSE selected.change_abs END AS change_abs,
               selected.currency, selected.source_id, selected.observed_at, selected.available_at,
               selected.valuation_status, selected.source_kind, selected.trading_date
        FROM selected
        LEFT JOIN LATERAL (
            SELECT prior.close FROM confirmed_daily_bar prior
            WHERE selected.is_price_bar AND prior.instrument_id = selected.instrument_id
              AND prior.trading_date < selected.trading_date
            ORDER BY prior.trading_date DESC, prior.available_at DESC, prior.observed_at DESC LIMIT 1
        ) previous ON true
        $$;
    """


def upgrade() -> None:
    op.execute(_function(information_time_first=True))


def downgrade() -> None:
    op.execute(_function(information_time_first=False))
