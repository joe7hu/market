"""Add compact current outcomes and idempotent catalyst projection.

Revision ID: 20260711_0002
Revises: 20260711_0001
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260711_0002"
down_revision = "20260711_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("option_outcome", sa.Column("current_return", sa.Double(), nullable=True), schema="analysis")
    op.execute(
        """
        DELETE FROM app.catalyst duplicate
        USING app.catalyst keeper
        WHERE duplicate.market_event_id = keeper.market_event_id
          AND duplicate.market_event_id IS NOT NULL
          AND duplicate.id > keeper.id
        """
    )
    op.create_unique_constraint(
        "uq_app_catalyst_market_event", "catalyst", ["market_event_id"], schema="app"
    )


def downgrade() -> None:
    op.drop_constraint("uq_app_catalyst_market_event", "catalyst", schema="app", type_="unique")
    op.drop_column("option_outcome", "current_return", schema="analysis")
