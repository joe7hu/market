"""Allow strategy routing to read saved option thesis expressions."""

from __future__ import annotations

from alembic import op


revision = "20260905_0117"
down_revision = "20260905_0116"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON app.thesis_expression TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON app.thesis_expression FROM market_app;")
