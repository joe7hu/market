"""Allow the application role to deduplicate immutable ingest payloads."""

from __future__ import annotations

from alembic import op


revision = "20260905_0095"
down_revision = "20260905_0094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON TABLE ingest.payload TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON TABLE ingest.payload FROM market_app;")
