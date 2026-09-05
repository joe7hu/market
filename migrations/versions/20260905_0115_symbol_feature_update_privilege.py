"""Allow the existing symbol-feature writer to refresh conflict rows."""

from __future__ import annotations

from alembic import op


revision = "20260905_0115"
down_revision = "20260905_0114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT UPDATE ON analysis.symbol_feature TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE UPDATE ON analysis.symbol_feature FROM market_app;")
