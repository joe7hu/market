"""Persist the canonical point-in-time opportunity episode envelope.

Revision ID: 20260824_0054
Revises: 20260823_0053
"""

from __future__ import annotations

from alembic import op


revision = "20260824_0054"
down_revision = "20260823_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE analysis.ticker_decision
            ADD COLUMN IF NOT EXISTS opportunity_episode_id TEXT,
            ADD COLUMN IF NOT EXISTS opportunity_cutoff TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS opportunity_episode JSONB NOT NULL DEFAULT '{}'::jsonb;
        CREATE INDEX IF NOT EXISTS ix_ticker_decision_opportunity_episode
            ON analysis.ticker_decision (opportunity_episode_id, opportunity_cutoff DESC);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS analysis.ix_ticker_decision_opportunity_episode;
        ALTER TABLE analysis.ticker_decision
            DROP COLUMN IF EXISTS opportunity_episode,
            DROP COLUMN IF EXISTS opportunity_cutoff,
            DROP COLUMN IF EXISTS opportunity_episode_id;
        """
    )
