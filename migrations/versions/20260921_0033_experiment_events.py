"""Append-only prospective experiment marks, separate from funded paper NAV."""
from alembic import op

revision = "20260921_0033"
down_revision = "20260921_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE analysis.option_experiment_event (
            shadow_trade_id uuid NOT NULL REFERENCES analysis.shadow_trade(id),
            event_key text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('entry', 'mark', 'exit', 'mark_gap')),
            observed_at timestamptz NOT NULL,
            quote_observed_at timestamptz,
            price numeric(24,8),
            net_pnl numeric(24,8),
            net_return numeric(24,12),
            fees numeric(24,8),
            reason text,
            evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
            PRIMARY KEY (shadow_trade_id, event_key),
            CHECK (quote_observed_at IS NULL OR quote_observed_at <= observed_at),
            CHECK ((kind = 'mark_gap' AND price IS NULL AND net_pnl IS NULL AND net_return IS NULL AND reason IS NOT NULL)
                OR (kind <> 'mark_gap' AND price IS NOT NULL AND net_pnl IS NOT NULL
                    AND net_return IS NOT NULL AND fees IS NOT NULL AND quote_observed_at IS NOT NULL)),
            CHECK (price IS NULL OR (price >= 0 AND price < 100000000 AND (kind <> 'entry' OR price > 0))),
            CHECK (net_pnl IS NULL OR (net_pnl > -1000000000000 AND net_pnl < 1000000000000)),
            CHECK (net_return IS NULL OR (net_return > -1000000 AND net_return < 1000000)),
            CHECK (fees IS NULL OR (fees >= 0 AND fees < 100000000))
        );
        CREATE INDEX ix_option_experiment_event_clock ON analysis.option_experiment_event
            (shadow_trade_id, observed_at, event_key);
        CREATE INDEX ix_option_experiment_status ON analysis.shadow_trade
            (status, created_at DESC, id)
            WHERE source_kind = 'options_paper_experiment';
        REVOKE ALL ON analysis.option_experiment_event FROM PUBLIC;
        REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON analysis.option_experiment_event FROM market_app;
        GRANT SELECT, INSERT ON analysis.option_experiment_event TO market_app;
    """)
    # Deliberately no historical backfill: old snapshots are not a mark series.


def downgrade() -> None:
    op.execute("DROP INDEX analysis.ix_option_experiment_status")
    op.execute("DROP TABLE analysis.option_experiment_event")
