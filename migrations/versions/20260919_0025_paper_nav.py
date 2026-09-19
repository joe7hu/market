"""Persist prospective verified paper NAV, including explicit evidence gaps."""
from alembic import op

revision = "20260919_0025"
down_revision = "20260919_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE app.paper_nav_observation (
            book text NOT NULL REFERENCES app.paper_account(book),
            bucket_at timestamptz NOT NULL,
            observed_at timestamptz NOT NULL,
            status text NOT NULL CHECK (status IN ('complete', 'incomplete')),
            nav numeric(24,2), cash_balance numeric(24,2),
            reserved_capital numeric(24,2), net_pnl numeric(24,2),
            blockers jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(blockers) = 'array'),
            calculation_version text NOT NULL,
            PRIMARY KEY (book, bucket_at),
            CHECK (observed_at >= bucket_at AND observed_at < bucket_at + interval '5 minutes'),
            CHECK ((status = 'complete' AND nav IS NOT NULL AND cash_balance IS NOT NULL
                    AND reserved_capital IS NOT NULL AND net_pnl IS NOT NULL
                    AND jsonb_array_length(blockers) = 0)
                OR (status = 'incomplete' AND nav IS NULL AND cash_balance IS NULL
                    AND reserved_capital IS NULL AND net_pnl IS NULL
                    AND jsonb_array_length(blockers) > 0)),
            CHECK (nav IS NULL OR (nav > '-1000000000000' AND nav < '1000000000000')),
            CHECK (cash_balance IS NULL OR (cash_balance > '-1000000000000' AND cash_balance < '1000000000000')),
            CHECK (reserved_capital IS NULL OR (reserved_capital >= 0 AND reserved_capital < '1000000000000')),
            CHECK (net_pnl IS NULL OR (net_pnl > '-1000000000000' AND net_pnl < '1000000000000'))
        );
        REVOKE ALL ON app.paper_nav_observation FROM PUBLIC;
        REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON app.paper_nav_observation FROM market_app;
        GRANT SELECT, INSERT ON app.paper_nav_observation TO market_app;
        CREATE INDEX ix_paper_order_management_due ON app.paper_order
            ((execution_quote #>> '{management,last_claimed_at}'), created_at, id)
            WHERE event_id IS NULL AND status NOT IN
                ('exited', 'invalidated', 'unfilled', 'rejected', 'unmeasurable');
    """)


def downgrade() -> None:
    op.execute("DROP INDEX app.ix_paper_order_management_due")
    op.execute("DROP TABLE app.paper_nav_observation")
