"""Allow explicit manual cash movements in the append-only ledger."""

from __future__ import annotations

from alembic import op


revision = "20260905_0089"
down_revision = "20260905_0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.portfolio_transaction DROP CONSTRAINT IF EXISTS ck_portfolio_transaction_type;")
    op.execute(
        """
        ALTER TABLE app.portfolio_transaction
        ADD CONSTRAINT ck_portfolio_transaction_type CHECK (transaction_type IN (
            'opening_balance', 'buy', 'sell', 'dividend', 'fee', 'split',
            'transfer_in', 'transfer_out', 'cash_deposit', 'cash_withdrawal'
        ));
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.portfolio_transaction DROP CONSTRAINT IF EXISTS ck_portfolio_transaction_type;")
    op.execute(
        """
        ALTER TABLE app.portfolio_transaction
        ADD CONSTRAINT ck_portfolio_transaction_type CHECK (transaction_type IN (
            'opening_balance', 'buy', 'sell', 'dividend', 'fee', 'split',
            'transfer_in', 'transfer_out'
        ));
        """
    )
