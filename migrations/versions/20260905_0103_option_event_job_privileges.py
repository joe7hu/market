"""Allow scheduled option event jobs to use their PostgreSQL owners."""

from __future__ import annotations

from alembic import op


revision = "20260905_0103"
down_revision = "20260905_0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE ON TABLE
          analysis.option_event_agent_batch,
          analysis.option_event_capture,
          analysis.option_event_contract,
          analysis.option_event_signal,
          analysis.option_event_spot
        TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE SELECT, INSERT, UPDATE ON TABLE
          analysis.option_event_agent_batch,
          analysis.option_event_capture,
          analysis.option_event_contract,
          analysis.option_event_signal,
          analysis.option_event_spot
        FROM market_app;
        """
    )
