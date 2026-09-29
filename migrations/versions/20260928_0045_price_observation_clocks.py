"""Rank confirmed prices by observation freshness and preserve crypto UTC closes."""

from importlib import import_module

from alembic import op

revision = "20260928_0045"
down_revision = "20260928_0044"
branch_labels = None
depends_on = None

_PREVIOUS = "migrations.versions.20260922_0034_current_price_confirmed_tips"


def _function() -> str:
    previous = import_module(_PREVIOUS)
    sql = previous._function()
    replace = previous._replace_once
    sql = replace(
        sql,
        """CASE WHEN candidate.observed_at = candidate.source_latest_observed_at
                          THEN candidate.confirmed_at END DESC NULLS LAST,
                     candidate.confirmed_at DESC, candidate.observed_at DESC""",
        "candidate.observed_at DESC, candidate.confirmed_at DESC",
    )
    # The source-tip index probes still bound quote history. The old extra
    # per-source maximum is no longer an ordering authority (or needed work).
    sql = replace(
        sql,
        """SELECT candidate.*,
                   max(candidate.observed_at) OVER (
                       PARTITION BY candidate.instrument_id, candidate.source_id
                   ) AS source_latest_observed_at""",
        "SELECT candidate.*",
    )
    sql = replace(
        sql,
        """VALUES ((fact.trading_date::timestamp + time '16:00')
                    AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York'))""",
        """VALUES (CASE WHEN instrument.asset_class = 'crypto'
                    THEN (fact.trading_date + 1)::timestamp AT TIME ZONE 'UTC'
                    ELSE (fact.trading_date::timestamp + time '16:00')
                         AT TIME ZONE COALESCE(instrument.market_timezone, 'America/New_York') END)""",
    )
    sql = replace(
        sql,
        "WHERE effective.close_at <= p_as_of",
        """WHERE effective.close_at <= p_as_of
              AND (instrument.asset_class <> 'crypto'
                   OR (fact.observed_at = effective.close_at
                       AND fact.available_at >= effective.close_at
                       AND fact.confirmed_at >= fact.available_at))""",
    )
    sql = replace(
        sql,
        """CASE WHEN source.kind IN ('daily_bars', 'daily_quote')
                        THEN (quote.observed_at AT TIME ZONE 'UTC')::date""",
        """CASE WHEN verified_close.instrument_id IS NOT NULL THEN verified_close.trading_date
                        WHEN source.kind IN ('daily_bars', 'daily_quote')
                        THEN (quote.observed_at AT TIME ZONE 'UTC')::date""",
    )
    sql = replace(
        sql,
        "WHERE quote.price > 0 AND effective.observed_at <= p_as_of",
        """WHERE quote.price > 0 AND effective.observed_at <= p_as_of
              AND (instrument.asset_class <> 'crypto'
                   OR source.kind NOT IN ('daily_bars', 'daily_quote')
                   OR verified_close.instrument_id IS NOT NULL)""",
    )
    return sql


def upgrade() -> None:
    op.execute(_function())


def downgrade() -> None:
    op.execute(import_module(_PREVIOUS)._function())
