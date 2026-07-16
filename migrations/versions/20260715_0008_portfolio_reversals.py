"""Enforce atomic reversals and finalize the portfolio ledger contract.

Revision ID: 20260715_0008
Revises: 20260715_0007
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260715_0008"
down_revision = "20260715_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ux_app_portfolio_transaction_reversal",
        "portfolio_transaction",
        ["reverses_transaction_id"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("reverses_transaction_id IS NOT NULL"),
    )
    op.drop_constraint(
        "ck_portfolio_transaction_type",
        "portfolio_transaction",
        schema="app",
        type_="check",
    )
    op.create_check_constraint(
        "ck_portfolio_transaction_type",
        "portfolio_transaction",
        "transaction_type IN ('opening_balance','buy','sell','dividend','fee','split','transfer_in','transfer_out')",
        schema="app",
    )
    op.execute(
        """
        UPDATE app.portfolio_transaction transaction
        SET executed_at = (
            position.purchase_date::timestamp + time '12:00'
        ) AT TIME ZONE 'America/New_York'
        FROM app.portfolio_position position
        WHERE transaction.instrument_id = position.instrument_id
          AND transaction.transaction_type = 'opening_balance'
          AND transaction.idempotency_key = 'opening:' || position.instrument_id::text
          AND position.purchase_date IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_portfolio_transaction_type",
        "portfolio_transaction",
        schema="app",
        type_="check",
    )
    op.create_check_constraint(
        "ck_portfolio_transaction_type",
        "portfolio_transaction",
        "transaction_type IN ('opening_balance','buy','sell','dividend','fee','split','transfer_in','transfer_out')",
        schema="app",
    )
    op.drop_index(
        "ux_app_portfolio_transaction_reversal",
        table_name="portfolio_transaction",
        schema="app",
    )
