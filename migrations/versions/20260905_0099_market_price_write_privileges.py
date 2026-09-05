"""Allow the scheduled market-data collector to maintain versioned price facts."""

from __future__ import annotations

from alembic import op


revision = "20260905_0099"
down_revision = "20260905_0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE raw.price_bar, raw.quote, raw.price_bar_history, raw.quote_history TO market_app;"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE ON TABLE raw.price_bar, raw.quote, raw.price_bar_history, raw.quote_history FROM market_app;"
    )
