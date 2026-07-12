"""Enforce one active revision per strategy authority group.

Revision ID: 20260711_0004
Revises: 20260711_0003
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260711_0004"
down_revision = "20260711_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategy_revision",
        sa.Column("authority_group", sa.Text(), nullable=True),
        schema="analysis",
    )
    op.execute(
        "UPDATE analysis.strategy_revision SET authority_group = strategy_key"
    )
    op.execute(
        """
        WITH RECURSIVE lineage AS (
            SELECT id FROM analysis.strategy_revision
            WHERE strategy_key = 'options-radar-core'
            UNION
            SELECT child.id FROM analysis.strategy_revision child
            JOIN lineage parent ON child.supersedes_id = parent.id
        )
        UPDATE analysis.strategy_revision revision
        SET authority_group = 'options-radar-core'
        FROM lineage
        WHERE revision.id = lineage.id
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (
                PARTITION BY authority_group
                ORDER BY promoted_at DESC NULLS LAST, revision DESC, id DESC
            ) AS authority_rank
            FROM analysis.strategy_revision
            WHERE status = 'active'
        )
        UPDATE analysis.strategy_revision revision
        SET status = 'superseded'
        FROM ranked
        WHERE revision.id = ranked.id AND ranked.authority_rank > 1
        """
    )
    op.alter_column(
        "strategy_revision", "authority_group", nullable=False, schema="analysis"
    )
    op.create_index(
        "uq_analysis_strategy_active_authority",
        "strategy_revision",
        ["authority_group"],
        unique=True,
        schema="analysis",
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_analysis_strategy_active_authority",
        table_name="strategy_revision",
        schema="analysis",
    )
    op.drop_column("strategy_revision", "authority_group", schema="analysis")
