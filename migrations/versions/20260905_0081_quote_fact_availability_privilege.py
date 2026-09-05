"""Allow the current-price authority to verify quote availability."""

from __future__ import annotations

from alembic import op


revision = "20260905_0081"
down_revision = "20260905_0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON TABLE raw.quote_fact_availability TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON TABLE raw.quote_fact_availability FROM market_app;")
