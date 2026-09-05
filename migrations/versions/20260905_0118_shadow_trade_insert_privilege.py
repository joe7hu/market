"""Allow the options publisher to open shadow observations."""

from __future__ import annotations

from alembic import op


revision = "20260905_0118"
down_revision = "20260905_0117"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT INSERT ON analysis.shadow_trade TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE INSERT ON analysis.shadow_trade FROM market_app;")
