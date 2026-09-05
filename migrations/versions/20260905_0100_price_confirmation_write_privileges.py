"""Allow the price collector to confirm facts in the existing projections."""

from __future__ import annotations

from alembic import op


revision = "20260905_0100"
down_revision = "20260905_0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "GRANT INSERT ON TABLE raw.price_bar_confirmation, raw.quote_confirmation, raw.price_bar_fact_availability, raw.quote_fact_availability TO market_app;"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE INSERT ON TABLE raw.price_bar_confirmation, raw.quote_confirmation, raw.price_bar_fact_availability, raw.quote_fact_availability FROM market_app;"
    )
