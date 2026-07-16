"""Append-only portfolio ledger and transaction-derived position state.

Revision ID: 20260715_0007
Revises: 20260712_0006
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260715_0007"
down_revision = "20260712_0006"
branch_labels = None
depends_on = None


TRANSACTION_TYPES = (
    "opening_balance",
    "buy",
    "sell",
    "dividend",
    "fee",
    "split",
    "transfer_in",
    "transfer_out",
)


def upgrade() -> None:
    op.create_table(
        "portfolio_transaction",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("instrument_id", sa.BigInteger(), sa.ForeignKey("catalog.instrument.id"), nullable=True),
        sa.Column("transaction_type", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 8), nullable=True),
        sa.Column("price", sa.Numeric(20, 6), nullable=True),
        sa.Column("amount", sa.Numeric(20, 6), nullable=True),
        sa.Column("fees", sa.Numeric(20, 6), nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Numeric(20, 6), nullable=False, server_default="0"),
        sa.Column("currency", sa.Text(), nullable=False, server_default="USD"),
        sa.Column("account", sa.Text(), nullable=False, server_default="manual"),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("reverses_transaction_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.portfolio_transaction.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "transaction_type IN ('" + "','".join(TRANSACTION_TYPES) + "')",
            name="ck_portfolio_transaction_type",
        ),
        sa.CheckConstraint("fees >= 0", name="ck_portfolio_transaction_fees_nonnegative"),
        schema="app",
    )
    op.create_index(
        "ix_app_portfolio_transaction_instrument_time",
        "portfolio_transaction",
        ["instrument_id", "executed_at", "created_at"],
        schema="app",
    )
    op.create_index(
        "ix_app_portfolio_transaction_recent",
        "portfolio_transaction",
        [sa.text("executed_at DESC")],
        schema="app",
    )
    op.execute(
        """
        INSERT INTO app.portfolio_transaction
            (instrument_id, transaction_type, quantity, price, amount, fees,
             realized_pnl, executed_at, notes, idempotency_key)
        SELECT position.instrument_id,
               'opening_balance',
               position.quantity,
               COALESCE(position.average_cost, 0),
               position.quantity * COALESCE(position.average_cost, 0),
               0,
               0,
               COALESCE(
                   (position.purchase_date::timestamp + time '12:00') AT TIME ZONE 'America/New_York',
                   position.updated_at
               ),
               COALESCE(position.notes, ''),
               'opening:' || position.instrument_id::text
        FROM app.portfolio_position position
        ON CONFLICT (idempotency_key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_app_portfolio_transaction_recent", table_name="portfolio_transaction", schema="app")
    op.drop_index("ix_app_portfolio_transaction_instrument_time", table_name="portfolio_transaction", schema="app")
    op.drop_table("portfolio_transaction", schema="app")
