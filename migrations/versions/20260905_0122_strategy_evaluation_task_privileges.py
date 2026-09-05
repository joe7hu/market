"""Allow deterministic strategy evaluation to read and close agent tasks."""

from __future__ import annotations

from alembic import op


revision = "20260905_0122"
down_revision = "20260905_0121"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT, UPDATE ON analysis.agent_task TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE UPDATE ON analysis.agent_task FROM market_app;")
