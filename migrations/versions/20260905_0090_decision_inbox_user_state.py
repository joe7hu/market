"""Persist owner action state on decision Inbox events."""

from __future__ import annotations

from alembic import op


revision = "20260905_0090"
down_revision = "20260905_0089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.decision_inbox_item
          ADD COLUMN user_state TEXT NOT NULL DEFAULT 'open',
          ADD COLUMN snoozed_until TIMESTAMPTZ,
          ADD COLUMN dismiss_reason TEXT,
          ADD COLUMN user_state_updated_at TIMESTAMPTZ,
          ADD COLUMN reviewed_at TIMESTAMPTZ,
          ADD CONSTRAINT ck_decision_inbox_user_state
            CHECK (user_state IN ('open', 'acknowledged', 'snoozed', 'dismissed', 'review_complete')),
          ADD CONSTRAINT ck_decision_inbox_snooze_state
            CHECK (user_state <> 'snoozed' OR snoozed_until IS NOT NULL),
          ADD CONSTRAINT ck_decision_inbox_dismiss_reason
            CHECK (user_state <> 'dismissed' OR NULLIF(BTRIM(dismiss_reason), '') IS NOT NULL);
        CREATE INDEX ix_decision_inbox_user_state
          ON app.decision_inbox_item (user_state, snoozed_until, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.ix_decision_inbox_user_state;")
    op.execute(
        """
        ALTER TABLE app.decision_inbox_item
          DROP CONSTRAINT IF EXISTS ck_decision_inbox_dismiss_reason,
          DROP CONSTRAINT IF EXISTS ck_decision_inbox_snooze_state,
          DROP CONSTRAINT IF EXISTS ck_decision_inbox_user_state,
          DROP COLUMN IF EXISTS reviewed_at,
          DROP COLUMN IF EXISTS user_state_updated_at,
          DROP COLUMN IF EXISTS dismiss_reason,
          DROP COLUMN IF EXISTS snoozed_until,
          DROP COLUMN IF EXISTS user_state;
        """
    )
