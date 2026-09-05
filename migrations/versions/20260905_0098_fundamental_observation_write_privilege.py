"""Allow the scheduled SEC collector to upsert normalized fundamental facts."""

from __future__ import annotations

from alembic import op


revision = "20260905_0098"
down_revision = "20260905_0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT INSERT, UPDATE ON TABLE raw.fundamental_observation TO market_app;")


def downgrade() -> None:
    op.execute("REVOKE INSERT, UPDATE ON TABLE raw.fundamental_observation FROM market_app;")
