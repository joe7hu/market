"""Allow the application role to read durable decision inbox state."""

from __future__ import annotations

from alembic import op


revision = "20260905_0083"
down_revision = "20260905_0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON TABLE app.decision_inbox_item TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON TABLE app.decision_inbox_item FROM market_app;")
