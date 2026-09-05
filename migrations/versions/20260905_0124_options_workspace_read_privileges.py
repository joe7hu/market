"""Allow the application role to read the complete options workspace."""

from __future__ import annotations

from alembic import op


revision = "20260905_0124"
down_revision = "20260905_0123"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT SELECT ON analysis.option_surface_summary,
                       analysis.option_relative_value,
                       analysis.option_relative_value_verification TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE SELECT ON analysis.option_surface_summary,
                       analysis.option_relative_value,
                       analysis.option_relative_value_verification FROM market_app;
        """
    )
