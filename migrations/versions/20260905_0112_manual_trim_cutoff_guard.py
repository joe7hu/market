"""Keep manual trim capacity fail-closed for historical allocation cutoffs."""

from __future__ import annotations

from alembic import op


revision = "20260905_0112"
down_revision = "20260905_0111"
branch_labels = None
depends_on = None


_FUNCTION = """
CREATE OR REPLACE FUNCTION analysis.phase4_funding_source_capacity(
  source_key TEXT, authority_id TEXT, cutoff TIMESTAMPTZ
) RETURNS DOUBLE PRECISION LANGUAGE plpgsql STABLE AS $$
DECLARE capacity DOUBLE PRECISION;
BEGIN
  IF NOT analysis.phase4_account_authority_exists(authority_id, cutoff) THEN
    RETURN NULL;
  END IF;
  IF source_key = 'CASH:' || authority_id THEN
    IF authority_id ~ '^broker-account:[0-9]+$' THEN
      SELECT account.cash_balance::DOUBLE PRECISION INTO capacity
        FROM raw.broker_account_snapshot account
       WHERE ('broker-account:' || account.id::TEXT) = authority_id
         AND account.observed_at <= cutoff;
    ELSE
      SELECT account.cash_balance::DOUBLE PRECISION INTO capacity
        FROM app.manual_account_snapshot account
       WHERE ('manual-account:' || account.id::TEXT) = authority_id
         AND account.reconciliation_state = 'reconciled'
         AND account.effective_at <= cutoff;
    END IF;
    RETURN capacity;
  END IF;
  IF source_key ~ '^TRIM:broker-position:[0-9]+$' THEN
    SELECT abs(position.market_value)::DOUBLE PRECISION INTO capacity
      FROM raw.broker_position_snapshot position
      JOIN raw.broker_account_snapshot account ON account.id = position.account_snapshot_id
     WHERE position.id = split_part(source_key, ':', 3)::BIGINT
       AND ('broker-account:' || account.id::TEXT) = authority_id
       AND account.observed_at <= cutoff
       AND position.quantity > 0 AND position.market_value IS NOT NULL;
    RETURN capacity;
  END IF;
  IF source_key ~ '^TRIM:manual-position:[0-9]+$' THEN
    SELECT position.quantity::DOUBLE PRECISION * quote.price::DOUBLE PRECISION INTO capacity
      FROM app.portfolio_position position
      LEFT JOIN LATERAL (
        SELECT price FROM raw.current_price_at(cutoff, ARRAY[position.instrument_id]::BIGINT[])
        LIMIT 1
      ) quote ON TRUE
     WHERE position.instrument_id = split_part(source_key, ':', 3)::BIGINT
       AND position.quantity > 0
       AND NOT EXISTS (
         SELECT 1 FROM app.portfolio_transaction transaction
          WHERE transaction.instrument_id = position.instrument_id
            AND (transaction.executed_at > cutoff OR transaction.created_at > cutoff)
       );
    RETURN capacity;
  END IF;
  RETURN NULL;
END;
$$;
"""


def upgrade() -> None:
    op.execute(_FUNCTION)


def downgrade() -> None:
    op.execute(_FUNCTION.replace(
        "       AND NOT EXISTS (\n         SELECT 1 FROM app.portfolio_transaction transaction\n          WHERE transaction.instrument_id = position.instrument_id\n            AND (transaction.executed_at > cutoff OR transaction.created_at > cutoff)\n       );\n",
        ";\n",
    ))
