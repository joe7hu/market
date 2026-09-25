"""Allow the option agent to materialize its validated thesis expression."""

from alembic import op


revision = "20260924_0039"
down_revision = "20260924_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT INSERT, UPDATE ON TABLE app.thesis_expression TO market_app")
    op.execute("GRANT SELECT, USAGE ON SEQUENCE app.thesis_expression_id_seq TO market_app")


def downgrade() -> None:
    op.execute("REVOKE INSERT, UPDATE ON TABLE app.thesis_expression FROM market_app")
    op.execute("REVOKE SELECT, USAGE ON SEQUENCE app.thesis_expression_id_seq FROM market_app")
