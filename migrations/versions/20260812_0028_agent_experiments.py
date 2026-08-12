"""Add advisory-only paired provider experiment telemetry.

Revision ID: 20260812_0028
Revises: 20260812_0027
"""

from __future__ import annotations

from alembic import op


revision = "20260812_0028"
down_revision = "20260812_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE analysis.agent_experiment (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            experiment_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'completed', 'archived')),
            champion_provider TEXT NOT NULL,
            champion_model TEXT NOT NULL,
            challenger_provider TEXT NOT NULL,
            challenger_model TEXT NOT NULL,
            max_pairs_per_trading_day INTEGER NOT NULL DEFAULT 12
                CHECK (max_pairs_per_trading_day BETWEEN 1 AND 12),
            advisory_only BOOLEAN NOT NULL DEFAULT true,
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
            immutable_report JSONB,
            report_sealed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK ((status <> 'completed') OR immutable_report IS NOT NULL)
        );

        ALTER TABLE analysis.agent_run
          ADD COLUMN IF NOT EXISTS experiment_id UUID REFERENCES analysis.agent_experiment(id) ON DELETE RESTRICT,
          ADD COLUMN IF NOT EXISTS arm TEXT,
          ADD COLUMN IF NOT EXISTS evidence_fingerprint TEXT,
          ADD COLUMN IF NOT EXISTS prompt_version TEXT,
          ADD COLUMN IF NOT EXISTS schema_version TEXT,
          ADD COLUMN IF NOT EXISTS baseline_version TEXT,
          ADD COLUMN IF NOT EXISTS validation_status TEXT,
          ADD COLUMN IF NOT EXISTS validation_detail JSONB NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN IF NOT EXISTS latency_ms INTEGER;

        ALTER TABLE analysis.agent_task
          ADD COLUMN IF NOT EXISTS experiment_id UUID REFERENCES analysis.agent_experiment(id) ON DELETE RESTRICT,
          ADD COLUMN IF NOT EXISTS arm TEXT,
          ADD COLUMN IF NOT EXISTS paired_task_id UUID REFERENCES analysis.agent_task(id) ON DELETE RESTRICT,
          ADD COLUMN IF NOT EXISTS provider TEXT,
          ADD COLUMN IF NOT EXISTS model TEXT,
          ADD COLUMN IF NOT EXISTS evidence_fingerprint TEXT,
          ADD COLUMN IF NOT EXISTS prompt_version TEXT,
          ADD COLUMN IF NOT EXISTS schema_version TEXT,
          ADD COLUMN IF NOT EXISTS baseline_version TEXT,
          ADD COLUMN IF NOT EXISTS validation_status TEXT,
          ADD COLUMN IF NOT EXISTS validation_detail JSONB NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN IF NOT EXISTS latency_ms INTEGER,
          ADD COLUMN IF NOT EXISTS input_tokens BIGINT,
          ADD COLUMN IF NOT EXISTS output_tokens BIGINT,
          ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(14, 6);

        ALTER TABLE analysis.option_event_agent_batch
          ADD COLUMN IF NOT EXISTS experiment_id UUID REFERENCES analysis.agent_experiment(id) ON DELETE RESTRICT,
          ADD COLUMN IF NOT EXISTS arm TEXT,
          ADD COLUMN IF NOT EXISTS paired_task_id UUID REFERENCES analysis.agent_task(id) ON DELETE RESTRICT,
          ADD COLUMN IF NOT EXISTS evidence_fingerprint TEXT,
          ADD COLUMN IF NOT EXISTS prompt_version TEXT,
          ADD COLUMN IF NOT EXISTS schema_version TEXT,
          ADD COLUMN IF NOT EXISTS baseline_version TEXT,
          ADD COLUMN IF NOT EXISTS validation_status TEXT,
          ADD COLUMN IF NOT EXISTS validation_detail JSONB NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN IF NOT EXISTS latency_ms INTEGER;

        CREATE INDEX IF NOT EXISTS ix_agent_task_experiment_arm_created
          ON analysis.agent_task (experiment_id, arm, created_at DESC)
          WHERE experiment_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_agent_run_experiment_arm_started
          ON analysis.agent_run (experiment_id, arm, started_at DESC)
          WHERE experiment_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_option_event_agent_batch_experiment
          ON analysis.option_event_agent_batch (experiment_id, arm, created_at DESC)
          WHERE experiment_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS analysis.ix_option_event_agent_batch_experiment")
    op.execute("DROP INDEX IF EXISTS analysis.ix_agent_run_experiment_arm_started")
    op.execute("DROP INDEX IF EXISTS analysis.ix_agent_task_experiment_arm_created")
    for column in (
        "latency_ms", "validation_detail", "validation_status", "baseline_version",
        "schema_version", "prompt_version", "evidence_fingerprint", "paired_task_id",
        "arm", "experiment_id",
    ):
        op.execute(f"ALTER TABLE analysis.option_event_agent_batch DROP COLUMN IF EXISTS {column}")
    for column in (
        "cost_usd", "output_tokens", "input_tokens", "latency_ms", "validation_detail",
        "validation_status", "baseline_version", "schema_version", "prompt_version",
        "evidence_fingerprint", "model", "provider", "paired_task_id", "arm", "experiment_id",
    ):
        op.execute(f"ALTER TABLE analysis.agent_task DROP COLUMN IF EXISTS {column}")
    for column in (
        "latency_ms", "validation_detail", "validation_status", "baseline_version",
        "schema_version", "prompt_version", "evidence_fingerprint", "arm", "experiment_id",
    ):
        op.execute(f"ALTER TABLE analysis.agent_run DROP COLUMN IF EXISTS {column}")
    op.execute("DROP TABLE IF EXISTS analysis.agent_experiment")
