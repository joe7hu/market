"""Track source-row lineage for content-addressed storage artifacts.

Revision ID: 20260815_0039
Revises: 20260815_0038

This is metadata only.  The archive service performs the small, resumable
reference repair for artifacts that were written before this relation existed.
"""

from __future__ import annotations

from alembic import op


revision = "20260815_0039"
down_revision = "20260815_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ops.storage_archive_manifest_reference (
            manifest_id BIGINT NOT NULL REFERENCES ops.storage_archive_manifest(id) ON DELETE RESTRICT,
            source_relation TEXT NOT NULL,
            source_row_id BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (manifest_id, source_relation, source_row_id),
            UNIQUE (source_relation, source_row_id)
        );
        CREATE INDEX ix_storage_archive_manifest_reference_source
            ON ops.storage_archive_manifest_reference (source_relation, source_row_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ops.storage_archive_manifest_reference")
