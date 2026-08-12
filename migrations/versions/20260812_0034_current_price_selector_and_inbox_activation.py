"""Optimize bounded price selection and arm the Decision Inbox from now on.

Revision ID: 20260812_0034
Revises: 20260812_0033
"""

from __future__ import annotations

from alembic import op

from migrations.current_price_selector_sql import (
    current_price_selector_sql,
    optimized_current_price_selector_sql,
)


revision = "20260812_0034"
down_revision = "20260812_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preserve the 0033 point-in-time and confirmation contract.  This form
    # only postpones previous-close computation until after one current fact is
    # selected per instrument, so page readers no longer repeat it thousands of
    # times per symbol.
    op.execute(optimized_current_price_selector_sql(use_availability_projection=True))

    # The Inbox is an event stream, not a historical ticket export.  A first
    # sync records its activation point and does not replay retained SETUPs or
    # already-expired tickets.  Remove only the lifecycle rows produced by the
    # pre-activation poller; paper and critical-risk lifecycle records remain
    # auditable.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.decision_inbox_sync_state (
            state_key TEXT PRIMARY KEY,
            activated_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        DELETE FROM app.notification_outbox notification
        USING app.decision_inbox_item item
        WHERE notification.inbox_item_id = item.id
          AND item.event_type IN ('ready', 'revoked', 'expired')
        """
    )
    op.execute(
        """
        DELETE FROM app.decision_inbox_item
        WHERE event_type IN ('ready', 'revoked', 'expired')
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.decision_inbox_sync_state")
    op.execute(current_price_selector_sql(use_availability_projection=True))
