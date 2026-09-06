"""Complete runtime sequence access and make the large lookup index safe to build."""

from alembic import op


revision = "20260906_0130"
down_revision = "20260906_0129"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT USAGE, SELECT ON SEQUENCE app.thesis_review_event_id_seq TO market_app")
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_option_relative_value_generation_contract "
            "ON analysis.option_relative_value (capture_generation_id, contract_id, id DESC)"
        )


def downgrade() -> None:
    op.execute("REVOKE USAGE, SELECT ON SEQUENCE app.thesis_review_event_id_seq FROM market_app")
