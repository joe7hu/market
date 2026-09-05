"""Allow the application role to read option-recovery session quality."""

from __future__ import annotations

from alembic import op


revision = "20260905_0109"
down_revision = "20260905_0108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON TABLE analysis.option_recovery_event_session_quality TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON TABLE analysis.option_recovery_event_session_quality FROM market_app;")
