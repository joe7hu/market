"""Keep explicit review usefulness separate from workflow state and returns."""

from alembic import op

revision = "20260907_0003"
down_revision = "20260907_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE app.decision_inbox_item
        ADD COLUMN useful boolean,
        ADD COLUMN usefulness_updated_at timestamptz,
        ADD CONSTRAINT decision_inbox_usefulness_timestamp CHECK (
            (useful IS NULL) = (usefulness_updated_at IS NULL)
        )""")
    op.execute("GRANT UPDATE (useful, usefulness_updated_at) ON app.decision_inbox_item TO market_app")


def downgrade() -> None:
    op.execute("""ALTER TABLE app.decision_inbox_item
        DROP CONSTRAINT decision_inbox_usefulness_timestamp,
        DROP COLUMN usefulness_updated_at,
        DROP COLUMN useful""")
