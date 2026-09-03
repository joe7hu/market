"""Add immutable Phase 4 allocation, scenario, execution, and attribution artifacts."""

from __future__ import annotations

from alembic import op


revision = "20260902_0071"
down_revision = "20260902_0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE analysis.portfolio_allocation_snapshot (
            allocation_id TEXT PRIMARY KEY,
            as_of TIMESTAMPTZ NOT NULL,
            input_cutoff TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('available', 'cash_only', 'unavailable')),
            cash_hurdle DOUBLE PRECISION CHECK (cash_hurdle IS NULL OR (cash_hurdle < 'Infinity'::double precision AND cash_hurdle > '-Infinity'::double precision AND cash_hurdle >= 0)),
            forecast_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            action_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            strategy_registry_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            input_hash CHAR(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$' AND input_hash <> repeat('0', 64)),
            content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$' AND content_hash <> repeat('0', 64)),
            available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            CHECK (as_of = input_cutoff),
            CHECK (status <> 'available' OR cash_hurdle > 0),
            CHECK (allocation_id = 'allocation:' || input_hash::text),
            CHECK (jsonb_typeof(forecast_ids) = 'array'),
            CHECK (jsonb_typeof(action_ids) = 'array'),
            CHECK (jsonb_typeof(strategy_registry_ids) = 'array')
        );

        CREATE TABLE analysis.portfolio_allocation_item (
            allocation_item_id TEXT PRIMARY KEY,
            allocation_id TEXT NOT NULL REFERENCES analysis.portfolio_allocation_snapshot(allocation_id),
            candidate_id TEXT NOT NULL DEFAULT '',
            ticker TEXT NOT NULL,
            strategy_forecast_id TEXT REFERENCES analysis.strategy_forecast(id),
            action_id TEXT,
            rank_id TEXT,
            hypothesis_id UUID REFERENCES analysis.hypothesis(id),
            disposition TEXT NOT NULL CHECK (disposition IN ('selected', 'ranked_out', 'rejected', 'rollback')),
            target_weight DOUBLE PRECISION NOT NULL CHECK (target_weight < 'Infinity'::double precision AND target_weight > '-Infinity'::double precision AND target_weight >= 0 AND target_weight <= 1),
            current_weight DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (current_weight < 'Infinity'::double precision AND current_weight > '-Infinity'::double precision AND current_weight >= 0 AND current_weight <= 1),
            marginal_book_utility DOUBLE PRECISION NOT NULL CHECK (marginal_book_utility < 'Infinity'::double precision AND marginal_book_utility > '-Infinity'::double precision),
            trace JSONB NOT NULL,
            blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
            funding_source TEXT,
            funding_amount DOUBLE PRECISION,
            input_hash CHAR(64) NOT NULL,
            content_hash CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CHECK (jsonb_typeof(trace) = 'object'),
            CHECK (jsonb_typeof(blockers) = 'array'),
            CHECK (ticker = 'CASH' OR candidate_id <> ''),
            CHECK (input_hash ~ '^[0-9a-f]{64}$' AND input_hash <> repeat('0', 64) AND allocation_item_id = 'allocation-item:' || input_hash::text),
            CHECK (content_hash ~ '^[0-9a-f]{64}$' AND content_hash <> repeat('0', 64)),
            CHECK (ticker = 'CASH' OR disposition <> 'selected' OR (strategy_forecast_id IS NOT NULL AND action_id IS NOT NULL)),
            CHECK (ticker = 'CASH' OR disposition <> 'selected' OR rank_id IS NOT NULL),
            CHECK (disposition <> 'selected' OR (ticker = 'CASH' AND target_weight > 0 AND marginal_book_utility >= 0) OR (target_weight > 0 AND marginal_book_utility > 0)),
            CHECK (ticker <> '')
            ,CHECK (ticker = 'CASH' OR disposition <> 'selected' OR (funding_source IS NOT NULL AND (funding_source LIKE 'CASH:%' OR funding_source LIKE 'TRIM:%')))
            ,CHECK (ticker = 'CASH' OR disposition <> 'selected' OR (funding_amount IS NOT NULL AND funding_amount > 0))
        );
        CREATE INDEX ix_portfolio_allocation_item_snapshot
            ON analysis.portfolio_allocation_item (allocation_id, disposition, target_weight DESC);

        CREATE TABLE analysis.probabilistic_portfolio_scenario_artifact (
            scenario_artifact_id TEXT PRIMARY KEY,
            allocation_id TEXT NOT NULL REFERENCES analysis.portfolio_allocation_snapshot(allocation_id),
            model_version TEXT NOT NULL,
            probability_semantics TEXT NOT NULL,
            scenarios JSONB NOT NULL,
            tail_dependence JSONB NOT NULL,
            simultaneous_unwind JSONB NOT NULL,
            input_cutoff TIMESTAMPTZ NOT NULL,
            input_hash CHAR(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$' AND input_hash <> repeat('0', 64)),
            content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$' AND content_hash <> repeat('0', 64)),
            available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CHECK (jsonb_typeof(scenarios) = 'array' AND jsonb_array_length(scenarios) > 0),
            CHECK (jsonb_typeof(tail_dependence) = 'object' AND tail_dependence <> '{}'::jsonb),
            CHECK (jsonb_typeof(simultaneous_unwind) = 'object' AND simultaneous_unwind <> '{}'::jsonb)
            ,CHECK (scenario_artifact_id = 'scenario:' || input_hash::text)
        );

        CREATE TABLE analysis.execution_model_snapshot (
            execution_model_snapshot_id TEXT PRIMARY KEY,
            allocation_id TEXT NOT NULL REFERENCES analysis.portfolio_allocation_snapshot(allocation_id),
            model_version TEXT NOT NULL,
            calibration_status TEXT NOT NULL CHECK (calibration_status IN ('calibrated', 'calibration_pending', 'unavailable')),
            sample_count INTEGER NOT NULL CHECK (sample_count >= 0),
            fill_probability DOUBLE PRECISION CHECK (fill_probability IS NULL OR (fill_probability < 'Infinity'::double precision AND fill_probability > '-Infinity'::double precision AND fill_probability BETWEEN 0 AND 1)),
            spread_bps DOUBLE PRECISION CHECK (spread_bps IS NULL OR (spread_bps < 'Infinity'::double precision AND spread_bps > '-Infinity'::double precision AND spread_bps >= 0)),
            latency_ms DOUBLE PRECISION CHECK (latency_ms IS NULL OR (latency_ms < 'Infinity'::double precision AND latency_ms > '-Infinity'::double precision AND latency_ms >= 0)),
            impact_bps DOUBLE PRECISION CHECK (impact_bps IS NULL OR (impact_bps < 'Infinity'::double precision AND impact_bps > '-Infinity'::double precision AND impact_bps >= 0)),
            input_cutoff TIMESTAMPTZ NOT NULL,
            input_hash CHAR(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$' AND input_hash <> repeat('0', 64)),
            content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$' AND content_hash <> repeat('0', 64)),
            available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            CHECK (calibration_status <> 'calibrated' OR sample_count > 0)
            ,CHECK (execution_model_snapshot_id = 'execution:' || input_hash::text)
        );

        CREATE TABLE app.paper_execution_observation (
            paper_execution_observation_id TEXT PRIMARY KEY,
            allocation_item_id TEXT NOT NULL REFERENCES analysis.portfolio_allocation_item(allocation_item_id),
            action_id TEXT NOT NULL,
            paper_order_id UUID NOT NULL REFERENCES app.paper_order(id),
            execution_mode TEXT NOT NULL DEFAULT 'paper' CHECK (execution_mode = 'paper'),
            paper_only BOOLEAN NOT NULL DEFAULT true CHECK (paper_only),
            status TEXT NOT NULL CHECK (status IN ('planned', 'submitted', 'partial', 'filled', 'exited', 'cancelled', 'unavailable')),
            requested_quantity DOUBLE PRECISION NOT NULL CHECK (requested_quantity < 'Infinity'::double precision AND requested_quantity > '-Infinity'::double precision AND requested_quantity >= 0),
            filled_quantity DOUBLE PRECISION NOT NULL CHECK (filled_quantity < 'Infinity'::double precision AND filled_quantity > '-Infinity'::double precision AND filled_quantity >= 0 AND filled_quantity <= requested_quantity),
            requested_price DOUBLE PRECISION CHECK (requested_price IS NULL OR (requested_price < 'Infinity'::double precision AND requested_price > '-Infinity'::double precision AND requested_price > 0)),
            fill_price DOUBLE PRECISION CHECK (fill_price IS NULL OR (fill_price < 'Infinity'::double precision AND fill_price > '-Infinity'::double precision AND fill_price > 0)),
            spread_bps DOUBLE PRECISION CHECK (spread_bps IS NULL OR (spread_bps < 'Infinity'::double precision AND spread_bps > '-Infinity'::double precision AND spread_bps >= 0)),
            latency_ms DOUBLE PRECISION CHECK (latency_ms IS NULL OR (latency_ms < 'Infinity'::double precision AND latency_ms > '-Infinity'::double precision AND latency_ms >= 0)),
            impact_bps DOUBLE PRECISION CHECK (impact_bps IS NULL OR (impact_bps < 'Infinity'::double precision AND impact_bps > '-Infinity'::double precision AND impact_bps >= 0)),
            side TEXT NOT NULL DEFAULT 'buy' CHECK (side IN ('buy', 'sell')),
            exit_price DOUBLE PRECISION CHECK (exit_price IS NULL OR (exit_price < 'Infinity'::double precision AND exit_price > 0)),
            observed_at TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            CHECK (status NOT IN ('filled', 'exited') OR filled_quantity > 0),
            CHECK (fill_price IS NOT NULL OR filled_quantity = 0),
            CHECK (available_at >= observed_at)
        );

        CREATE TABLE analysis.book_attribution (
            book_attribution_id TEXT PRIMARY KEY,
            allocation_id TEXT NOT NULL REFERENCES analysis.portfolio_allocation_snapshot(allocation_id),
            allocation_item_id TEXT NOT NULL REFERENCES analysis.portfolio_allocation_item(allocation_item_id),
            strategy_forecast_id TEXT NOT NULL REFERENCES analysis.strategy_forecast(id),
            hypothesis_id UUID NOT NULL REFERENCES analysis.hypothesis(id),
            action_id TEXT NOT NULL,
            rank_id TEXT NOT NULL,
            expression JSONB NOT NULL,
            experiment_id TEXT NOT NULL,
            trial_id UUID NOT NULL REFERENCES analysis.research_trial(id),
            result_id UUID NOT NULL REFERENCES analysis.trial_result(id),
            paper_execution_observation_id TEXT NOT NULL REFERENCES app.paper_execution_observation(paper_execution_observation_id),
            pnl_status TEXT NOT NULL CHECK (pnl_status IN ('pending_fill', 'realized', 'unavailable')),
            realized_pnl DOUBLE PRECISION CHECK (realized_pnl IS NULL OR (realized_pnl < 'Infinity'::double precision AND realized_pnl > '-Infinity'::double precision)),
            attribution JSONB NOT NULL,
            input_cutoff TIMESTAMPTZ NOT NULL,
            input_hash CHAR(64) NOT NULL,
            content_hash CHAR(64) NOT NULL,
            available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CHECK (jsonb_typeof(attribution) = 'object' AND jsonb_typeof(expression) = 'object' AND expression <> '{}'::jsonb),
            CHECK (pnl_status <> 'realized' OR realized_pnl IS NOT NULL)
            ,CHECK (input_hash ~ '^[0-9a-f]{64}$' AND input_hash <> repeat('0', 64) AND book_attribution_id = 'attribution:' || input_hash::text)
            ,CHECK (content_hash ~ '^[0-9a-f]{64}$' AND content_hash <> repeat('0', 64))
        );

        CREATE TABLE analysis.portfolio_drift_evidence (
            decision_id TEXT PRIMARY KEY,
            allocation_id TEXT NOT NULL REFERENCES analysis.portfolio_allocation_snapshot(allocation_id),
            allocation_item_id TEXT NOT NULL REFERENCES analysis.portfolio_allocation_item(allocation_item_id),
            drift_score DOUBLE PRECISION NOT NULL CHECK (drift_score < 'Infinity'::double precision AND drift_score > '-Infinity'::double precision AND drift_score >= 0),
            rollback_threshold DOUBLE PRECISION NOT NULL CHECK (rollback_threshold < 'Infinity'::double precision AND rollback_threshold > 0),
            proposed_weight DOUBLE PRECISION NOT NULL CHECK (proposed_weight < 'Infinity'::double precision AND proposed_weight >= 0 AND proposed_weight <= 1),
            action TEXT NOT NULL CHECK (action IN ('hold', 'reduce', 'rollback', 'unavailable')),
            input_cutoff TIMESTAMPTZ NOT NULL,
            input_hash CHAR(64) NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
            content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            CHECK (action <> 'reduce' OR (drift_score < rollback_threshold AND drift_score >= rollback_threshold / 2)),
            CHECK (action <> 'rollback' OR drift_score >= rollback_threshold),
            CHECK (action <> 'hold' OR drift_score < rollback_threshold / 2),
            CHECK (jsonb_typeof(metadata) = 'object')
            ,CHECK (decision_id = 'drift:' || input_hash::text)
        );

        CREATE OR REPLACE FUNCTION analysis.phase4_canonical_json(payload JSONB)
        RETURNS TEXT LANGUAGE plpgsql IMMUTABLE STRICT AS $$
        DECLARE kind TEXT; result TEXT;
        BEGIN
            kind := jsonb_typeof(payload);
            IF kind = 'object' THEN
                SELECT COALESCE('{' || string_agg(to_json(key)::text || ': ' || analysis.phase4_canonical_json(value), ', ' ORDER BY length(key), key) || '}', '{}')
                  INTO result FROM jsonb_each(payload);
            ELSIF kind = 'array' THEN
                SELECT COALESCE('[' || string_agg(analysis.phase4_canonical_json(value), ', ' ORDER BY ordinality) || ']', '[]')
                  INTO result FROM jsonb_array_elements(payload) WITH ORDINALITY;
            ELSE
                result := payload::text;
            END IF;
            RETURN result;
        END;
        $$;

        CREATE OR REPLACE FUNCTION analysis.phase4_canonical_timestamp(value TIMESTAMPTZ)
        RETURNS JSONB LANGUAGE SQL IMMUTABLE STRICT AS $$
            SELECT to_jsonb(value AT TIME ZONE 'UTC')
        $$;

        CREATE OR REPLACE FUNCTION analysis.phase4_content_digest(payload JSONB)
        RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT AS $$
            SELECT encode(public.digest(convert_to(analysis.phase4_canonical_json(payload), 'UTF8'), 'sha256'), 'hex')
        $$;

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_lineage()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE expected_cutoff TIMESTAMPTZ; expected_allocation TEXT;
                expected_forecast TEXT; expected_hypothesis UUID;
                expected_action TEXT; expected_rank TEXT; expected_expression JSONB;
                expected_experiment TEXT; expected_trial UUID; expected_result UUID;
                scenario JSONB; probability_total DOUBLE PRECISION := 0;
                expected_realized_pnl DOUBLE PRECISION;
        BEGIN
            IF TG_TABLE_NAME = 'portfolio_allocation_snapshot' THEN
                NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
                    'allocation_id', NEW.allocation_id, 'as_of', analysis.phase4_canonical_timestamp(NEW.as_of),
                    'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff), 'status', NEW.status,
                    'cash_hurdle', NEW.cash_hurdle, 'forecast_ids', NEW.forecast_ids,
                    'action_ids', NEW.action_ids, 'strategy_registry_ids', NEW.strategy_registry_ids,
                    'metadata', NEW.metadata
                ));
            ELSIF TG_TABLE_NAME = 'portfolio_allocation_item' THEN
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
            ELSIF TG_TABLE_NAME = 'probabilistic_portfolio_scenario_artifact' THEN
                NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
                    'scenario_artifact_id', NEW.scenario_artifact_id, 'allocation_id', NEW.allocation_id,
                    'model_version', NEW.model_version, 'probability_semantics', NEW.probability_semantics,
                    'scenarios', NEW.scenarios, 'tail_dependence', NEW.tail_dependence,
                    'simultaneous_unwind', NEW.simultaneous_unwind, 'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff)
                ));
            ELSIF TG_TABLE_NAME = 'execution_model_snapshot' THEN
                NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
                    'execution_model_snapshot_id', NEW.execution_model_snapshot_id, 'allocation_id', NEW.allocation_id,
                    'model_version', NEW.model_version, 'calibration_status', NEW.calibration_status,
                    'sample_count', NEW.sample_count, 'fill_probability', NEW.fill_probability,
                    'spread_bps', NEW.spread_bps, 'latency_ms', NEW.latency_ms, 'impact_bps', NEW.impact_bps,
                    'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff), 'metadata', NEW.metadata
                ));
            ELSIF TG_TABLE_NAME = 'book_attribution' THEN
                NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
                    'book_attribution_id', NEW.book_attribution_id, 'allocation_id', NEW.allocation_id,
                    'allocation_item_id', NEW.allocation_item_id, 'strategy_forecast_id', NEW.strategy_forecast_id,
                    'hypothesis_id', NEW.hypothesis_id, 'action_id', NEW.action_id, 'rank_id', NEW.rank_id,
                    'expression', NEW.expression, 'experiment_id', NEW.experiment_id,
                    'trial_id', NEW.trial_id, 'result_id', NEW.result_id,
                    'paper_execution_observation_id', NEW.paper_execution_observation_id,
                    'pnl_status', NEW.pnl_status, 'realized_pnl', NEW.realized_pnl,
                    'attribution', NEW.attribution, 'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff)
                ));
            ELSIF TG_TABLE_NAME = 'portfolio_drift_evidence' THEN
                NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
                    'decision_id', NEW.decision_id, 'allocation_id', NEW.allocation_id,
                    'allocation_item_id', NEW.allocation_item_id, 'drift_score', NEW.drift_score,
                    'rollback_threshold', NEW.rollback_threshold, 'proposed_weight', NEW.proposed_weight,
                    'action', NEW.action, 'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff), 'metadata', NEW.metadata
                ));
            END IF;
            IF TG_TABLE_NAME = 'portfolio_allocation_item' THEN
                IF NEW.disposition = 'selected' AND NEW.ticker <> 'CASH' THEN
                    IF NEW.funding_amount IS NULL OR NEW.funding_amount <= 0 THEN
                        RAISE EXCEPTION 'Phase 4 funded item requires a positive funding amount';
                    ELSIF NEW.funding_source LIKE 'CASH:broker-account:%' AND NOT EXISTS (
                        SELECT 1 FROM raw.broker_account_snapshot account
                        WHERE account.id = split_part(NEW.funding_source, ':', 4)::BIGINT
                          AND account.cash_balance >= NEW.funding_amount
                          AND account.observed_at <= (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id)
                    ) THEN
                        RAISE EXCEPTION 'Phase 4 cash funding source is not an actual PostgreSQL account snapshot';
                    ELSIF NEW.funding_source LIKE 'TRIM:broker-position:%' AND NOT EXISTS (
                        SELECT 1 FROM raw.broker_position_snapshot position
                        WHERE position.id = split_part(NEW.funding_source, ':', 3)::BIGINT
                          AND position.quantity > 0
                          AND abs(coalesce(position.market_value, 0)) >= NEW.funding_amount
                    ) THEN
                        RAISE EXCEPTION 'Phase 4 trim funding source is not an actual PostgreSQL position';
                    ELSIF NEW.funding_source NOT LIKE 'CASH:broker-account:%'
                          AND NEW.funding_source NOT LIKE 'TRIM:broker-position:%' THEN
                        RAISE EXCEPTION 'Phase 4 funded item has no authoritative funding source';
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
                        RAISE EXCEPTION 'Phase 4 allocation forecast or PIT lineage is invalid';
                    END IF;
                    IF NEW.action_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM analysis.ticker_decision decision
                        WHERE decision.input_manifest->'trade_plan'->>'trade_plan_id' = NEW.action_id
                          AND decision.input_manifest->'trade_plan'->>'rank_id' = NEW.rank_id
                          AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id
                          AND decision.status = 'published'
                          AND decision.published_at IS NOT NULL
                          AND decision.as_of <= (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id)
                          AND decision.published_at <= (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id)
                    ) THEN
                        RAISE EXCEPTION 'Phase 4 allocation action or rank lineage is invalid';
                    END IF;
                END IF;
            ELSIF TG_TABLE_NAME = 'probabilistic_portfolio_scenario_artifact' THEN
                IF jsonb_array_length(NEW.scenarios) = 0 OR jsonb_array_length(NEW.scenarios) > 64 THEN
                    RAISE EXCEPTION 'Phase 4 scenario paths must be bounded and non-empty';
                END IF;
                FOR scenario IN SELECT value FROM jsonb_array_elements(NEW.scenarios) LOOP
                    IF jsonb_typeof(scenario->'probability') IS DISTINCT FROM 'number'
                       OR jsonb_typeof(scenario->'returns') IS DISTINCT FROM 'object'
                       OR scenario->'returns' = '{}'::jsonb
                       OR jsonb_typeof(scenario->'shocks') IS DISTINCT FROM 'object'
                       OR scenario->'shocks' = '{}'::jsonb
                       OR scenario->'returns' = scenario->'shocks' THEN
                        RAISE EXCEPTION 'Phase 4 scenario path requires probability, returns, and shocks';
                    END IF;
                    IF jsonb_typeof(scenario->'provenance') IS DISTINCT FROM 'array'
                       OR jsonb_array_length(scenario->'provenance') = 0
                       OR EXISTS (
                           SELECT 1 FROM jsonb_array_elements(scenario->'provenance') source
                           WHERE jsonb_typeof(source->'strategy_pnl_tape_id') IS DISTINCT FROM 'string'
                              OR jsonb_typeof(source->'pnl_date') IS DISTINCT FROM 'string'
                              OR jsonb_typeof(source->'input_cutoff') IS DISTINCT FROM 'string'
                              OR jsonb_typeof(source->'available_at') IS DISTINCT FROM 'string'
                              OR jsonb_typeof(source->'input_hash') IS DISTINCT FROM 'string'
                       ) THEN
                        RAISE EXCEPTION 'Phase 4 scenario path requires complete persisted tape provenance';
                    END IF;
                    probability_total := probability_total + (scenario->>'probability')::DOUBLE PRECISION;
                END LOOP;
                IF EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(NEW.scenarios) path
                    CROSS JOIN LATERAL jsonb_array_elements(path->'provenance') source
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM analysis.strategy_pnl_tape tape
                        JOIN analysis.portfolio_allocation_item item
                          ON item.allocation_id = NEW.allocation_id
                         AND item.strategy_forecast_id = tape.strategy_forecast_id
                         AND item.disposition = 'selected'
                        WHERE tape.id::text = source->>'strategy_pnl_tape_id'
                          AND tape.input_hash::text = source->>'input_hash'
                          AND tape.input_cutoff <= NEW.input_cutoff
                          AND tape.available_at <= NEW.input_cutoff
                    )
                ) THEN
                    RAISE EXCEPTION 'Phase 4 scenario provenance is not bound to a persisted tape row';
                END IF;
                IF probability_total < 0.999999 OR probability_total > 1.000001 THEN
                    RAISE EXCEPTION 'Phase 4 scenario probabilities must sum to one';
                END IF;
                IF NOT (NEW.tail_dependence ? 'negative_return_co_exceedance' OR NEW.tail_dependence ? 'co_exceedance') THEN
                    RAISE EXCEPTION 'Phase 4 scenario artifact requires persisted tail co-exceedance results';
                END IF;
                IF NOT (NEW.simultaneous_unwind ? 'probability' AND NEW.simultaneous_unwind ? 'observations') THEN
                    RAISE EXCEPTION 'Phase 4 scenario artifact requires simultaneous-unwind results';
                END IF;
                SELECT input_cutoff INTO expected_cutoff
                FROM analysis.portfolio_allocation_snapshot
                WHERE allocation_id = NEW.allocation_id;
                IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN
                    RAISE EXCEPTION 'Phase 4 scenario input cutoff does not match allocation lineage';
                END IF;
            ELSIF TG_TABLE_NAME = 'execution_model_snapshot' THEN
                IF NEW.allocation_id IS NULL THEN
                    RAISE EXCEPTION 'Phase 4 execution snapshot requires allocation lineage';
                END IF;
                SELECT input_cutoff INTO expected_cutoff
                FROM analysis.portfolio_allocation_snapshot
                WHERE allocation_id = NEW.allocation_id;
                IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN
                    RAISE EXCEPTION 'Phase 4 execution input cutoff does not match allocation lineage';
                END IF;
                IF NEW.sample_count > 0 AND (
                    jsonb_typeof(NEW.metadata->'paper_observation_ids') IS DISTINCT FROM 'array'
                    OR jsonb_array_length(NEW.metadata->'paper_observation_ids') <> NEW.sample_count
                    OR EXISTS (
                        SELECT 1
                        FROM jsonb_array_elements_text(NEW.metadata->'paper_observation_ids') observation_id
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM app.paper_execution_observation observation
                            JOIN app.paper_order paper ON paper.id = observation.paper_order_id
                            WHERE observation.paper_execution_observation_id = observation_id
                              AND observation.allocation_item_id IN (
                                  SELECT allocation_item_id FROM analysis.portfolio_allocation_item
                                  WHERE allocation_id = NEW.allocation_id
                              )
                              AND observation.paper_only
                              AND observation.execution_mode = 'paper'
                              AND observation.filled_quantity > 0
                              AND observation.fill_price IS NOT NULL
                              AND paper.status IN ('open', 'entered', 'partial_exited', 'exited', 'closed', 'invalidated')
                        )
                    )
                ) THEN
                    RAISE EXCEPTION 'Phase 4 execution snapshot is not bound to genuine paper fills';
                END IF;
            ELSIF TG_TABLE_NAME = 'book_attribution' THEN
                SELECT item.allocation_id, item.strategy_forecast_id, item.hypothesis_id,
                       item.action_id, item.rank_id, item.trace->'expression',
                       forecast.research_trial_id, forecast.trial_result_id
                  INTO expected_allocation, expected_forecast, expected_hypothesis,
                       expected_action, expected_rank, expected_expression,
                       expected_trial, expected_result
                FROM analysis.portfolio_allocation_item item
                JOIN analysis.strategy_forecast forecast ON forecast.id = item.strategy_forecast_id
                WHERE item.allocation_item_id = NEW.allocation_item_id;
                IF expected_allocation IS NULL OR NEW.allocation_id IS DISTINCT FROM expected_allocation THEN
                    RAISE EXCEPTION 'Phase 4 attribution item does not match allocation lineage';
                END IF;
                IF NEW.pnl_status = 'realized' AND (NEW.paper_execution_observation_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM app.paper_execution_observation observation
                    JOIN app.paper_order paper ON paper.id = observation.paper_order_id
                    WHERE observation.paper_execution_observation_id = NEW.paper_execution_observation_id
                      AND observation.allocation_item_id = NEW.allocation_item_id
                      AND observation.paper_only AND observation.execution_mode = 'paper'
                      AND observation.filled_quantity > 0 AND observation.fill_price IS NOT NULL
                      AND observation.exit_price IS NOT NULL AND paper.status IN ('exited', 'closed')
                )) THEN
                    RAISE EXCEPTION 'Phase 4 realized attribution requires a genuine linked paper fill';
                END IF;
                IF NEW.strategy_forecast_id IS DISTINCT FROM expected_forecast
                   OR NEW.hypothesis_id IS DISTINCT FROM expected_hypothesis
                   OR NEW.action_id IS DISTINCT FROM expected_action
                   OR NEW.rank_id IS DISTINCT FROM expected_rank
                   OR NEW.expression IS DISTINCT FROM expected_expression
                   OR NEW.trial_id IS DISTINCT FROM expected_trial
                   OR NEW.result_id IS DISTINCT FROM expected_result THEN
                    RAISE EXCEPTION 'Phase 4 attribution does not match allocation lineage';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM analysis.ticker_decision decision
                    WHERE decision.input_manifest->'trade_plan'->>'trade_plan_id' = (
                              SELECT action_id FROM analysis.portfolio_allocation_item
                              WHERE allocation_item_id = NEW.allocation_item_id)
                      AND decision.input_manifest->'trade_plan'->>'rank_id' = (
                              SELECT rank_id FROM analysis.portfolio_allocation_item
                              WHERE allocation_item_id = NEW.allocation_item_id)
                      AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id
                      AND decision.status = 'published' AND decision.published_at IS NOT NULL
                ) THEN
                    RAISE EXCEPTION 'Phase 4 attribution requires a published action and rank lineage';
                END IF;
                SELECT decision.experiment_id INTO expected_experiment
                FROM analysis.ticker_decision decision
                WHERE decision.input_manifest->'trade_plan'->>'trade_plan_id' = NEW.action_id
                  AND decision.input_manifest->'trade_plan'->>'rank_id' = NEW.rank_id
                  AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id
                  AND decision.status = 'published' AND decision.published_at IS NOT NULL
                ORDER BY decision.published_at DESC, decision.id DESC LIMIT 1;
                IF expected_experiment IS NULL OR NEW.experiment_id IS DISTINCT FROM expected_experiment THEN
                    RAISE EXCEPTION 'Phase 4 attribution experiment lineage is invalid';
                END IF;
                SELECT input_cutoff INTO expected_cutoff
                FROM analysis.portfolio_allocation_snapshot
                WHERE allocation_id = NEW.allocation_id;
                IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN
                    RAISE EXCEPTION 'Phase 4 attribution input cutoff does not match allocation lineage';
                END IF;
                IF NEW.pnl_status = 'realized' THEN
                    SELECT CASE WHEN observation.side = 'buy' THEN 1 ELSE -1 END
                             * (observation.exit_price - observation.fill_price)
                             * observation.filled_quantity
                             - coalesce(paper.fees, 0)
                      INTO expected_realized_pnl
                    FROM app.paper_execution_observation observation
                    JOIN app.paper_order paper ON paper.id = observation.paper_order_id
                    WHERE observation.paper_execution_observation_id = NEW.paper_execution_observation_id;
                    IF expected_realized_pnl IS NULL
                       OR abs(NEW.realized_pnl - expected_realized_pnl) > 0.000000001 THEN
                        RAISE EXCEPTION 'Phase 4 attribution P&L does not match the persisted fill and fee lineage';
                    END IF;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_paper_execution()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE order_created_at TIMESTAMPTZ; order_status TEXT;
                order_filled_quantity DOUBLE PRECISION; order_fill_price DOUBLE PRECISION;
                order_filled_at TIMESTAMPTZ; order_exit_at TIMESTAMPTZ;
        BEGIN
            SELECT created_at, status, coalesce(filled_quantity, 0)::DOUBLE PRECISION,
                   actual_fill_price::DOUBLE PRECISION, filled_at, exit_at
              INTO order_created_at, order_status, order_filled_quantity,
                   order_fill_price, order_filled_at, order_exit_at
            FROM app.paper_order WHERE id = NEW.paper_order_id;
            IF order_created_at IS NULL
               OR NEW.observed_at < order_created_at
               OR NEW.available_at < NEW.observed_at THEN
                RAISE EXCEPTION 'Phase 4 paper observation has invalid order clock lineage';
            END IF;
            IF NEW.filled_quantity > 0
               AND (order_status NOT IN ('open', 'entered', 'partial_exited', 'exited', 'invalidated', 'closed')
                    OR order_filled_quantity < NEW.filled_quantity
                    OR order_fill_price IS NULL OR order_filled_at IS NULL
                    OR NEW.observed_at < order_filled_at) THEN
                RAISE EXCEPTION 'Phase 4 paper observation requires a genuine paper fill';
            END IF;
            IF NEW.status = 'exited' AND (order_exit_at IS NULL OR NEW.observed_at < order_exit_at) THEN
                RAISE EXCEPTION 'Phase 4 exit observation requires an exited paper order';
            END IF;
            IF NEW.allocation_item_id IS NULL OR NEW.action_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM analysis.portfolio_allocation_item item
                WHERE item.allocation_item_id = NEW.allocation_item_id
                  AND item.action_id = NEW.action_id
                  AND EXISTS (
                      SELECT 1 FROM app.paper_order paper
                      WHERE paper.id = NEW.paper_order_id
                        AND paper.policy_result->>'trade_plan_id' = NEW.action_id
                  )
            ) THEN
                RAISE EXCEPTION 'Phase 4 paper observation action lineage is invalid';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER portfolio_scenario_lineage
            BEFORE INSERT ON analysis.probabilistic_portfolio_scenario_artifact
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();
        CREATE TRIGGER portfolio_allocation_snapshot_lineage
            BEFORE INSERT ON analysis.portfolio_allocation_snapshot
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();
        CREATE TRIGGER portfolio_allocation_item_lineage
            BEFORE INSERT ON analysis.portfolio_allocation_item
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();
        CREATE TRIGGER execution_model_lineage
            BEFORE INSERT ON analysis.execution_model_snapshot
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();
        CREATE TRIGGER book_attribution_lineage
            BEFORE INSERT ON analysis.book_attribution
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();
        CREATE TRIGGER portfolio_drift_lineage
            BEFORE INSERT ON analysis.portfolio_drift_evidence
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();
        CREATE TRIGGER paper_execution_lineage
            BEFORE INSERT ON app.paper_execution_observation
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_paper_execution();

        CREATE OR REPLACE FUNCTION analysis.insert_phase4_scenario(
            p_id TEXT, p_allocation_id TEXT, p_model_version TEXT,
            p_probability_semantics TEXT, p_scenarios JSONB,
            p_tail_dependence JSONB, p_simultaneous_unwind JSONB,
            p_input_cutoff TIMESTAMPTZ, p_input_hash TEXT, p_content_hash TEXT
        ) RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = analysis, app, public AS $$
        BEGIN
            INSERT INTO analysis.probabilistic_portfolio_scenario_artifact
                (scenario_artifact_id, allocation_id, model_version, probability_semantics,
                 scenarios, tail_dependence, simultaneous_unwind, input_cutoff, input_hash, content_hash)
            VALUES (p_id, p_allocation_id, p_model_version, p_probability_semantics,
                    p_scenarios, p_tail_dependence, p_simultaneous_unwind,
                    p_input_cutoff, p_input_hash, p_content_hash)
            ON CONFLICT (scenario_artifact_id) DO NOTHING;
        END;
        $$;
        CREATE OR REPLACE FUNCTION analysis.insert_phase4_execution(
            p_id TEXT, p_allocation_id TEXT, p_model_version TEXT,
            p_calibration_status TEXT, p_sample_count INTEGER,
            p_fill_probability DOUBLE PRECISION, p_spread_bps DOUBLE PRECISION,
            p_latency_ms DOUBLE PRECISION, p_impact_bps DOUBLE PRECISION,
            p_input_cutoff TIMESTAMPTZ, p_input_hash TEXT, p_content_hash TEXT,
            p_metadata JSONB
        ) RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = analysis, app, public AS $$
        BEGIN
            INSERT INTO analysis.execution_model_snapshot
                (execution_model_snapshot_id, allocation_id, model_version, calibration_status,
                 sample_count, fill_probability, spread_bps, latency_ms, impact_bps,
                 input_cutoff, input_hash, content_hash, metadata)
            VALUES (p_id, p_allocation_id, p_model_version, p_calibration_status,
                    p_sample_count, p_fill_probability, p_spread_bps, p_latency_ms, p_impact_bps,
                    p_input_cutoff, p_input_hash, p_content_hash, p_metadata)
            ON CONFLICT (execution_model_snapshot_id) DO NOTHING;
        END;
        $$;

        CREATE OR REPLACE FUNCTION analysis.reject_phase4_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Phase 4 artifacts are immutable';
        END;
        $$;
        CREATE TRIGGER portfolio_allocation_snapshot_immutable
            BEFORE UPDATE OR DELETE ON analysis.portfolio_allocation_snapshot
            FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();
        CREATE TRIGGER portfolio_allocation_item_immutable
            BEFORE UPDATE OR DELETE ON analysis.portfolio_allocation_item
            FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();
        CREATE TRIGGER portfolio_scenario_artifact_immutable
            BEFORE UPDATE OR DELETE ON analysis.probabilistic_portfolio_scenario_artifact
            FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();
        CREATE TRIGGER execution_model_snapshot_immutable
            BEFORE UPDATE OR DELETE ON analysis.execution_model_snapshot
            FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();
        CREATE TRIGGER book_attribution_immutable
            BEFORE UPDATE OR DELETE ON analysis.book_attribution
            FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();
        CREATE TRIGGER paper_execution_observation_immutable
            BEFORE UPDATE OR DELETE ON app.paper_execution_observation
            FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();
        CREATE TRIGGER portfolio_drift_evidence_immutable
            BEFORE UPDATE OR DELETE ON analysis.portfolio_drift_evidence
            FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

        GRANT SELECT, INSERT ON analysis.portfolio_allocation_snapshot,
            analysis.portfolio_allocation_item,
            analysis.book_attribution,
            analysis.portfolio_drift_evidence TO market_app;
        GRANT SELECT, INSERT ON app.paper_execution_observation TO market_app;
        GRANT SELECT ON analysis.probabilistic_portfolio_scenario_artifact,
            analysis.execution_model_snapshot TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.insert_phase4_scenario(TEXT, TEXT, TEXT, TEXT, JSONB, JSONB, JSONB, TIMESTAMPTZ, TEXT, TEXT),
            analysis.insert_phase4_execution(TEXT, TEXT, TEXT, TEXT, INTEGER, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, TIMESTAMPTZ, TEXT, TEXT, JSONB)
            TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE SELECT, INSERT ON analysis.portfolio_allocation_snapshot,
            analysis.portfolio_allocation_item,
            analysis.probabilistic_portfolio_scenario_artifact,
            analysis.execution_model_snapshot,
            analysis.book_attribution,
            analysis.portfolio_drift_evidence FROM market_app;
        REVOKE SELECT, INSERT ON app.paper_execution_observation FROM market_app;
        REVOKE EXECUTE ON FUNCTION analysis.insert_phase4_scenario(TEXT, TEXT, TEXT, TEXT, JSONB, JSONB, JSONB, TIMESTAMPTZ, TEXT, TEXT),
            analysis.insert_phase4_execution(TEXT, TEXT, TEXT, TEXT, INTEGER, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, TIMESTAMPTZ, TEXT, TEXT, JSONB)
            FROM market_app;
        DROP TRIGGER IF EXISTS paper_execution_observation_immutable ON app.paper_execution_observation;
        DROP TRIGGER IF EXISTS paper_execution_lineage ON app.paper_execution_observation;
        DROP TRIGGER IF EXISTS book_attribution_lineage ON analysis.book_attribution;
        DROP TRIGGER IF EXISTS portfolio_drift_lineage ON analysis.portfolio_drift_evidence;
        DROP TRIGGER IF EXISTS execution_model_lineage ON analysis.execution_model_snapshot;
        DROP TRIGGER IF EXISTS portfolio_scenario_lineage ON analysis.probabilistic_portfolio_scenario_artifact;
        DROP TRIGGER IF EXISTS portfolio_allocation_snapshot_lineage ON analysis.portfolio_allocation_snapshot;
        DROP TRIGGER IF EXISTS portfolio_allocation_item_lineage ON analysis.portfolio_allocation_item;
        DROP TRIGGER IF EXISTS book_attribution_immutable ON analysis.book_attribution;
        DROP TRIGGER IF EXISTS execution_model_snapshot_immutable ON analysis.execution_model_snapshot;
        DROP TRIGGER IF EXISTS portfolio_scenario_artifact_immutable ON analysis.probabilistic_portfolio_scenario_artifact;
        DROP TRIGGER IF EXISTS portfolio_allocation_item_immutable ON analysis.portfolio_allocation_item;
        DROP TRIGGER IF EXISTS portfolio_allocation_snapshot_immutable ON analysis.portfolio_allocation_snapshot;
        DROP TRIGGER IF EXISTS portfolio_drift_evidence_immutable ON analysis.portfolio_drift_evidence;
        DROP FUNCTION IF EXISTS analysis.reject_phase4_update();
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_paper_execution();
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_lineage();
        DROP FUNCTION IF EXISTS analysis.insert_phase4_scenario(TEXT, TEXT, TEXT, TEXT, JSONB, JSONB, JSONB, TIMESTAMPTZ, TEXT, TEXT);
        DROP FUNCTION IF EXISTS analysis.insert_phase4_execution(TEXT, TEXT, TEXT, TEXT, INTEGER, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, TIMESTAMPTZ, TEXT, TEXT, JSONB);
        DROP FUNCTION IF EXISTS analysis.phase4_content_digest(JSONB);
        DROP FUNCTION IF EXISTS analysis.phase4_canonical_timestamp(TIMESTAMPTZ);
        DROP FUNCTION IF EXISTS analysis.phase4_canonical_json(JSONB);
        DROP TABLE IF EXISTS analysis.book_attribution;
        DROP TABLE IF EXISTS analysis.portfolio_drift_evidence;
        DROP TABLE IF EXISTS app.paper_execution_observation;
        DROP TABLE IF EXISTS analysis.execution_model_snapshot;
        DROP TABLE IF EXISTS analysis.probabilistic_portfolio_scenario_artifact;
        DROP TABLE IF EXISTS analysis.portfolio_allocation_item;
        DROP TABLE IF EXISTS analysis.portfolio_allocation_snapshot;
        """
    )
