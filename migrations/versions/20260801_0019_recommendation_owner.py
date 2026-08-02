"""Consolidate recommendations under canonical option trade tickets.

Revision ID: 20260801_0019
Revises: 20260730_0018
"""

from __future__ import annotations

from alembic import op


revision = "20260801_0019"
down_revision = "20260730_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preserve historical broker publications for audit, but ensure the stale
    # misnamed recommendation surface can no longer be the current authority.
    op.execute(
        """
        UPDATE app.publication
        SET status = 'superseded'
        WHERE scope = 'broker' AND status = 'published'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_app_thesis_expression_active_kind
        ON app.thesis_expression (thesis_revision_id, expression_kind)
        WHERE status = 'active'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.uq_app_thesis_expression_active_kind")
