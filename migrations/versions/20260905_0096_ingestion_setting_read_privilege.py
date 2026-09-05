"""Allow ingestion jobs to read the existing price-confirmation cutover setting."""

from __future__ import annotations

from alembic import op


revision = "20260905_0096"
down_revision = "20260905_0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON TABLE app.setting TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON TABLE app.setting FROM market_app;")
