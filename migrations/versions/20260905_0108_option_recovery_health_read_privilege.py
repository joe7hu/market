"""Allow the application role to read option-recovery canary sessions."""

from __future__ import annotations

from alembic import op


revision = "20260905_0108"
down_revision = "20260905_0107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON TABLE analysis.option_recovery_program_session TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON TABLE analysis.option_recovery_program_session FROM market_app;")
