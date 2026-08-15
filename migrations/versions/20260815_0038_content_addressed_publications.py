"""Keep publication evidence immutable without repeating heavy UI payloads.

Revision ID: 20260815_0038
Revises: 20260815_0037

No historical publication rows are moved here.  The storage job owns that
resumable, verified backfill and later physical table reclamation.
"""

from __future__ import annotations

from alembic import op


revision = "20260815_0038"
down_revision = "20260815_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.publication_payload (
            content_hash CHAR(64) PRIMARY KEY,
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE app.publication_bundle (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            scope TEXT NOT NULL,
            bundle_hash CHAR(64) NOT NULL,
            item_count INTEGER NOT NULL CHECK (item_count >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (scope, bundle_hash)
        );
        CREATE TABLE app.publication_bundle_item (
            bundle_id UUID NOT NULL REFERENCES app.publication_bundle(id) ON DELETE CASCADE,
            model_name TEXT NOT NULL,
            stable_key TEXT NOT NULL,
            rank INTEGER NOT NULL,
            instrument_id BIGINT REFERENCES catalog.instrument(id),
            content_hash CHAR(64) NOT NULL REFERENCES app.publication_payload(content_hash) ON DELETE RESTRICT,
            PRIMARY KEY (bundle_id, model_name, stable_key)
        );
        CREATE INDEX ix_app_publication_bundle_item_rank
            ON app.publication_bundle_item (bundle_id, model_name, rank);

        ALTER TABLE app.publication
            ADD COLUMN bundle_id UUID REFERENCES app.publication_bundle(id) ON DELETE RESTRICT;
        CREATE INDEX ix_app_publication_bundle ON app.publication (bundle_id) WHERE bundle_id IS NOT NULL;

        CREATE TABLE app.current_publication_item (
            scope TEXT NOT NULL,
            publication_id UUID NOT NULL REFERENCES app.publication(id) ON DELETE CASCADE,
            model_name TEXT NOT NULL,
            stable_key TEXT NOT NULL,
            rank INTEGER NOT NULL,
            instrument_id BIGINT REFERENCES catalog.instrument(id),
            content_hash CHAR(64) NOT NULL REFERENCES app.publication_payload(content_hash) ON DELETE RESTRICT,
            PRIMARY KEY (scope, model_name, stable_key)
        );
        CREATE INDEX ix_app_current_publication_item_rank
            ON app.current_publication_item (scope, model_name, rank);

        -- Read-only compatibility surface.  New publications use their
        -- bundle; old publications stay visible until the separate verified
        -- archival job has compacted them.
        CREATE VIEW app.publication_content_item AS
        SELECT item.publication_id, item.model_name, item.stable_key, item.rank,
               item.instrument_id, item.payload
        FROM app.publication_item item
        JOIN app.publication publication ON publication.id = item.publication_id
        WHERE publication.bundle_id IS NULL
        UNION ALL
        SELECT publication.id AS publication_id, item.model_name, item.stable_key,
               item.rank, item.instrument_id, payload.payload
        FROM app.publication publication
        JOIN app.publication_bundle_item item ON item.bundle_id = publication.bundle_id
        JOIN app.publication_payload payload ON payload.content_hash = item.content_hash;
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS app.publication_content_item")
    op.execute("DROP TABLE IF EXISTS app.current_publication_item")
    op.execute("DROP INDEX IF EXISTS app.ix_app_publication_bundle")
    op.execute("ALTER TABLE app.publication DROP COLUMN IF EXISTS bundle_id")
    op.execute("DROP TABLE IF EXISTS app.publication_bundle_item")
    op.execute("DROP TABLE IF EXISTS app.publication_bundle")
    op.execute("DROP TABLE IF EXISTS app.publication_payload")
