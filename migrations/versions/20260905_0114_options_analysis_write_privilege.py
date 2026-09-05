"""Allow the existing application role to write derived options analysis."""

from __future__ import annotations

from alembic import op


revision = "20260905_0114"
down_revision = "20260905_0113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT SELECT, INSERT ON analysis.option_feature, analysis.option_decision,
                              analysis.symbol_feature TO market_app;
        GRANT USAGE, SELECT ON SEQUENCE analysis.option_feature_id_seq,
                              analysis.symbol_feature_id_seq TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE USAGE, SELECT ON SEQUENCE analysis.option_feature_id_seq,
                              analysis.symbol_feature_id_seq FROM market_app;
        REVOKE INSERT ON analysis.option_feature, analysis.option_decision,
                         analysis.symbol_feature FROM market_app;
        """
    )
