"""Version archive references against their source ingestion run.

Revision ID: 20260815_0040
Revises: 20260815_0039

No backfill runs in Alembic.  A resumable archive audit records the current
source run before a reference is eligible to skip future verification.
"""

from __future__ import annotations

from alembic import op


revision = "20260815_0040"
down_revision = "20260815_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE ops.storage_archive_manifest_reference "
        "ADD COLUMN source_ingest_run_id UUID"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE ops.storage_archive_manifest_reference "
        "DROP COLUMN IF EXISTS source_ingest_run_id"
    )
