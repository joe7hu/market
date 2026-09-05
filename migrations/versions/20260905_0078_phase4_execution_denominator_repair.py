"""Repair Phase 4 execution calibration for databases already at 0077."""

from __future__ import annotations

from alembic import op


revision = "20260905_0078"
down_revision = "20260904_0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        GRANT SELECT ON TABLE public.alembic_version TO market_app;
        ALTER FUNCTION analysis.write_phase4_execution(JSONB, TEXT)
          RENAME TO write_phase4_execution_0077;
        REVOKE ALL ON FUNCTION analysis.write_phase4_execution_0077(JSONB, TEXT)
          FROM PUBLIC, market_app, market_migrator;

        CREATE OR REPLACE FUNCTION analysis.write_phase4_execution(p JSONB, sig TEXT)
        RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, analysis, app, public AS $$
        DECLARE
          allocation_cutoff TIMESTAMPTZ;
          complete_cutoff TIMESTAMPTZ;
          target_allocation_id TEXT;
          requested_cutoff TIMESTAMPTZ;
          requested_ids JSONB;
          eligible_ids JSONB;
        BEGIN
          IF NOT analysis.phase4_telemetry_authorized('phase4-execution.v1', p, sig) THEN
            RAISE EXCEPTION 'Phase 4 execution authorization signature is invalid';
          END IF;
          target_allocation_id := p->>'allocation_id';
          requested_cutoff := (p->>'input_cutoff')::TIMESTAMPTZ;
          SELECT input_cutoff INTO allocation_cutoff
            FROM analysis.portfolio_allocation_snapshot snapshot
           WHERE snapshot.allocation_id = target_allocation_id;
          IF allocation_cutoff IS NULL OR requested_cutoff IS NULL THEN
            RAISE EXCEPTION 'Phase 4 execution allocation or cutoff is not persisted';
          END IF;
          IF jsonb_typeof(p->'metadata'->'paper_observation_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Phase 4 execution observations must be an array';
          END IF;
          SELECT coalesce(jsonb_agg(value ORDER BY value), '[]'::jsonb)
            INTO requested_ids
            FROM jsonb_array_elements_text(p->'metadata'->'paper_observation_ids') AS ids(value);
          SELECT coalesce(jsonb_agg(observation.paper_execution_observation_id ORDER BY observation.paper_execution_observation_id), '[]'::jsonb),
                 max(observation.available_at)
            INTO eligible_ids, complete_cutoff
            FROM app.paper_execution_observation observation
            JOIN analysis.portfolio_allocation_item item
              ON item.allocation_item_id = observation.allocation_item_id
            JOIN app.paper_order paper ON paper.id = observation.paper_order_id
           WHERE item.allocation_id = target_allocation_id
             AND observation.action_id = item.action_id
             AND paper.policy_result->>'trade_plan_id' = observation.action_id
             AND observation.available_at > allocation_cutoff
             AND observation.observed_at < observation.available_at
             AND observation.execution_mode = 'paper'
             AND observation.paper_only
             AND observation.status IN ('partial', 'filled', 'partial_exited', 'exited')
             AND observation.filled_quantity > 0
             AND observation.fill_price IS NOT NULL
             AND observation.contract_multiplier IS NOT NULL
             AND observation.event_fee IS NOT NULL
             AND paper.paper_only
             AND paper.submitted_at IS NOT NULL
             AND paper.filled_at IS NOT NULL
             AND paper.fill_evidence_at IS NOT NULL
             AND paper.fill_evidence_at > paper.filled_at
             AND paper.execution_quote IS NOT NULL
             AND paper.fees IS NOT NULL
             AND paper.entry_slippage IS NOT NULL
             AND paper.actual_fill_price IS NOT NULL
             AND paper.filled_quantity > 0
             AND paper.contract_multiplier IS NOT NULL
             AND paper.status IN ('open', 'entered', 'partial_exited', 'exited', 'closed', 'invalidated');
          IF requested_ids IS DISTINCT FROM eligible_ids THEN
            RAISE EXCEPTION 'Phase 4 execution observations must equal the complete eligible allocation fill set';
          END IF;
          IF complete_cutoff IS NOT NULL AND requested_cutoff IS DISTINCT FROM complete_cutoff THEN
            RAISE EXCEPTION 'Phase 4 execution cutoff must equal maximum allocation observation availability';
          END IF;
          PERFORM analysis.write_phase4_execution_0077(p, sig);
        END;
        $$;
        REVOKE ALL ON FUNCTION analysis.write_phase4_execution(JSONB, TEXT)
          FROM PUBLIC, market_migrator;
        GRANT EXECUTE ON FUNCTION analysis.write_phase4_execution(JSONB, TEXT) TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        REVOKE ALL ON FUNCTION analysis.write_phase4_execution(JSONB, TEXT) FROM PUBLIC, market_app, market_migrator;
        REVOKE SELECT ON TABLE public.alembic_version FROM market_app;
        DROP FUNCTION analysis.write_phase4_execution(JSONB, TEXT);
        ALTER FUNCTION analysis.write_phase4_execution_0077(JSONB, TEXT)
          RENAME TO write_phase4_execution;
        GRANT EXECUTE ON FUNCTION analysis.write_phase4_execution(JSONB, TEXT) TO market_app;
        """
    )
