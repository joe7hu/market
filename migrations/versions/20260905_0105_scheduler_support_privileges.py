"""Allow scheduler coordination and publication projection maintenance."""

from __future__ import annotations

from alembic import op


revision = "20260905_0105"
down_revision = "20260905_0104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE ops.provider_lease TO market_app;
        GRANT SELECT, INSERT, DELETE ON TABLE app.current_publication_item TO market_app;
        GRANT SELECT, INSERT, UPDATE ON TABLE analysis.option_opportunity_observation TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE ops.provider_lease FROM market_app;
        REVOKE SELECT, INSERT, DELETE ON TABLE app.current_publication_item FROM market_app;
        REVOKE SELECT, INSERT, UPDATE ON TABLE analysis.option_opportunity_observation FROM market_app;
        """
    )
