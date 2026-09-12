"""Add explicit paper-book scope metadata for workbench filtering."""

from alembic import op


revision = "20260912_0021"
down_revision = "20260912_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.paper_order
            ADD COLUMN book text NOT NULL DEFAULT 'paper',
            ADD COLUMN sleeve text;
        ALTER TABLE app.paper_order
            ADD CONSTRAINT ck_paper_order_book CHECK (book = 'paper');
        CREATE INDEX ix_paper_workbench_scope
            ON app.paper_order (book, sleeve, lane, structure, created_at DESC, id DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX app.ix_paper_workbench_scope")
    op.execute("ALTER TABLE app.paper_order DROP CONSTRAINT ck_paper_order_book")
    op.execute("ALTER TABLE app.paper_order DROP COLUMN sleeve, DROP COLUMN book")
