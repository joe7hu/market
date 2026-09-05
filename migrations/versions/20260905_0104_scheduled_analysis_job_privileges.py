"""Allow scheduled analysis jobs to persist their PostgreSQL read models."""

from __future__ import annotations

from alembic import op


revision = "20260905_0104"
down_revision = "20260905_0103"
branch_labels = None
depends_on = None


_TABLES = (
    "analysis.agent_run",
    "analysis.ticker_outcome",
    "app.catalyst",
    "app.publication",
    "app.publication_bundle",
    "app.publication_bundle_item",
    "app.publication_content_item",
    "app.publication_item",
    "app.publication_payload",
)


def upgrade() -> None:
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {', '.join(_TABLES)} TO market_app;")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT, INSERT, UPDATE ON TABLE {', '.join(_TABLES)} FROM market_app;")
