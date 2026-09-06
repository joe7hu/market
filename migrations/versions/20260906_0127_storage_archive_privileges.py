"""Allow the application role to operate the bounded storage archive state."""

from __future__ import annotations

from alembic import op


revision = "20260906_0127"
down_revision = "20260905_0126"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE ON TABLE
            ops.storage_archive_manifest,
            ops.storage_archive_checkpoint TO market_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
            ops.storage_archive_manifest_reference TO market_app;
        GRANT USAGE, SELECT ON SEQUENCE ops.storage_archive_manifest_id_seq TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE USAGE, SELECT ON SEQUENCE ops.storage_archive_manifest_id_seq FROM market_app;
        REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE
            ops.storage_archive_manifest_reference FROM market_app;
        REVOKE SELECT, INSERT, UPDATE ON TABLE
            ops.storage_archive_manifest,
            ops.storage_archive_checkpoint FROM market_app;
        """
    )
