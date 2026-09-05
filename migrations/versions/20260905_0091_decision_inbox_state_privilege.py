"""Allow the application role to update only owner Inbox state."""

from __future__ import annotations

from alembic import op


revision = "20260905_0091"
down_revision = "20260905_0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT UPDATE (
          user_state, snoozed_until, dismiss_reason,
          user_state_updated_at, reviewed_at
        ) ON TABLE app.decision_inbox_item TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE UPDATE (
          user_state, snoozed_until, dismiss_reason,
          user_state_updated_at, reviewed_at
        ) ON TABLE app.decision_inbox_item FROM market_app;
        """
    )
