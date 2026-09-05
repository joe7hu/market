"""Allow the application role to read notification delivery state."""

from __future__ import annotations

from alembic import op


revision = "20260905_0084"
down_revision = "20260905_0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON TABLE app.notification_outbox TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON TABLE app.notification_outbox FROM market_app;")
