"""Allow the application role to read option-history health evidence."""

from __future__ import annotations

from alembic import op


revision = "20260905_0087"
down_revision = "20260905_0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "GRANT SELECT ON TABLE raw.option_capture_generation, analysis.shadow_trade TO market_app;"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT ON TABLE raw.option_capture_generation, analysis.shadow_trade FROM market_app;"
    )
