"""Keep canonical catalysts versioned and source-authoritative.

Revision ID: 20260813_0036
Revises: 20260813_0035
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260813_0036"
down_revision = "20260813_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("catalyst", sa.Column("event_key", sa.Text(), nullable=True), schema="app")
    op.add_column("catalyst", sa.Column("version", sa.Integer(), nullable=False, server_default="1"), schema="app")
    op.add_column("catalyst", sa.Column("status", sa.Text(), nullable=False, server_default="current"), schema="app")
    op.add_column("catalyst", sa.Column("supersedes_id", sa.BigInteger(), nullable=True), schema="app")
    op.add_column("catalyst", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True), schema="app")
    op.add_column("catalyst", sa.Column("source_id", sa.Text(), nullable=True), schema="app")
    op.add_column("catalyst", sa.Column("source_priority", sa.Integer(), nullable=False, server_default="0"), schema="app")
    op.add_column("catalyst", sa.Column("confidence", sa.Float(), nullable=True), schema="app")
    op.execute(
        """
        UPDATE app.catalyst
        SET event_key = coalesce('market-event:' || market_event_id::text, 'legacy:' || id::text),
            status = 'current',
            version = 1
        WHERE event_key IS NULL
        """
    )
    op.create_foreign_key(
        "fk_app_catalyst_supersedes", "catalyst", "catalyst",
        ["supersedes_id"], ["id"], source_schema="app", referent_schema="app",
    )
    op.drop_constraint("uq_app_catalyst_market_event", "catalyst", schema="app", type_="unique")
    op.create_index(
        "uq_app_catalyst_current_event_key", "catalyst", ["event_key"], unique=True,
        schema="app", postgresql_where=sa.text("status = 'current'"),
    )
    op.create_index(
        "ix_app_catalyst_current_starts_at", "catalyst", ["starts_at"],
        schema="app", postgresql_where=sa.text("status = 'current'"),
    )


def downgrade() -> None:
    op.drop_index("ix_app_catalyst_current_starts_at", table_name="catalyst", schema="app")
    op.drop_index("uq_app_catalyst_current_event_key", table_name="catalyst", schema="app")
    # Preserve the canonical current row and detach older projections so the
    # original one-row-per-market-event constraint can be restored.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (
                PARTITION BY market_event_id
                ORDER BY (status = 'current') DESC, version DESC, id DESC
            ) AS row_number
            FROM app.catalyst
            WHERE market_event_id IS NOT NULL
        )
        UPDATE app.catalyst catalyst
        SET market_event_id = NULL
        FROM ranked
        WHERE catalyst.id = ranked.id AND ranked.row_number > 1
        """
    )
    op.create_unique_constraint("uq_app_catalyst_market_event", "catalyst", ["market_event_id"], schema="app")
    op.drop_constraint("fk_app_catalyst_supersedes", "catalyst", schema="app", type_="foreignkey")
    op.drop_column("catalyst", "confidence", schema="app")
    op.drop_column("catalyst", "source_priority", schema="app")
    op.drop_column("catalyst", "source_id", schema="app")
    op.drop_column("catalyst", "superseded_at", schema="app")
    op.drop_column("catalyst", "supersedes_id", schema="app")
    op.drop_column("catalyst", "status", schema="app")
    op.drop_column("catalyst", "version", schema="app")
    op.drop_column("catalyst", "event_key", schema="app")
