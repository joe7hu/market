"""Allow the application role to inspect published evidence content."""

from __future__ import annotations

from alembic import op


revision = "20260905_0085"
down_revision = "20260905_0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON TABLE app.publication_content_item TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON TABLE app.publication_content_item FROM market_app;")
