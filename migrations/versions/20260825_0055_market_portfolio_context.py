"""Persist the point-in-time market and portfolio context of ticker decisions.

Revision ID: 20260825_0055
Revises: 20260824_0054
"""

from __future__ import annotations

from alembic import op


revision = "20260825_0055"
down_revision = "20260824_0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE analysis.ticker_decision
            ADD COLUMN IF NOT EXISTS market_state_publication_id UUID
                REFERENCES app.publication(id) ON DELETE RESTRICT,
            ADD COLUMN IF NOT EXISTS market_state_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS portfolio_impacts JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS risk_policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb;
        CREATE INDEX IF NOT EXISTS ix_ticker_decision_market_publication
            ON analysis.ticker_decision (market_state_publication_id)
            WHERE market_state_publication_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS analysis.ix_ticker_decision_market_publication;
        ALTER TABLE analysis.ticker_decision
            DROP COLUMN IF EXISTS risk_policy_snapshot,
            DROP COLUMN IF EXISTS portfolio_impacts,
            DROP COLUMN IF EXISTS market_state_snapshot,
            DROP COLUMN IF EXISTS market_state_publication_id;
        """
    )
