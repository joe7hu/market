"""Allow the scheduled recovery job to refresh session quality rows."""

from __future__ import annotations

from alembic import op


revision = "20260905_0110"
down_revision = "20260905_0109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON TABLE analysis.option_recovery_event_session_quality TO market_app;"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE INSERT, UPDATE ON TABLE analysis.option_recovery_event_session_quality FROM market_app;"
    )
