"""Make options-recovery measurements executable and promotion-safe.

Revision ID: 20260803_0020
Revises: 20260801_0019
"""

from __future__ import annotations

from alembic import op


revision = "20260803_0020"
down_revision = "20260801_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every pre-recovery row used a midpoint/peak-oriented model.  Preserve it
    # for audit, but make it impossible to accidentally feed a promotion cohort.
    op.execute(
        """
        ALTER TABLE analysis.option_outcome
          ADD COLUMN IF NOT EXISTS objective_version TEXT NOT NULL DEFAULT 'legacy',
          ADD COLUMN IF NOT EXISTS outcome_classification TEXT NOT NULL DEFAULT 'legacy_non_executable',
          ADD COLUMN IF NOT EXISTS promotion_eligible BOOLEAN NOT NULL DEFAULT false,
          ADD COLUMN IF NOT EXISTS entry_fill_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS entry_fill_price NUMERIC(20, 6),
          ADD COLUMN IF NOT EXISTS exit_fill_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS exit_fill_price NUMERIC(20, 6),
          ADD COLUMN IF NOT EXISTS exit_reason TEXT,
          ADD COLUMN IF NOT EXISTS fee_total NUMERIC(20, 6),
          ADD COLUMN IF NOT EXISTS slippage_total NUMERIC(20, 6),
          ADD COLUMN IF NOT EXISTS return_3d DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS return_10d DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS time_to_3x_days INTEGER,
          ADD COLUMN IF NOT EXISTS time_to_4x_days INTEGER,
          ADD COLUMN IF NOT EXISTS executable_peak_return DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS mae DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS giveback DOUBLE PRECISION,
          ADD COLUMN IF NOT EXISTS exit_efficiency DOUBLE PRECISION;

        ALTER TABLE analysis.option_outcome
          DROP CONSTRAINT IF EXISTS ck_option_outcome_recovery_classification;
        ALTER TABLE analysis.option_outcome
          ADD CONSTRAINT ck_option_outcome_recovery_classification
          CHECK (outcome_classification IN (
            'legacy_non_executable', 'captured', 'missed', 'unfilled', 'unmeasurable', 'observing'
          ));

        UPDATE analysis.option_outcome
        SET objective_version = 'legacy',
            outcome_classification = 'legacy_non_executable',
            promotion_eligible = false
        WHERE objective_version = 'legacy'
           OR outcome_classification = 'legacy_non_executable';

        CREATE INDEX IF NOT EXISTS ix_option_outcome_objective_classification
        ON analysis.option_outcome (objective_version, outcome_classification, promotion_eligible);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS analysis.ix_option_outcome_objective_classification")
    op.execute("ALTER TABLE analysis.option_outcome DROP CONSTRAINT IF EXISTS ck_option_outcome_recovery_classification")
    for column in (
        "exit_efficiency", "giveback", "mae", "executable_peak_return",
        "time_to_4x_days", "time_to_3x_days", "return_10d", "return_3d",
        "slippage_total", "fee_total", "exit_reason", "exit_fill_price",
        "exit_fill_at", "entry_fill_price", "entry_fill_at", "promotion_eligible",
        "outcome_classification", "objective_version",
    ):
        op.execute(f"ALTER TABLE analysis.option_outcome DROP COLUMN IF EXISTS {column}")
