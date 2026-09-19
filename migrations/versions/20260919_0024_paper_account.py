"""Record explicit simulated opening capital separately from broker cash."""
from alembic import op

revision = "20260919_0024"
down_revision = "20260918_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE app.paper_account (
            book text PRIMARY KEY CHECK (book = 'paper'),
            opening_cash numeric(24,2) NOT NULL CHECK (opening_cash > 0 AND opening_cash < 1000000000000),
            currency text NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
            opened_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            funding_note text NOT NULL CHECK (length(trim(funding_note)) > 0)
        );
        GRANT SELECT, INSERT ON app.paper_account TO market_app;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE app.paper_account")
