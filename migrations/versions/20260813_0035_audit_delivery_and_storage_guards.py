"""Add non-executable research delivery to the audit-safe Inbox contract.

Revision ID: 20260813_0035
Revises: 20260812_0034
"""

from __future__ import annotations

from alembic import op


revision = "20260813_0035"
down_revision = "20260812_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.decision_inbox_item
          DROP CONSTRAINT IF EXISTS decision_inbox_item_event_type_check;
        ALTER TABLE app.decision_inbox_item
          DROP CONSTRAINT IF EXISTS ck_app_decision_inbox_event_type;
        ALTER TABLE app.decision_inbox_item
          ADD CONSTRAINT ck_app_decision_inbox_event_type
          CHECK (event_type IN (
            'ready', 'revoked', 'expired', 'paper_filled', 'paper_exited',
            'portfolio_critical', 'paper_engine_halt', 'high_priority_research'
          ));
        """
    )


def downgrade() -> None:
    # The old schema cannot represent research delivery.  Remove only these
    # derived Inbox rows before restoring its stricter enum-like constraint.
    op.execute("DELETE FROM app.decision_inbox_item WHERE event_type = 'high_priority_research'")
    op.execute("ALTER TABLE app.decision_inbox_item DROP CONSTRAINT IF EXISTS ck_app_decision_inbox_event_type")
    op.execute(
        """
        ALTER TABLE app.decision_inbox_item
          ADD CONSTRAINT decision_inbox_item_event_type_check
          CHECK (event_type IN (
            'ready', 'revoked', 'expired', 'paper_filled', 'paper_exited',
            'portfolio_critical', 'paper_engine_halt'
          ));
        """
    )
