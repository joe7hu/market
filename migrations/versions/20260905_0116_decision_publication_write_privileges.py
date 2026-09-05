"""Allow the existing application role to publish the options decision path."""

from __future__ import annotations

from alembic import op


revision = "20260905_0116"
down_revision = "20260905_0115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE, DELETE ON analysis.decision,
            analysis.option_decision, analysis.option_feature TO market_app;
        GRANT SELECT, INSERT, UPDATE ON analysis.decision_evidence,
            analysis.event_study_feature TO market_app;
        GRANT SELECT, INSERT ON analysis.reject_summary TO market_app;
        GRANT USAGE, SELECT ON SEQUENCE analysis.reject_summary_id_seq TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE USAGE, SELECT ON SEQUENCE analysis.reject_summary_id_seq FROM market_app;
        REVOKE SELECT, INSERT ON analysis.reject_summary FROM market_app;
        REVOKE SELECT, INSERT, UPDATE ON analysis.decision_evidence,
            analysis.event_study_feature FROM market_app;
        REVOKE SELECT, INSERT, UPDATE, DELETE ON analysis.decision,
            analysis.option_decision, analysis.option_feature FROM market_app;
        """
    )
