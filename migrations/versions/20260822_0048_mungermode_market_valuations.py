"""Make Munger Mode a Market-owned active refresh source."""

from __future__ import annotations

from alembic import op


revision = "20260822_0048"
down_revision = "20260821_0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep all existing observations and runs.  This only transfers ownership
    # of future freshness checks to the local PostgreSQL-backed job.
    op.execute(
        """
        UPDATE ingest.source
        SET operational_state = 'active',
            enabled = TRUE,
            health_owner = 'update_market_valuations',
            freshness_seconds = 86400,
            updated_at = now()
        WHERE id = 'mungermode-market-valuations'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE ingest.source
        SET health_owner = 'external:mungermode',
            freshness_seconds = 86400,
            updated_at = now()
        WHERE id = 'mungermode-market-valuations'
        """
    )
