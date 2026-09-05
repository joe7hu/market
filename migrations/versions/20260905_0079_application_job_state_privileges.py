"""Allow the configured application login to maintain operational job state."""

from __future__ import annotations

from alembic import op


revision = "20260905_0079"
down_revision = "20260905_0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT USAGE ON SCHEMA ops TO market_app;
        GRANT SELECT, INSERT, UPDATE ON TABLE ops.job_run TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE SELECT, INSERT, UPDATE ON TABLE ops.job_run FROM market_app;
        REVOKE USAGE ON SCHEMA ops FROM market_app;
        """
    )
