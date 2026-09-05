"""Allow the paper outcome refresh to persist option outcomes."""

from __future__ import annotations

from alembic import op


revision = "20260905_0121"
down_revision = "20260905_0120"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT INSERT, UPDATE ON analysis.option_outcome TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE INSERT, UPDATE ON analysis.option_outcome FROM market_app;")
