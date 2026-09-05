"""Allow the application role to maintain the Inbox activation watermark."""

from __future__ import annotations

from alembic import op


revision = "20260905_0094"
down_revision = "20260905_0093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT, INSERT ON TABLE app.decision_inbox_sync_state TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE SELECT, INSERT ON TABLE app.decision_inbox_sync_state FROM market_app;")
