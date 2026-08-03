"""Add advisory-only event batches for options recovery.

Revision ID: 20260803_0024
Revises: 20260803_0023
"""

from __future__ import annotations

from alembic import op


revision = "20260803_0024"
down_revision = "20260803_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE analysis.option_event_agent_batch (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            event_id UUID NOT NULL REFERENCES analysis.option_event(id) ON DELETE CASCADE,
            capture_id UUID REFERENCES analysis.option_event_capture(id) ON DELETE SET NULL,
            trigger TEXT NOT NULL,
            fingerprint_key TEXT NOT NULL,
            fingerprint JSONB NOT NULL,
            provider TEXT NOT NULL DEFAULT 'codex',
            model TEXT NOT NULL,
            reasoning_effort TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            task_count INTEGER NOT NULL DEFAULT 0,
            agent_run_id UUID REFERENCES analysis.agent_run(id) ON DELETE SET NULL,
            telemetry JSONB NOT NULL DEFAULT '{}'::jsonb,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            CONSTRAINT ck_option_event_agent_batch_trigger CHECK (trigger IN (
                'event_established', 'underlying_move_2pct', 'material_iv_change',
                'new_material_evidence', 'signal_family_transition', 'preopen_review'
            )),
            CONSTRAINT ck_option_event_agent_batch_status CHECK (status IN (
                'queued', 'running', 'completed', 'failed', 'skipped'
            )),
            CONSTRAINT ck_option_event_agent_batch_task_count CHECK (task_count >= 0 AND task_count <= 12),
            UNIQUE (event_id, fingerprint_key)
        );
        CREATE INDEX ix_option_event_agent_batch_queue
        ON analysis.option_event_agent_batch (status, created_at, event_id);
        CREATE INDEX ix_option_event_agent_batch_event_day
        ON analysis.option_event_agent_batch (event_id, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS analysis.ix_option_event_agent_batch_event_day")
    op.execute("DROP INDEX IF EXISTS analysis.ix_option_event_agent_batch_queue")
    op.execute("DROP TABLE IF EXISTS analysis.option_event_agent_batch")
