"""Persist conservative generic paper-order execution lifecycle fields.

Revision ID: 20260812_0030
Revises: 20260812_0029
"""

from __future__ import annotations

from alembic import op


revision = "20260812_0030"
down_revision = "20260812_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.paper_order
          ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS filled_quantity NUMERIC(24, 8),
          ADD COLUMN IF NOT EXISTS exited_quantity NUMERIC(24, 8) NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS entry_slippage NUMERIC(20, 6),
          ADD COLUMN IF NOT EXISTS exit_slippage NUMERIC(20, 6),
          ADD COLUMN IF NOT EXISTS unfilled_reason TEXT;

        UPDATE app.paper_order
        SET filled_quantity = quantity
        WHERE filled_quantity IS NULL
          AND status IN ('entered', 'partial_exited', 'exited', 'invalidated');
        """
    )
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_app_paper_order_generic_execution
            ON app.paper_order (lane, status, created_at, id)
            WHERE event_id IS NULL
            """
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.ix_app_paper_order_generic_execution")
    for column in (
        "unfilled_reason", "exit_slippage", "entry_slippage", "exited_quantity",
        "filled_quantity", "submitted_at",
    ):
        op.execute(f"ALTER TABLE app.paper_order DROP COLUMN IF EXISTS {column}")
