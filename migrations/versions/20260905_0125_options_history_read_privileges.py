"""Allow the application role to read option history diagnostics."""

from __future__ import annotations

from alembic import op


revision = "20260905_0125"
down_revision = "20260905_0124"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT SELECT ON analysis.option_history_anomaly,
                       analysis.option_surface_shift TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE SELECT ON analysis.option_history_anomaly,
                       analysis.option_surface_shift FROM market_app;
        """
    )
