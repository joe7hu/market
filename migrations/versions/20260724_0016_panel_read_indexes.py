"""Indexes for bounded portfolio and current-price panel reads.

Revision ID: 20260724_0016
Revises: 20260722_0015
"""

from alembic import op


revision = "20260724_0016"
down_revision = "20260722_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_raw_price_bar_history_panel_latest "
        "ON raw.price_bar_history (instrument_id, interval, trading_date DESC, observed_at DESC) "
        "INCLUDE (close, available_at, source_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_raw_quote_history_panel_latest "
        "ON raw.quote_history (instrument_id, observed_at DESC, available_at DESC) "
        "INCLUDE (price, change_pct, change_abs, source_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_raw_quote_panel_latest "
        "ON raw.quote (instrument_id, observed_at DESC, available_at DESC) "
        "INCLUDE (price, change_pct, change_abs, source_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS raw.ix_raw_quote_panel_latest")
    op.execute("DROP INDEX IF EXISTS raw.ix_raw_quote_history_panel_latest")
    op.execute("DROP INDEX IF EXISTS raw.ix_raw_price_bar_history_panel_latest")
