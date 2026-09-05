"""Allow outcome attribution to inspect paper-order legs."""

from __future__ import annotations

from alembic import op


revision = "20260905_0111"
down_revision = "20260905_0110"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON TABLE app.paper_order_leg TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON TABLE app.paper_order_leg FROM market_app;")
