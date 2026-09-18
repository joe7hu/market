"""Index bounded publication evidence and latest fundamental lookups."""

from alembic import op

revision = "20260918_0023"
down_revision = "20260912_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE INDEX ix_publication_payload_decision
            ON app.publication_payload ((payload->>'decision_id'));
        CREATE INDEX ix_fundamental_observation_latest
            ON raw.fundamental_observation (instrument_id, metric_set, observed_at DESC, id DESC);
        ANALYZE app.publication_payload;
        ANALYZE raw.fundamental_observation;
    """)


def downgrade() -> None:
    op.execute("DROP INDEX raw.ix_fundamental_observation_latest")
    op.execute("DROP INDEX app.ix_publication_payload_decision")
