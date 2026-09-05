"""Allow scheduled source jobs to use their PostgreSQL owners."""

from __future__ import annotations

from alembic import op


revision = "20260905_0102"
down_revision = "20260905_0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE ON TABLE
          raw.content_item, raw.content_item_instrument,
          raw.market_event, raw.market_event_version,
          analysis.option_event, analysis.option_recovery_cohort,
          analysis.option_event_detector_run, analysis.symbol_decision_outcome,
          app.thesis_automation_run
        TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE SELECT, INSERT, UPDATE ON TABLE
          raw.content_item, raw.content_item_instrument,
          raw.market_event, raw.market_event_version,
          analysis.option_event, analysis.option_recovery_cohort,
          analysis.option_event_detector_run, analysis.symbol_decision_outcome,
          app.thesis_automation_run
        FROM market_app;
        """
    )
