"""Bind strategy signals to complete runs and explicit input provenance."""

from alembic import op


revision = "20260911_0018"
down_revision = "20260910_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE analysis.run DROP CONSTRAINT run_status_check;
        ALTER TABLE analysis.run ADD CONSTRAINT run_status_check
            CHECK (status = ANY (ARRAY['running', 'succeeded', 'partial', 'failed', 'canceled']));

        ALTER TABLE analysis.strategy_evaluation
            ADD COLUMN run_id uuid,
            ADD COLUMN scope text,
            ADD COLUMN mode text,
            ADD COLUMN input_manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN output_hash character(64),
            ADD CONSTRAINT strategy_evaluation_run_id_fkey
                FOREIGN KEY (run_id) REFERENCES analysis.run(id),
            ADD CONSTRAINT strategy_evaluation_mode_check
                CHECK (mode IS NULL OR mode = ANY (ARRAY['research', 'replay'])),
            ADD CONSTRAINT strategy_evaluation_input_manifest_check
                CHECK (jsonb_typeof(input_manifest) = 'object'),
            ADD CONSTRAINT strategy_evaluation_output_hash_check
                CHECK (output_hash IS NULL OR output_hash ~ '^[0-9a-f]{64}$');

        CREATE INDEX ix_strategy_evaluation_current_run
            ON analysis.strategy_evaluation (strategy_revision_id, run_id, scope, evaluated_at DESC, id DESC);
        CREATE UNIQUE INDEX ux_strategy_evaluation_run_identity
            ON analysis.strategy_evaluation (run_id, strategy_revision_id, scope, mode, input_hash)
            WHERE run_id IS NOT NULL;
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS analysis.ix_strategy_evaluation_current_run;
        DROP INDEX IF EXISTS analysis.ux_strategy_evaluation_run_identity;
        ALTER TABLE analysis.strategy_evaluation
            DROP CONSTRAINT strategy_evaluation_output_hash_check,
            DROP CONSTRAINT strategy_evaluation_input_manifest_check,
            DROP CONSTRAINT strategy_evaluation_mode_check,
            DROP CONSTRAINT strategy_evaluation_run_id_fkey,
            DROP COLUMN output_hash,
            DROP COLUMN input_manifest,
            DROP COLUMN mode,
            DROP COLUMN scope,
            DROP COLUMN run_id;
        UPDATE analysis.run
           SET summary = summary || '{"downgraded_from":"canceled"}'::jsonb,
               status = 'failed'
         WHERE status = 'canceled';
        ALTER TABLE analysis.run DROP CONSTRAINT run_status_check;
        ALTER TABLE analysis.run ADD CONSTRAINT run_status_check
            CHECK (status = ANY (ARRAY['running', 'succeeded', 'partial', 'failed']));
        """,
    )
