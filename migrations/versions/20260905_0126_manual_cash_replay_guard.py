"""Use replayed manual cash for Phase 4 funding capacity."""

from __future__ import annotations

from alembic import op


revision = "20260905_0126"
down_revision = "20260905_0125"
branch_labels = None
depends_on = None


_FUNDING_FUNCTION = """
CREATE OR REPLACE FUNCTION analysis.phase4_funding_source_capacity(
  source_key TEXT, authority_id TEXT, cutoff TIMESTAMPTZ
) RETURNS DOUBLE PRECISION LANGUAGE plpgsql STABLE AS $$
DECLARE
  capacity DOUBLE PRECISION;
  snapshot_effective_at TIMESTAMPTZ;
  cash_change DOUBLE PRECISION;
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
      RETURN capacity;
    END IF;
    SELECT account.cash_balance::DOUBLE PRECISION, account.effective_at
      INTO capacity, snapshot_effective_at
      FROM app.manual_account_snapshot account
     WHERE ('manual-account:' || account.id::TEXT) = authority_id
       AND account.reconciliation_state = 'reconciled'
       AND account.effective_at <= cutoff
       AND account.recorded_at <= cutoff;
    IF capacity IS NULL OR snapshot_effective_at IS NULL THEN
      RETURN NULL;
    END IF;
    SELECT COALESCE(SUM(
        CASE
          WHEN transaction.transaction_type IN ('cash_deposit', 'dividend', 'sell')
            THEN COALESCE(transaction.amount, 0) - COALESCE(transaction.fees, 0)
          WHEN transaction.transaction_type IN ('cash_withdrawal', 'fee', 'buy')
            THEN -(COALESCE(transaction.amount, 0) + COALESCE(transaction.fees, 0))
          ELSE 0
        END
      ), 0)
      INTO cash_change
      FROM app.portfolio_transaction transaction
     WHERE transaction.executed_at > snapshot_effective_at
       AND transaction.executed_at <= cutoff
       AND transaction.created_at <= cutoff
       AND transaction.reverses_transaction_id IS NULL
       AND NOT EXISTS (
         SELECT 1 FROM app.portfolio_transaction reversal
          WHERE reversal.reverses_transaction_id = transaction.id
            AND reversal.executed_at <= cutoff
            AND reversal.created_at <= cutoff
       );
    capacity := capacity + cash_change;
    SELECT COALESCE(SUM(
        CASE
          WHEN original.transaction_type IN ('cash_deposit', 'dividend', 'sell')
            THEN COALESCE(original.amount, 0) - COALESCE(original.fees, 0)
          WHEN original.transaction_type IN ('cash_withdrawal', 'fee', 'buy')
            THEN -(COALESCE(original.amount, 0) + COALESCE(original.fees, 0))
          ELSE 0
        END
      ), 0)
      INTO cash_change
      FROM app.portfolio_transaction original
      JOIN app.portfolio_transaction reversal
        ON reversal.reverses_transaction_id = original.id
     WHERE original.reverses_transaction_id IS NULL
       AND original.executed_at <= snapshot_effective_at
       AND original.created_at <= cutoff
       AND reversal.executed_at > snapshot_effective_at
       AND reversal.executed_at <= cutoff
       AND reversal.created_at <= cutoff;
    RETURN capacity - cash_change;
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


_LEGACY_FUNDING_FUNCTION = _FUNDING_FUNCTION.replace(
    "DECLARE\n  capacity DOUBLE PRECISION;\n  snapshot_effective_at TIMESTAMPTZ;\n  cash_change DOUBLE PRECISION;\n",
    "DECLARE\n  capacity DOUBLE PRECISION;\n",
).replace(
    "    SELECT account.cash_balance::DOUBLE PRECISION, account.effective_at\n      INTO capacity, snapshot_effective_at\n      FROM app.manual_account_snapshot account\n     WHERE ('manual-account:' || account.id::TEXT) = authority_id\n       AND account.reconciliation_state = 'reconciled'\n       AND account.effective_at <= cutoff\n       AND account.recorded_at <= cutoff;\n    IF capacity IS NULL OR snapshot_effective_at IS NULL THEN\n      RETURN NULL;\n    END IF;\n    SELECT COALESCE(SUM(\n        CASE\n          WHEN transaction.transaction_type IN ('cash_deposit', 'dividend', 'sell')\n            THEN COALESCE(transaction.amount, 0) - COALESCE(transaction.fees, 0)\n          WHEN transaction.transaction_type IN ('cash_withdrawal', 'fee', 'buy')\n            THEN -(COALESCE(transaction.amount, 0) + COALESCE(transaction.fees, 0))\n          ELSE 0\n        END\n      ), 0)\n      INTO cash_change\n      FROM app.portfolio_transaction transaction\n     WHERE transaction.executed_at > snapshot_effective_at\n       AND transaction.executed_at <= cutoff\n       AND transaction.created_at <= cutoff\n       AND transaction.reverses_transaction_id IS NULL\n       AND NOT EXISTS (\n         SELECT 1 FROM app.portfolio_transaction reversal\n          WHERE reversal.reverses_transaction_id = transaction.id\n            AND reversal.executed_at <= cutoff\n            AND reversal.created_at <= cutoff\n       );\n    capacity := capacity + cash_change;\n    SELECT COALESCE(SUM(\n        CASE\n          WHEN original.transaction_type IN ('cash_deposit', 'dividend', 'sell')\n            THEN COALESCE(original.amount, 0) - COALESCE(original.fees, 0)\n          WHEN original.transaction_type IN ('cash_withdrawal', 'fee', 'buy')\n            THEN -(COALESCE(original.amount, 0) + COALESCE(original.fees, 0))\n          ELSE 0\n        END\n      ), 0)\n      INTO cash_change\n      FROM app.portfolio_transaction original\n      JOIN app.portfolio_transaction reversal\n        ON reversal.reverses_transaction_id = original.id\n     WHERE original.reverses_transaction_id IS NULL\n       AND original.executed_at <= snapshot_effective_at\n       AND original.created_at <= cutoff\n       AND reversal.executed_at > snapshot_effective_at\n       AND reversal.executed_at <= cutoff\n       AND reversal.created_at <= cutoff;\n    RETURN capacity - cash_change;\n",
    "    SELECT account.cash_balance::DOUBLE PRECISION INTO capacity\n      FROM app.manual_account_snapshot account\n     WHERE ('manual-account:' || account.id::TEXT) = authority_id\n       AND account.reconciliation_state = 'reconciled'\n       AND account.effective_at <= cutoff\n       AND account.recorded_at <= cutoff;\n    RETURN capacity;\n",
)


def upgrade() -> None:
    op.execute(_FUNDING_FUNCTION)


def downgrade() -> None:
    op.execute(_LEGACY_FUNDING_FUNCTION)
