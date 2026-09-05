"""Bind Phase 4 calibration and CSP assignment data to persisted paper evidence."""

from __future__ import annotations

from alembic import op


revision = "20260904_0072"
down_revision = "20260902_0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE app.paper_order
            ADD COLUMN IF NOT EXISTS execution_quote JSONB,
            ADD COLUMN IF NOT EXISTS fill_evidence_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS contract_multiplier NUMERIC(20, 6),
            ADD COLUMN IF NOT EXISTS entry_fees NUMERIC(20, 6) NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS exit_fees NUMERIC(20, 6) NOT NULL DEFAULT 0;
        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_paper_execution_evidence()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE paper app.paper_order%ROWTYPE;
        BEGIN
            SELECT * INTO paper FROM app.paper_order WHERE id = NEW.paper_order_id;
            IF NEW.filled_quantity > 0 AND (paper.id IS NULL OR NOT paper.paper_only OR paper.submitted_at IS NULL
               OR paper.filled_at IS NULL OR paper.fill_evidence_at IS NULL
               OR paper.execution_quote IS NULL OR paper.fees IS NULL OR paper.entry_slippage IS NULL
               OR paper.contract_multiplier IS NULL OR paper.filled_quantity <= 0
               OR paper.actual_fill_price IS NULL OR paper.fill_evidence_at <= paper.filled_at
               OR NEW.observed_at IS DISTINCT FROM paper.filled_at
               OR NEW.available_at IS DISTINCT FROM paper.fill_evidence_at
               OR NEW.fill_price IS DISTINCT FROM paper.actual_fill_price
               OR NEW.filled_quantity > paper.filled_quantity) THEN
                RAISE EXCEPTION 'Phase 4 observation requires persisted paper fill evidence';
            END IF;
            RETURN NEW;
        END;
        $$;
        DROP TRIGGER IF EXISTS paper_execution_observation_evidence ON app.paper_execution_observation;
        CREATE TRIGGER paper_execution_observation_evidence
            BEFORE INSERT ON app.paper_execution_observation
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_paper_execution_evidence();

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_execution_snapshot_evidence()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE expected_cutoff TIMESTAMPTZ;
        BEGIN
            SELECT input_cutoff INTO expected_cutoff
            FROM analysis.portfolio_allocation_snapshot
            WHERE allocation_id = NEW.allocation_id;
            IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN
                RAISE EXCEPTION 'Phase 4 calibrated snapshot cutoff is not bound to its allocation';
            END IF;
            IF NEW.calibration_status = 'calibrated' AND (
                NEW.sample_count <= 0
                OR jsonb_typeof(NEW.metadata->'paper_observation_ids') IS DISTINCT FROM 'array'
                OR jsonb_array_length(NEW.metadata->'paper_observation_ids') <> NEW.sample_count
                OR EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'paper_observation_ids') id
                    JOIN app.paper_execution_observation observation ON observation.paper_execution_observation_id = id
                    JOIN analysis.portfolio_allocation_item item ON item.allocation_item_id = observation.allocation_item_id
                    JOIN app.paper_order paper ON paper.id = observation.paper_order_id
                    WHERE item.allocation_id <> NEW.allocation_id
                       OR observation.available_at > NEW.input_cutoff
                       OR observation.filled_quantity <= 0
                       OR observation.fill_price IS NULL
                       OR observation.observed_at IS DISTINCT FROM paper.filled_at
                       OR observation.available_at IS DISTINCT FROM paper.fill_evidence_at
                       OR paper.execution_quote IS NULL OR paper.fees IS NULL
                       OR paper.entry_slippage IS NULL OR paper.contract_multiplier IS NULL
                       OR paper.status NOT IN ('open', 'entered', 'partial_exited', 'exited', 'closed', 'invalidated')
                )
            ) THEN RAISE EXCEPTION 'Phase 4 calibrated snapshot requires matching persisted fill evidence'; END IF;
            RETURN NEW;
        END;
        $$;
        DROP TRIGGER IF EXISTS execution_model_snapshot_evidence ON analysis.execution_model_snapshot;
        CREATE TRIGGER execution_model_snapshot_evidence
            BEFORE INSERT ON analysis.execution_model_snapshot
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_execution_snapshot_evidence();
    """)


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER IF EXISTS execution_model_snapshot_evidence ON analysis.execution_model_snapshot;
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_execution_snapshot_evidence();
        DROP TRIGGER IF EXISTS paper_execution_observation_evidence ON app.paper_execution_observation;
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_paper_execution_evidence();
        ALTER TABLE app.paper_order DROP COLUMN IF EXISTS exit_fees;
        ALTER TABLE app.paper_order DROP COLUMN IF EXISTS entry_fees;
        ALTER TABLE app.paper_order DROP COLUMN IF EXISTS contract_multiplier;
        ALTER TABLE app.paper_order DROP COLUMN IF EXISTS fill_evidence_at;
        ALTER TABLE app.paper_order DROP COLUMN IF EXISTS execution_quote;
    """)
