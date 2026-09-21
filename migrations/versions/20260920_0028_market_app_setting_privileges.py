"""Allow the runtime role to persist allowlisted application settings."""

from alembic import op


revision = "20260920_0028"
down_revision = "20260920_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT,INSERT,UPDATE ON TABLE app.setting TO market_app")


def downgrade() -> None:
    op.execute("REVOKE INSERT,UPDATE ON TABLE app.setting FROM market_app")
