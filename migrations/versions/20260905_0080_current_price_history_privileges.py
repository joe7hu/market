"""Allow the current-price authority to read its historical quote inputs."""

from __future__ import annotations

from alembic import op


revision = "20260905_0080"
down_revision = "20260905_0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT SELECT ON TABLE raw.quote_history, raw.quote_confirmation TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE SELECT ON TABLE raw.quote_history, raw.quote_confirmation FROM market_app;
        """
    )
