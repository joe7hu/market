"""Close Phase 4 authority, execution, and partial-exit gaps."""

from __future__ import annotations

from alembic import op


revision = "20260904_0074"
down_revision = "20260904_0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE app.paper_execution_observation
            ADD COLUMN IF NOT EXISTS event_fee DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS contract_multiplier DOUBLE PRECISION;

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_authority_lineage()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE allocation_meta JSONB; allocation_cutoff TIMESTAMPTZ;
        BEGIN
            SELECT metadata, input_cutoff INTO allocation_meta, allocation_cutoff
              FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id;
            IF NEW.disposition = 'selected' AND NEW.ticker <> 'CASH' THEN
                IF allocation_meta IS NULL
                   OR allocation_meta->>'authority' <> 'postgresql'
                   OR NOT EXISTS (
                       SELECT 1 FROM raw.broker_account_snapshot account
                        WHERE ('broker-account:' || account.id::text) = allocation_meta->>'authority_snapshot_id'
                          AND account.observed_at <= allocation_cutoff
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM analysis.ticker_decision decision
                        JOIN analysis.strategy_forecast forecast
                          ON forecast.id = NEW.strategy_forecast_id
                       WHERE decision.id::text = NEW.trace->>'source_decision_id'
                         AND decision.input_hash = NEW.trace->>'source_decision_input_hash'
                         AND decision.status = 'published' AND decision.published_at IS NOT NULL
                         AND decision.input_manifest->'trade_plan'->>'trade_plan_id' = NEW.action_id
                         AND decision.input_manifest->'trade_plan'->>'rank_id' = NEW.rank_id
                         AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id
                         AND forecast.input_cutoff <= allocation_cutoff
                   )
                   OR jsonb_typeof(allocation_meta->'source_hashes') <> 'array'
                   OR NOT EXISTS (
                       SELECT 1 FROM jsonb_array_elements_text(allocation_meta->'source_hashes') hash
                        WHERE hash = NEW.trace->>'source_decision_input_hash'
                   ) THEN
                    RAISE EXCEPTION 'Phase 4 allocation authority is not bound to PostgreSQL action lineage';
                END IF;
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF EXISTS (
                    SELECT 1
                      FROM analysis.portfolio_allocation_item item
                     WHERE item.allocation_id = NEW.allocation_id
                       AND item.disposition = 'selected' AND item.ticker <> 'CASH'
                       AND NOT EXISTS (
                           SELECT 1 FROM jsonb_array_elements_text(allocation_meta->'source_hashes') hash
                            WHERE hash = item.trace->>'source_decision_input_hash'
                       )
                ) THEN
                    RAISE EXCEPTION 'Phase 4 allocation source hashes are incomplete';
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$;
        DROP TRIGGER IF EXISTS phase4_authority_lineage ON analysis.portfolio_allocation_item;
        CREATE CONSTRAINT TRIGGER phase4_authority_lineage
            AFTER INSERT ON analysis.portfolio_allocation_item DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_authority_lineage();
        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_authority_snapshot_complete()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status <> 'cash_only' AND NOT EXISTS (
                SELECT 1 FROM analysis.portfolio_allocation_item item
                 WHERE item.allocation_id = NEW.allocation_id
                   AND item.disposition = 'selected' AND item.ticker <> 'CASH'
            ) THEN RAISE EXCEPTION 'Phase 4 available allocation requires PostgreSQL-bound actions'; END IF;
            RETURN NULL;
        END;
        $$;
        DROP TRIGGER IF EXISTS phase4_authority_snapshot_complete ON analysis.portfolio_allocation_snapshot;
        CREATE CONSTRAINT TRIGGER phase4_authority_snapshot_complete
            AFTER INSERT ON analysis.portfolio_allocation_snapshot DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_authority_snapshot_complete();

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_execution_snapshot_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE expected_cutoff TIMESTAMPTZ;
        BEGIN
            SELECT input_cutoff INTO expected_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id;
            IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN
                RAISE EXCEPTION 'Phase 4 execution snapshot cutoff is not bound to its allocation';
            END IF;
            IF NEW.calibration_status = 'calibrated' AND (
                NEW.sample_count <= 0 OR jsonb_typeof(NEW.metadata->'paper_observation_ids') IS DISTINCT FROM 'array'
                OR jsonb_array_length(NEW.metadata->'paper_observation_ids') <> NEW.sample_count
                OR EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'paper_observation_ids') id
                    LEFT JOIN app.paper_execution_observation observation ON observation.paper_execution_observation_id = id
                    LEFT JOIN analysis.portfolio_allocation_item item ON item.allocation_item_id = observation.allocation_item_id
                    LEFT JOIN app.paper_order paper ON paper.id = observation.paper_order_id
                    WHERE observation.paper_execution_observation_id IS NULL
                       OR item.allocation_id <> NEW.allocation_id
                       OR observation.available_at > NEW.input_cutoff
                       OR observation.filled_quantity <= 0 OR observation.fill_price IS NULL
                       OR observation.contract_multiplier IS NULL OR observation.event_fee IS NULL
                       OR paper.submitted_at IS NULL OR paper.filled_at IS NULL OR paper.fill_evidence_at IS NULL
                       OR paper.execution_quote IS NULL OR paper.actual_fill_price IS NULL
                )
            ) THEN RAISE EXCEPTION 'Phase 4 calibrated snapshot requires matching allocation paper fills'; END IF;
            RETURN NEW;
        END;
        $$;

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_paper_execution_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE paper app.paper_order%ROWTYPE;
        BEGIN
            SELECT * INTO paper FROM app.paper_order WHERE id = NEW.paper_order_id;
            IF NEW.filled_quantity > 0 AND (paper.id IS NULL OR NOT paper.paper_only OR paper.submitted_at IS NULL
               OR paper.filled_at IS NULL OR paper.fill_evidence_at IS NULL OR paper.execution_quote IS NULL
               OR paper.entry_slippage IS NULL OR paper.contract_multiplier IS NULL OR paper.filled_quantity <= 0
               OR paper.actual_fill_price IS NULL OR NEW.contract_multiplier IS NULL OR NEW.event_fee IS NULL
               OR NEW.contract_multiplier <> paper.contract_multiplier OR NEW.event_fee < 0
               OR NEW.filled_quantity > paper.filled_quantity OR NEW.fill_price IS DISTINCT FROM paper.actual_fill_price
               OR (NEW.status NOT IN ('exited', 'partial_exited') AND (NEW.observed_at IS DISTINCT FROM paper.filled_at OR NEW.available_at IS DISTINCT FROM paper.fill_evidence_at))
               OR (NEW.status IN ('exited', 'partial_exited') AND (paper.exit_at IS NULL OR NEW.observed_at IS DISTINCT FROM paper.exit_at OR NEW.available_at < NEW.observed_at))) THEN
                RAISE EXCEPTION 'Phase 4 observation requires persisted paper fill evidence';
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_attribution_multiplier_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE expected_realized_pnl DOUBLE PRECISION; expected_allocation TEXT; expected_forecast TEXT;
                expected_hypothesis UUID; expected_action TEXT; expected_rank TEXT; expected_expression JSONB;
                expected_experiment TEXT; expected_trial UUID; expected_result UUID; expected_cutoff TIMESTAMPTZ;
        BEGIN
            NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
                'book_attribution_id', NEW.book_attribution_id, 'allocation_id', NEW.allocation_id,
                'allocation_item_id', NEW.allocation_item_id, 'strategy_forecast_id', NEW.strategy_forecast_id,
                'hypothesis_id', NEW.hypothesis_id, 'action_id', NEW.action_id, 'rank_id', NEW.rank_id,
                'expression', NEW.expression, 'experiment_id', NEW.experiment_id, 'trial_id', NEW.trial_id,
                'result_id', NEW.result_id, 'paper_execution_observation_id', NEW.paper_execution_observation_id,
                'pnl_status', NEW.pnl_status, 'realized_pnl', NEW.realized_pnl, 'attribution', NEW.attribution,
                'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff)
            ));
            IF NEW.pnl_status = 'realized' THEN
                SELECT sum((CASE WHEN observation.side = 'buy' THEN 1 ELSE -1 END)
                           * (observation.exit_price - observation.fill_price) * observation.filled_quantity
                           * observation.contract_multiplier - observation.event_fee)
                  INTO expected_realized_pnl
                  FROM app.paper_execution_observation observation
                 WHERE observation.allocation_item_id = NEW.allocation_item_id
                   AND observation.paper_order_id = (SELECT paper_order_id FROM app.paper_execution_observation WHERE paper_execution_observation_id = NEW.paper_execution_observation_id)
                   AND observation.status IN ('partial_exited', 'exited');
                IF expected_realized_pnl IS NULL OR abs(NEW.realized_pnl - expected_realized_pnl) > 0.000000001 THEN
                    RAISE EXCEPTION 'Phase 4 attribution does not match persisted event multiplier and fees';
                END IF;
            END IF;
            SELECT item.allocation_id, item.strategy_forecast_id, item.hypothesis_id, item.action_id, item.rank_id,
                   item.trace->'expression', forecast.research_trial_id, forecast.trial_result_id
              INTO expected_allocation, expected_forecast, expected_hypothesis, expected_action, expected_rank,
                   expected_expression, expected_trial, expected_result
              FROM analysis.portfolio_allocation_item item JOIN analysis.strategy_forecast forecast ON forecast.id = item.strategy_forecast_id
             WHERE item.allocation_item_id = NEW.allocation_item_id;
            IF expected_allocation IS NULL OR NEW.allocation_id IS DISTINCT FROM expected_allocation
               OR NEW.strategy_forecast_id IS DISTINCT FROM expected_forecast OR NEW.hypothesis_id IS DISTINCT FROM expected_hypothesis
               OR NEW.action_id IS DISTINCT FROM expected_action OR NEW.rank_id IS DISTINCT FROM expected_rank
               OR NEW.expression IS DISTINCT FROM expected_expression OR NEW.trial_id IS DISTINCT FROM expected_trial
               OR NEW.result_id IS DISTINCT FROM expected_result THEN RAISE EXCEPTION 'Phase 4 attribution does not match allocation lineage'; END IF;
            IF NEW.pnl_status = 'realized' AND NOT EXISTS (
                SELECT 1 FROM app.paper_execution_observation observation JOIN app.paper_order paper ON paper.id = observation.paper_order_id
                 WHERE observation.paper_execution_observation_id = NEW.paper_execution_observation_id
                   AND observation.allocation_item_id = NEW.allocation_item_id AND observation.paper_only
                   AND observation.execution_mode = 'paper' AND observation.status = 'exited' AND paper.status IN ('exited', 'closed')
            ) THEN RAISE EXCEPTION 'Phase 4 realized attribution requires a genuine linked paper fill'; END IF;
            SELECT input_cutoff INTO expected_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id;
            IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN RAISE EXCEPTION 'Phase 4 attribution input cutoff does not match allocation lineage'; END IF;
            SELECT decision.experiment_id INTO expected_experiment FROM analysis.ticker_decision decision
             WHERE decision.input_manifest->'trade_plan'->>'trade_plan_id' = NEW.action_id
               AND decision.input_manifest->'trade_plan'->>'rank_id' = NEW.rank_id
               AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id
               AND decision.status = 'published' AND decision.published_at IS NOT NULL
             ORDER BY decision.published_at DESC, decision.id DESC LIMIT 1;
            IF expected_experiment IS NULL OR NEW.experiment_id IS DISTINCT FROM expected_experiment THEN RAISE EXCEPTION 'Phase 4 attribution experiment lineage is invalid'; END IF;
            RETURN NEW;
        END;
        $$;

        CREATE OR REPLACE FUNCTION analysis.insert_phase4_allocation_snapshot(p JSONB)
        RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = analysis, raw, public AS $$
        BEGIN
            INSERT INTO analysis.portfolio_allocation_snapshot
                (allocation_id, as_of, input_cutoff, status, cash_hurdle, forecast_ids, action_ids,
                 strategy_registry_ids, input_hash, content_hash, metadata)
            VALUES (p->>'allocation_id', (p->>'as_of')::timestamptz, (p->>'input_cutoff')::timestamptz,
                    p->>'status', (p->>'cash_hurdle')::double precision, p->'forecast_ids', p->'action_ids',
                    p->'strategy_registry_ids', p->>'input_hash', p->>'content_hash', p->'metadata')
            ON CONFLICT (allocation_id) DO NOTHING;
        END;
        $$;
        CREATE OR REPLACE FUNCTION analysis.insert_phase4_allocation_item(p JSONB)
        RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = analysis, raw, public AS $$
        BEGIN
            INSERT INTO analysis.portfolio_allocation_item
                (allocation_item_id, allocation_id, candidate_id, ticker, strategy_forecast_id, action_id,
                 rank_id, hypothesis_id, disposition, target_weight, current_weight, marginal_book_utility,
                 trace, blockers, funding_source, funding_amount, input_hash, content_hash)
            VALUES (p->>'allocation_item_id', p->>'allocation_id', p->>'candidate_id', p->>'ticker',
                    NULLIF(p->>'strategy_forecast_id', ''), NULLIF(p->>'action_id', ''), NULLIF(p->>'rank_id', ''),
                    NULLIF(p->>'hypothesis_id', '')::uuid, p->>'disposition', (p->>'target_weight')::double precision,
                    (p->>'current_weight')::double precision, (p->>'marginal_book_utility')::double precision,
                    p->'trace', p->'blockers', NULLIF(p->>'funding_source', ''), (p->>'funding_amount')::double precision,
                    p->>'input_hash', p->>'content_hash')
            ON CONFLICT (allocation_item_id) DO NOTHING;
        END;
        $$;
        CREATE OR REPLACE FUNCTION analysis.insert_phase4_paper_execution_observation(p JSONB)
        RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = analysis, app, public AS $$
        BEGIN
            INSERT INTO app.paper_execution_observation
                (paper_execution_observation_id, allocation_item_id, action_id, paper_order_id, execution_mode,
                 paper_only, status, requested_quantity, filled_quantity, requested_price, fill_price, spread_bps,
                 latency_ms, impact_bps, side, exit_price, event_fee, contract_multiplier, observed_at, available_at, metadata)
            VALUES (p->>'paper_execution_observation_id', p->>'allocation_item_id', p->>'action_id',
                    (p->>'paper_order_id')::uuid, p->>'execution_mode', (p->>'paper_only')::boolean, p->>'status',
                    (p->>'requested_quantity')::double precision, (p->>'filled_quantity')::double precision,
                    (p->>'requested_price')::double precision, (p->>'fill_price')::double precision,
                    (p->>'spread_bps')::double precision, (p->>'latency_ms')::double precision,
                    (p->>'impact_bps')::double precision, p->>'side', (p->>'exit_price')::double precision,
                    (p->>'event_fee')::double precision, (p->>'contract_multiplier')::double precision,
                    (p->>'observed_at')::timestamptz, (p->>'available_at')::timestamptz, p->'metadata')
            ON CONFLICT (paper_execution_observation_id) DO NOTHING;
        END;
        $$;
        CREATE OR REPLACE FUNCTION analysis.insert_phase4_book_attribution(p JSONB)
        RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = analysis, app, public AS $$
        BEGIN
            INSERT INTO analysis.book_attribution
                (book_attribution_id, allocation_id, allocation_item_id, strategy_forecast_id, hypothesis_id,
                 action_id, rank_id, expression, experiment_id, trial_id, result_id, paper_execution_observation_id,
                 pnl_status, realized_pnl, attribution, input_cutoff, input_hash, content_hash)
            VALUES (p->>'book_attribution_id', p->>'allocation_id', p->>'allocation_item_id', p->>'strategy_forecast_id',
                    (p->>'hypothesis_id')::uuid, p->>'action_id', p->>'rank_id', p->'expression', p->>'experiment_id',
                    (p->>'trial_id')::uuid, (p->>'result_id')::uuid, p->>'paper_execution_observation_id',
                    p->>'pnl_status', (p->>'realized_pnl')::double precision, p->'attribution',
                    (p->>'input_cutoff')::timestamptz, p->>'input_hash', p->>'content_hash')
            ON CONFLICT (book_attribution_id) DO NOTHING;
        END;
        $$;

        REVOKE INSERT ON analysis.portfolio_allocation_snapshot, analysis.portfolio_allocation_item,
            analysis.book_attribution, app.paper_execution_observation FROM market_app;
        GRANT EXECUTE ON FUNCTION analysis.insert_phase4_allocation_snapshot(JSONB),
            analysis.insert_phase4_allocation_item(JSONB), analysis.insert_phase4_paper_execution_observation(JSONB),
            analysis.insert_phase4_book_attribution(JSONB) TO market_app;
    """)


def downgrade() -> None:
    op.execute("""
        GRANT INSERT ON analysis.portfolio_allocation_snapshot, analysis.portfolio_allocation_item,
            analysis.book_attribution, app.paper_execution_observation TO market_app;
        REVOKE EXECUTE ON FUNCTION analysis.insert_phase4_allocation_snapshot(JSONB),
            analysis.insert_phase4_allocation_item(JSONB), analysis.insert_phase4_paper_execution_observation(JSONB),
            analysis.insert_phase4_book_attribution(JSONB) FROM market_app;
        DROP TRIGGER IF EXISTS phase4_authority_lineage ON analysis.portfolio_allocation_item;
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_authority_lineage();
        DROP TRIGGER IF EXISTS phase4_authority_snapshot_complete ON analysis.portfolio_allocation_snapshot;
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_authority_snapshot_complete();
        ALTER TABLE app.paper_execution_observation DROP COLUMN IF EXISTS event_fee;
        ALTER TABLE app.paper_execution_observation DROP COLUMN IF EXISTS contract_multiplier;
    """)
