"""Persist the canonical decision resolution and compiled policy version.

Revision ID: 20260823_0053
Revises: 20260823_0052
"""

from __future__ import annotations

from alembic import op


revision = "20260823_0053"
down_revision = "20260823_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE analysis.ticker_decision
            ADD COLUMN IF NOT EXISTS resolution JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS policy_version TEXT NOT NULL DEFAULT 'risk-policy.v2:legacy';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE analysis.ticker_decision
            DROP COLUMN IF EXISTS policy_version,
            DROP COLUMN IF EXISTS resolution;
        """
    )
