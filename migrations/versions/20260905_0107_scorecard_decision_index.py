"""Index the bounded, versioned option scorecard decision cohort."""

from __future__ import annotations

from alembic import op


revision = "20260905_0107"
down_revision = "20260905_0106"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX ix_analysis_decision_scorecard_episode
          ON analysis.decision (lane, episode_key, as_of DESC, id DESC)
          INCLUDE (run_id, state, sample_eligible, quarantine_reason, calibration_cohort)
          WHERE kind = 'option'
            AND calibration_cohort LIKE 'option-scorecard-truth-v1:%';
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS analysis.ix_analysis_decision_scorecard_episode;")
