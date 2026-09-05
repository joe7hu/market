"""Allow confirmed price facts to maintain their bounded projections."""

from __future__ import annotations

from alembic import op


revision = "20260905_0101"
down_revision = "20260905_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "GRANT SELECT, UPDATE ON TABLE raw.price_bar_fact_availability, raw.quote_fact_availability TO market_app;"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT, UPDATE ON TABLE raw.price_bar_fact_availability, raw.quote_fact_availability FROM market_app;"
    )
