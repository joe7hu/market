"""Make normalized source-signal provenance queryable and point-in-time safe.

Revision ID: 20260823_0050
Revises: 20260822_0049
"""

from __future__ import annotations

from alembic import op


revision = "20260823_0050"
down_revision = "20260822_0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE analysis.source_signal
            ADD COLUMN IF NOT EXISTS event_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS revision TEXT,
            ADD COLUMN IF NOT EXISTS license TEXT,
            ADD COLUMN IF NOT EXISTS evidence_state TEXT,
            ADD COLUMN IF NOT EXISTS transformation TEXT
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_analysis_source_signal_pit
            ON analysis.source_signal (instrument_id, available_at DESC, observed_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS analysis.ix_analysis_source_signal_pit")
    op.execute(
        """
        ALTER TABLE analysis.source_signal
            DROP COLUMN IF EXISTS transformation,
            DROP COLUMN IF EXISTS evidence_state,
            DROP COLUMN IF EXISTS license,
            DROP COLUMN IF EXISTS revision,
            DROP COLUMN IF EXISTS received_at,
            DROP COLUMN IF EXISTS available_at,
            DROP COLUMN IF EXISTS published_at,
            DROP COLUMN IF EXISTS event_at
        """
    )
