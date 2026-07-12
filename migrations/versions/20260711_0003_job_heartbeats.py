"""Protect long-running jobs with database heartbeats.

Revision ID: 20260711_0003
Revises: 20260711_0002
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260711_0003"
down_revision = "20260711_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_run",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        schema="ops",
    )
    op.execute("UPDATE ops.job_run SET heartbeat_at = COALESCE(heartbeat_at, started_at)")
    op.alter_column("job_run", "heartbeat_at", nullable=False, schema="ops")


def downgrade() -> None:
    op.drop_column("job_run", "heartbeat_at", schema="ops")
