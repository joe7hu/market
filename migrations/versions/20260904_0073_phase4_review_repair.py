"""Bind Phase 4 review repairs to persisted PostgreSQL evidence."""

from __future__ import annotations

from alembic import op

revision = "20260904_0073"
down_revision = "20260904_0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_review_snapshot_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF coalesce(NEW.metadata->>'authority', '') <> 'postgresql'
               OR coalesce(NEW.metadata->>'authority_snapshot_id', '') = ''
               OR jsonb_typeof(NEW.metadata->'source_hashes') IS DISTINCT FROM 'array'
               OR jsonb_array_length(NEW.metadata->'source_hashes') = 0
               OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'source_hashes') hash
                          WHERE hash !~ '^[0-9a-f]{64}$' OR hash = repeat('0', 64)) THEN
                RAISE EXCEPTION 'Phase 4 allocation requires PostgreSQL authority evidence';
            END IF;
            IF NEW.status <> 'cash_only' AND NOT EXISTS (
                SELECT 1 FROM raw.broker_account_snapshot account
                WHERE ('broker-account:' || account.id::text) = NEW.metadata->>'authority_snapshot_id'
                  AND account.observed_at <= NEW.input_cutoff
            ) THEN
                RAISE EXCEPTION 'Phase 4 allocation authority is not persisted';
            END IF;
            NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
                'allocation_id', NEW.allocation_id, 'as_of', analysis.phase4_canonical_timestamp(NEW.as_of),
                'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff), 'status', NEW.status,
                'cash_hurdle', NEW.cash_hurdle, 'forecast_ids', NEW.forecast_ids,
                'action_ids', NEW.action_ids, 'strategy_registry_ids', NEW.strategy_registry_ids,
                'metadata', NEW.metadata
            ));
            RETURN NEW;
        END;
        $$;
        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_review_item_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE expected_cutoff TIMESTAMPTZ; expected_forecast TEXT; expected_hypothesis UUID;
        BEGIN
            NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
                'allocation_item_id', NEW.allocation_item_id, 'allocation_id', NEW.allocation_id,
                'candidate_id', NEW.candidate_id, 'ticker', NEW.ticker,
                'strategy_forecast_id', NEW.strategy_forecast_id, 'action_id', NEW.action_id,
                'rank_id', NEW.rank_id, 'hypothesis_id', NEW.hypothesis_id,
                'disposition', NEW.disposition, 'target_weight', NEW.target_weight,
                'current_weight', NEW.current_weight, 'marginal_book_utility', NEW.marginal_book_utility,
                'trace', NEW.trace, 'blockers', NEW.blockers, 'funding_source', NEW.funding_source,
                'funding_amount', NEW.funding_amount
            ));
            IF NEW.disposition = 'selected' AND NEW.ticker <> 'CASH'
               AND NEW.target_weight > NEW.current_weight THEN
                IF NEW.funding_amount IS NULL OR NEW.funding_amount <= 0
                   OR NEW.funding_source NOT LIKE 'CASH:broker-account:%' THEN
                    RAISE EXCEPTION 'Phase 4 increase requires persisted PostgreSQL cash';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM raw.broker_account_snapshot account
                    WHERE ('CASH:broker-account:' || account.id::text) = NEW.funding_source
                      AND account.cash_balance >= NEW.funding_amount
                      AND account.observed_at <= (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id)
                ) THEN
                    RAISE EXCEPTION 'Phase 4 cash funding is not persisted';
                END IF;
            END IF;
            IF NEW.strategy_forecast_id IS NOT NULL THEN
                SELECT forecast.input_cutoff, forecast.id, revision.hypothesis_id
                  INTO expected_cutoff, expected_forecast, expected_hypothesis
                FROM analysis.strategy_forecast forecast
                JOIN analysis.strategy_revision revision ON revision.id = forecast.strategy_revision_id
                WHERE forecast.id = NEW.strategy_forecast_id;
                IF expected_forecast IS NULL OR expected_cutoff > (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id)
                   OR NEW.hypothesis_id IS DISTINCT FROM expected_hypothesis THEN
                    RAISE EXCEPTION 'Phase 4 allocation forecast lineage is invalid';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER phase4_review_snapshot_guard BEFORE INSERT ON analysis.portfolio_allocation_snapshot FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_review_snapshot_guard();
        CREATE TRIGGER phase4_review_item_guard BEFORE INSERT ON analysis.portfolio_allocation_item FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_review_item_guard();

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_execution_snapshot_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE expected_cutoff TIMESTAMPTZ;
        BEGIN
            SELECT input_cutoff INTO expected_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id;
            IF expected_cutoff IS NULL OR NEW.input_cutoff > expected_cutoff THEN
                RAISE EXCEPTION 'Phase 4 execution snapshot cutoff is not bounded by allocation';
            END IF;
            IF NEW.calibration_status = 'calibrated' AND (
                NEW.sample_count <= 0 OR jsonb_typeof(NEW.metadata->'paper_observation_ids') IS DISTINCT FROM 'array'
                OR jsonb_array_length(NEW.metadata->'paper_observation_ids') <> NEW.sample_count
                OR EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'paper_observation_ids') id
                    JOIN app.paper_execution_observation observation ON observation.paper_execution_observation_id = id
                    JOIN analysis.portfolio_allocation_item item ON item.allocation_item_id = observation.allocation_item_id
                    JOIN app.paper_order paper ON paper.id = observation.paper_order_id
                    WHERE item.allocation_id <> NEW.allocation_id OR observation.available_at > NEW.input_cutoff
                       OR observation.observed_at IS DISTINCT FROM paper.filled_at OR observation.available_at IS DISTINCT FROM paper.fill_evidence_at
                       OR observation.filled_quantity <= 0 OR observation.fill_price IS NULL OR paper.submitted_at IS NULL
                       OR paper.filled_at IS NULL OR paper.fill_evidence_at IS NULL OR paper.execution_quote IS NULL
                       OR paper.entry_fees IS NULL OR paper.contract_multiplier IS NULL OR paper.actual_fill_price IS NULL
                       OR paper.status NOT IN ('open', 'entered', 'partial_exited', 'exited', 'closed', 'invalidated')
                )
            ) THEN RAISE EXCEPTION 'Phase 4 calibrated snapshot requires matching persisted fills'; END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER phase4_execution_snapshot_guard BEFORE INSERT ON analysis.execution_model_snapshot FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_execution_snapshot_guard();

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_paper_execution_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE paper app.paper_order%ROWTYPE;
        BEGIN
            SELECT * INTO paper FROM app.paper_order WHERE id = NEW.paper_order_id;
            IF NEW.filled_quantity > 0 AND (paper.id IS NULL OR NOT paper.paper_only OR paper.submitted_at IS NULL
               OR paper.filled_at IS NULL OR paper.fill_evidence_at IS NULL OR paper.execution_quote IS NULL
               OR paper.fees IS NULL OR paper.entry_fees IS NULL OR paper.entry_slippage IS NULL
               OR paper.contract_multiplier IS NULL OR paper.filled_quantity <= 0 OR paper.actual_fill_price IS NULL
               OR paper.fill_evidence_at <= paper.filled_at OR NEW.observed_at IS DISTINCT FROM paper.filled_at
               OR NEW.available_at IS DISTINCT FROM paper.fill_evidence_at OR NEW.fill_price IS DISTINCT FROM paper.actual_fill_price
               OR NEW.filled_quantity > paper.filled_quantity) THEN
                RAISE EXCEPTION 'Phase 4 observation requires persisted paper fill evidence';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER phase4_paper_execution_guard BEFORE INSERT ON app.paper_execution_observation FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_paper_execution_guard();

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_attribution_multiplier_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE expected_realized_pnl DOUBLE PRECISION;
        BEGIN
            IF NEW.pnl_status = 'realized' THEN
                SELECT CASE WHEN observation.side = 'buy' THEN 1 ELSE -1 END * (observation.exit_price - observation.fill_price)
                         * observation.filled_quantity * paper.contract_multiplier - coalesce(paper.fees, 0)
                  INTO expected_realized_pnl
                FROM app.paper_execution_observation observation JOIN app.paper_order paper ON paper.id = observation.paper_order_id
                WHERE observation.paper_execution_observation_id = NEW.paper_execution_observation_id;
                IF expected_realized_pnl IS NULL OR abs(NEW.realized_pnl - expected_realized_pnl) > 0.000000001 THEN
                    RAISE EXCEPTION 'Phase 4 attribution does not match persisted multiplier and fees';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER phase4_attribution_multiplier_guard BEFORE INSERT ON analysis.book_attribution FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_attribution_multiplier_guard();
    """)


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER IF EXISTS phase4_attribution_multiplier_guard ON analysis.book_attribution;
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_attribution_multiplier_guard();
        DROP TRIGGER IF EXISTS phase4_review_snapshot_guard ON analysis.portfolio_allocation_snapshot;
        DROP TRIGGER IF EXISTS phase4_review_item_guard ON analysis.portfolio_allocation_item;
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_review_snapshot_guard();
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_review_item_guard();
        DROP TRIGGER IF EXISTS phase4_execution_snapshot_guard ON analysis.execution_model_snapshot;
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_execution_snapshot_guard();
        DROP TRIGGER IF EXISTS phase4_paper_execution_guard ON app.paper_execution_observation;
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_paper_execution_guard();
    """)
