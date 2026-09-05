"""Allow the application role to read panel task and broker snapshots."""

from __future__ import annotations

from alembic import op


revision = "20260905_0082"
down_revision = "20260905_0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT SELECT ON TABLE analysis.agent_task, raw.broker_position_snapshot TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE SELECT ON TABLE analysis.agent_task, raw.broker_position_snapshot FROM market_app;
        """
    )
