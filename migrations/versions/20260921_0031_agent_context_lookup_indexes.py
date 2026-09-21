"""Index ticker payload lookup in content-addressed publications."""

from alembic import op


revision = "20260921_0031"
down_revision = "20260921_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE INDEX ix_app_publication_payload_symbol
            ON app.publication_payload ((COALESCE(payload ->> 'symbol', payload ->> 'ticker', payload ->> 'underlying')));
        CREATE INDEX ix_app_publication_bundle_item_content_hash
            ON app.publication_bundle_item (content_hash);
        ANALYZE app.publication_payload;
        ANALYZE app.publication_bundle_item;
    """)


def downgrade() -> None:
    op.execute("DROP INDEX app.ix_app_publication_bundle_item_content_hash")
    op.execute("DROP INDEX app.ix_app_publication_payload_symbol")
