"""Add r3 option-history provenance and safety constraints.

Revision ID: 20260722_0014
Revises: 20260721_0013
"""

from __future__ import annotations

from alembic import op


revision = "20260722_0014"
down_revision = "20260721_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE analysis.option_outcome ADD COLUMN IF NOT EXISTS outcome_source TEXT NOT NULL DEFAULT 'generic'")
    op.execute("ALTER TABLE analysis.option_outcome ADD COLUMN IF NOT EXISTS shadow_trade_id UUID")
    op.execute(
        """
        ALTER TABLE analysis.option_outcome
        ADD CONSTRAINT fk_option_outcome_shadow_trade
        FOREIGN KEY (shadow_trade_id) REFERENCES analysis.shadow_trade(id) ON DELETE RESTRICT
        """
    )
    op.execute(
        """
        ALTER TABLE analysis.option_outcome
        ADD CONSTRAINT ck_option_outcome_source
        CHECK (outcome_source IN ('generic', 'options_history_v3'))
        """
    )
    op.execute(
        """
        ALTER TABLE analysis.option_outcome
        ADD CONSTRAINT ck_option_outcome_v3_shadow
        CHECK (outcome_source <> 'options_history_v3' OR shadow_trade_id IS NOT NULL)
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_option_outcome_source_shadow ON analysis.option_outcome (outcome_source, shadow_trade_id)")
    op.execute(
        """
        INSERT INTO analysis.option_history_canary (model_revision)
        SELECT 'history-v3-price-shape-r3'
        WHERE NOT EXISTS (
            SELECT 1 FROM analysis.option_history_canary
            WHERE model_revision = 'history-v3-price-shape-r3'
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS analysis.ix_option_outcome_source_shadow")
    op.execute("ALTER TABLE analysis.option_outcome DROP CONSTRAINT IF EXISTS ck_option_outcome_v3_shadow")
    op.execute("ALTER TABLE analysis.option_outcome DROP CONSTRAINT IF EXISTS ck_option_outcome_source")
    op.execute("ALTER TABLE analysis.option_outcome DROP CONSTRAINT IF EXISTS fk_option_outcome_shadow_trade")
    op.execute("ALTER TABLE analysis.option_outcome DROP COLUMN IF EXISTS shadow_trade_id")
    op.execute("ALTER TABLE analysis.option_outcome DROP COLUMN IF EXISTS outcome_source")
