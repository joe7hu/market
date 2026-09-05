"""Allow the application role to read option-history canary evidence."""

from __future__ import annotations

from alembic import op


revision = "20260905_0088"
down_revision = "20260905_0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON TABLE analysis.option_history_canary TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON TABLE analysis.option_history_canary FROM market_app;")
