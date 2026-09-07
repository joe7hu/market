"""Use confirmed daily-bar session clocks without advancing legacy prices."""

from alembic import op

revision = "20260907_0005"
down_revision = "20260907_0004"
branch_labels = None
depends_on = None


# Preserve the existing function signature, owner, ACL, source ranking and
# non-daily quote behavior. A failed exact replacement stops the migration.
_PATCHES = (
    (
        "SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.observed_at)",
        "SELECT DISTINCT ON (fact.id)",
    ),
    (
        "ORDER BY fact.instrument_id, fact.source_id, fact.observed_at,\n                     fact.available_at DESC",
        "ORDER BY fact.id, fact.available_at DESC",
    ),
    (
        "        quote_candidates AS MATERIALIZED (",
        """        daily_clocks AS MATERIALIZED (
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
                     AND (
                         (extract(month FROM fact.trading_date) = 11
                          AND extract(isodow FROM fact.trading_date) = 5
                          AND extract(day FROM fact.trading_date) BETWEEN 23 AND 29)
                         OR (extract(isodow FROM fact.trading_date) BETWEEN 1 AND 4
                             AND ((extract(month FROM fact.trading_date) = 7 AND extract(day FROM fact.trading_date) = 3)
                               OR (extract(month FROM fact.trading_date) = 12 AND extract(day FROM fact.trading_date) = 24)))
                     ) THEN fact.observed_at
                    ELSE nominal.close_at
                END)
            ) effective(close_at)
            WHERE effective.close_at <= p_as_of
              AND (instrument.asset_class NOT IN ('equity', 'etf')
                   OR COALESCE(instrument.market_timezone, 'America/New_York') <> 'America/New_York'
                   OR (fact.available_at >= effective.close_at AND fact.confirmed_at >= fact.available_at))
        ),
        quote_candidates AS MATERIALIZED (""",
    ),
    (
        """            FROM confirmed_quote quote
            JOIN catalog.instrument instrument ON instrument.id = quote.instrument_id
            JOIN ingest.source source ON source.id = quote.source_id AND source.enabled AND source.operational_state = 'active'
            CROSS JOIN LATERAL (""",
        """            FROM confirmed_quote quote
            JOIN catalog.instrument instrument ON instrument.id = quote.instrument_id
            JOIN ingest.source source ON source.id = quote.source_id AND source.enabled AND source.operational_state = 'active'
            LEFT JOIN daily_clocks verified_close
              ON source.kind = 'daily_bars' AND verified_close.instrument_id = quote.instrument_id
             AND verified_close.source_id = quote.source_id AND verified_close.ingest_run_id = quote.ingest_run_id
             AND verified_close.observed_at = quote.observed_at AND verified_close.close = quote.price
            CROSS JOIN LATERAL (""",
    ),
    (
        """                        WHEN source.kind IN ('daily_bars', 'daily_quote')
                            THEN ((quote.observed_at AT TIME ZONE 'UTC')::date::timestamp + time '16:00')""",
        """                        WHEN source.kind = 'daily_bars' AND verified_close.instrument_id IS NOT NULL
                            THEN verified_close.effective_close_at
                        WHEN source.kind IN ('daily_bars', 'daily_quote')
                            THEN ((quote.observed_at AT TIME ZONE 'UTC')::date::timestamp + time '16:00')""",
    ),
    (
        """            WHERE quote.price > 0
              AND effective.observed_at <= p_as_of""",
        """            WHERE quote.price > 0
              AND effective.observed_at <= p_as_of
              AND (source.kind <> 'daily_bars' OR instrument.asset_class NOT IN ('equity', 'etf')
                   OR COALESCE(instrument.market_timezone, 'America/New_York') <> 'America/New_York' OR (
                  quote.available_at >= effective.observed_at AND quote.confirmed_at >= quote.available_at
              ))""",
    ),
    (
        """                   ((bar.trading_date::timestamp + time '16:00')
                       AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York')) AS observed_at,""",
        "                   bar.effective_close_at AS observed_at,",
    ),
    (
        """            FROM confirmed_daily_bar bar
            JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id""",
        """            FROM daily_clocks bar
            JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id""",
    ),
    (
        """            WHERE bar.close > 0
              AND ((bar.trading_date::timestamp + time '16:00')
                   AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York')) <= p_as_of""",
        """            WHERE bar.close > 0
              AND bar.effective_close_at <= p_as_of""",
    ),
)


def _replace(*, reverse: bool) -> None:
    connection = op.get_bind()
    definition = connection.exec_driver_sql(
        "SELECT pg_get_functiondef('raw.current_price_for_instruments(timestamptz,bigint[])'::regprocedure)"
    ).scalar_one()
    for before, after in reversed(_PATCHES) if reverse else _PATCHES:
        old, new = (after, before) if reverse else (before, after)
        if definition.count(old) != 1:
            raise RuntimeError("current-price function differs from the reviewed session-clock contract")
        definition = definition.replace(old, new, 1)
    connection.exec_driver_sql(definition)


def upgrade() -> None:
    _replace(reverse=False)


def downgrade() -> None:
    _replace(reverse=True)
