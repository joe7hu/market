"""Normalize immutable decision context; retire the duplicate manifest writer.

Metadata-only upgrade for existing large tables. No history is deleted and no
large table is rewritten here. The explicit storage CLI owns backfill/export
and the separately authorized legacy-table cutover.
"""

from alembic import op

revision = "20260923_0035"
down_revision = "20260922_0034"
branch_labels = None
depends_on = None

# Keep the queryable decision/plan columns inline. Only context shared by many
# ticker revisions is interned. Scalar subqueries are evaluated only when the
# corresponding view column is requested (unlike eager Python hydration).
_COLUMNS = """
    d.id, d.instrument_id, d.decision_revision, d.contract_version, d.as_of,
    d.published_at, d.input_hash, d.code_version, d.experiment_id,
    d.tactical, d.fundamental, d.capital_action, d.risk_policy, d.expressions,
    d.selected_expression, d.data_requests, d.learning_history, d.input_manifest,
    d.status, d.created_at, d.resolution, d.policy_version,
    d.opportunity_episode_id, d.opportunity_cutoff, d.opportunity_episode,
    d.market_state_publication_id,
    CASE WHEN d.market_state_context_hash IS NULL THEN d.market_state_snapshot
         ELSE (SELECT context.payload FROM analysis.decision_context context
               WHERE context.content_hash = d.market_state_context_hash)
    END AS market_state_snapshot,
    d.portfolio_impacts,
    CASE WHEN d.risk_policy_context_hash IS NULL THEN d.risk_policy_snapshot
         ELSE (SELECT context.payload FROM analysis.decision_context context
               WHERE context.content_hash = d.risk_policy_context_hash)
    END AS risk_policy_snapshot,
    d.market_state_context_hash, d.risk_policy_context_hash
"""


def upgrade() -> None:
    op.execute("""
        CREATE TABLE analysis.decision_context (
            content_hash text PRIMARY KEY CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            payload jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        GRANT SELECT, INSERT ON analysis.decision_context TO market_app;
        CREATE FUNCTION analysis.reject_decision_context_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'decision context is immutable';
        END $$;
        CREATE TRIGGER decision_context_immutable
          BEFORE UPDATE OR DELETE ON analysis.decision_context
          FOR EACH ROW EXECUTE FUNCTION analysis.reject_decision_context_mutation();
        ALTER TABLE analysis.ticker_decision
          ADD COLUMN market_state_context_hash text
            REFERENCES analysis.decision_context(content_hash) ON DELETE RESTRICT,
          ADD COLUMN risk_policy_context_hash text
            REFERENCES analysis.decision_context(content_hash) ON DELETE RESTRICT;
        ALTER TABLE analysis.ticker_input_manifest RENAME TO ticker_input_manifest_legacy;
        REVOKE INSERT, UPDATE ON analysis.ticker_input_manifest_legacy FROM market_app;
        REVOKE USAGE ON SEQUENCE analysis.ticker_input_manifest_id_seq FROM market_app;
    """)
    op.execute(f"CREATE VIEW analysis.ticker_decision_read AS SELECT {_COLUMNS} FROM analysis.ticker_decision d")
    op.execute("GRANT SELECT ON analysis.ticker_decision_read TO market_app")


def downgrade() -> None:
    # A downgrade must not silently discard normalized context or recreate a
    # dropped legacy table without its archived rows. Restore the old checkout
    # only after an explicit data-restoration rehearsal.
    connection = op.get_bind()
    compacted = connection.exec_driver_sql("""
        SELECT EXISTS (SELECT 1 FROM analysis.ticker_decision
                       WHERE market_state_context_hash IS NOT NULL
                          OR risk_policy_context_hash IS NOT NULL)
    """).scalar()
    legacy = connection.exec_driver_sql("SELECT to_regclass('analysis.ticker_input_manifest_legacy')").scalar()
    if compacted or legacy is None:
        raise RuntimeError("restore inline context and the legacy manifest table before downgrading decision storage")
    op.execute("""
        DROP VIEW analysis.ticker_decision_read;
        ALTER TABLE analysis.ticker_decision
          DROP COLUMN market_state_context_hash, DROP COLUMN risk_policy_context_hash;
        DROP TABLE analysis.decision_context;
        DROP FUNCTION analysis.reject_decision_context_mutation();
        ALTER TABLE analysis.ticker_input_manifest_legacy RENAME TO ticker_input_manifest;
        GRANT INSERT, UPDATE ON analysis.ticker_input_manifest TO market_app;
        GRANT USAGE ON SEQUENCE analysis.ticker_input_manifest_id_seq TO market_app;
    """)
