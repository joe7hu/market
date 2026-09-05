"""Allow the ticker publisher to persist decision revisions and evidence."""

from __future__ import annotations

from alembic import op


revision = "20260905_0120"
down_revision = "20260905_0119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE ON analysis.ticker_decision,
            analysis.ticker_data_request, analysis.ticker_input_manifest TO market_app;
        GRANT USAGE, SELECT ON SEQUENCE analysis.ticker_input_manifest_id_seq TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE USAGE, SELECT ON SEQUENCE analysis.ticker_input_manifest_id_seq FROM market_app;
        REVOKE SELECT, INSERT, UPDATE ON analysis.ticker_decision,
            analysis.ticker_data_request, analysis.ticker_input_manifest FROM market_app;
        """
    )
