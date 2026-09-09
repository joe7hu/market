"""Preserve historical publication cutoff reads after supersession tracking."""

from alembic import op


revision = "20260909_0008"
down_revision = "20260908_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy supersessions had no timestamp.  Migration time is the first
    # truthful boundary available, so earlier point-in-time reads retain them.
    op.execute(
        "UPDATE app.publication SET superseded_at = now() "
        "WHERE status = 'superseded' AND superseded_at IS NULL"
    )


def downgrade() -> None:
    # Keep the derived timestamp: removing it would discard publication history.
    pass
