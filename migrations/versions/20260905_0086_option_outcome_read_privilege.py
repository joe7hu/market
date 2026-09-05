"""Allow the application role to read option outcome evidence."""

from __future__ import annotations

from alembic import op


revision = "20260905_0086"
down_revision = "20260905_0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON TABLE analysis.option_outcome TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON TABLE analysis.option_outcome FROM market_app;")
