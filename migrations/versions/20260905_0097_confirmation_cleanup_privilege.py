"""Allow ingestion jobs to remove finalized price-confirmation staging rows."""

from __future__ import annotations

from alembic import op


revision = "20260905_0097"
down_revision = "20260905_0096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT, DELETE ON TABLE raw.price_bar_confirmation, raw.quote_confirmation TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE SELECT, DELETE ON TABLE raw.price_bar_confirmation, raw.quote_confirmation FROM market_app;")
