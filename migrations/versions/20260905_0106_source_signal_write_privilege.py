"""Allow scheduled source analysis to persist normalized signals."""

from __future__ import annotations

from alembic import op


revision = "20260905_0106"
down_revision = "20260905_0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE analysis.source_signal TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE SELECT, INSERT, UPDATE ON TABLE analysis.source_signal FROM market_app;")
