"""Make Phase 4 source funding and execution calibration database-authoritative."""

from __future__ import annotations

from alembic import op


revision = "20260904_0077"
down_revision = "20260904_0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        DO $$
        DECLARE constraint_row RECORD;
        BEGIN
          FOR constraint_row IN
            SELECT conname
              FROM pg_constraint
             WHERE conrelid = 'app.paper_execution_observation'::regclass
               AND pg_get_constraintdef(oid) LIKE '%planned%'
          LOOP
            EXECUTE format('ALTER TABLE app.paper_execution_observation DROP CONSTRAINT %I', constraint_row.conname);
          END LOOP;
        END $$;
        ALTER TABLE app.paper_execution_observation
          ADD CONSTRAINT phase4_paper_observation_status
          CHECK (status IN ('planned', 'submitted', 'partial', 'filled', 'partial_exited', 'exited', 'cancelled', 'unavailable'));

        DO $$
        DECLARE constraint_row RECORD;
        BEGIN
          FOR constraint_row IN
            SELECT conname
              FROM pg_constraint
             WHERE conrelid = 'analysis.portfolio_allocation_item'::regclass
               AND (pg_get_constraintdef(oid) LIKE '%funding_source%'
                    OR conname IN ('phase4_allocation_item_funding_sources_shape',
                                   'phase4_allocation_item_funding_sources_required'))
          LOOP
            EXECUTE format('ALTER TABLE analysis.portfolio_allocation_item DROP CONSTRAINT %I', constraint_row.conname);
          END LOOP;
        END $$;
        ALTER TABLE analysis.portfolio_allocation_item
          ADD CONSTRAINT phase4_allocation_item_funding_sources_shape
          CHECK (jsonb_typeof(funding_sources) = 'object'),
          ADD CONSTRAINT phase4_allocation_item_funding_amount_shape
          CHECK (funding_amount IS NULL OR (funding_amount < 'Infinity'::double precision
                                            AND funding_amount > '-Infinity'::double precision
                                            AND funding_amount > 0));

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_review_item_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          expected_cutoff TIMESTAMPTZ;
          expected_forecast TEXT;
          expected_hypothesis UUID;
          source_key TEXT;
          source_value JSONB;
          source_amount DOUBLE PRECISION;
          source_total DOUBLE PRECISION := 0;
          source_count INTEGER := 0;
          authority_account BIGINT;
        BEGIN
          SELECT input_cutoff INTO expected_cutoff
            FROM analysis.portfolio_allocation_snapshot
           WHERE allocation_id = NEW.allocation_id;
          IF expected_cutoff IS NULL THEN
            RAISE EXCEPTION 'Phase 4 allocation item has no persisted allocation cutoff';
          END IF;

          IF NEW.funding_sources IS NULL OR jsonb_typeof(NEW.funding_sources) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Phase 4 funding sources must be an object';
          END IF;
          FOR source_key, source_value IN SELECT key, value FROM jsonb_each(NEW.funding_sources) LOOP
            source_count := source_count + 1;
            IF source_key !~ '^(CASH:broker-account:[0-9]+|TRIM:broker-position:[0-9]+)$'
               OR jsonb_typeof(source_value) IS DISTINCT FROM 'number' THEN
              RAISE EXCEPTION 'Phase 4 funding source is not a canonical PostgreSQL source: %', source_key;
            END IF;
            source_amount := (source_value #>> '{}')::DOUBLE PRECISION;
            IF source_amount IS NULL OR source_amount <= 0
               OR source_amount = 'Infinity'::DOUBLE PRECISION
               OR source_amount = '-Infinity'::DOUBLE PRECISION THEN
              RAISE EXCEPTION 'Phase 4 funding source amount is invalid: %', source_key;
            END IF;
            source_total := source_total + source_amount;
            IF source_key LIKE 'CASH:%' THEN
              IF NOT EXISTS (
                SELECT 1
                  FROM raw.broker_account_snapshot account
                 WHERE account.id = split_part(source_key, ':', 3)::BIGINT
                   AND account.observed_at <= expected_cutoff
                   AND ('broker-account:' || account.id::TEXT) =
                       (SELECT metadata->>'authority_snapshot_id'
                          FROM analysis.portfolio_allocation_snapshot
                         WHERE allocation_id = NEW.allocation_id)
              ) THEN
                RAISE EXCEPTION 'Phase 4 cash funding source is not the allocation authority account: %', source_key;
              END IF;
            ELSIF NOT EXISTS (
              SELECT 1
                FROM raw.broker_position_snapshot position
                JOIN raw.broker_account_snapshot account
                  ON account.id = position.account_snapshot_id
               WHERE position.id = split_part(source_key, ':', 3)::BIGINT
                 AND position.quantity > 0
                 AND position.market_value IS NOT NULL
                 AND account.observed_at <= expected_cutoff
                 AND ('broker-account:' || account.id::TEXT) =
                     (SELECT metadata->>'authority_snapshot_id'
                        FROM analysis.portfolio_allocation_snapshot
                       WHERE allocation_id = NEW.allocation_id)
            ) THEN
              RAISE EXCEPTION 'Phase 4 trim funding source is not a persisted position: %', source_key;
            END IF;
          END LOOP;

          IF source_count > 0 AND (NEW.funding_amount IS NULL
                                    OR abs(source_total - NEW.funding_amount) > 0.000000001) THEN
            RAISE EXCEPTION 'Phase 4 funding sources do not conserve funding_amount';
          END IF;
          IF NEW.funding_amount IS NOT NULL AND source_count = 0
             AND NEW.disposition IN ('selected', 'rollback')
             AND NEW.ticker <> 'CASH' AND NEW.target_weight <> NEW.current_weight THEN
            RAISE EXCEPTION 'Phase 4 funded item must name every source';
          END IF;
          IF source_count = 1 AND NEW.funding_source IS DISTINCT FROM (
            SELECT min(source.key) FROM jsonb_object_keys(NEW.funding_sources) AS source(key)
          ) THEN
            RAISE EXCEPTION 'Phase 4 single-source display does not match its source map';
          END IF;
          IF source_count > 1 AND NEW.funding_source IS DISTINCT FROM 'MULTI_SOURCE' THEN
            RAISE EXCEPTION 'Phase 4 multi-source funding must use display marker MULTI_SOURCE';
          END IF;

          IF NEW.disposition = 'selected' AND NEW.ticker <> 'CASH'
             AND NEW.target_weight > NEW.current_weight
             AND (NEW.funding_amount IS NULL OR NEW.funding_amount <= 0 OR source_count = 0) THEN
            RAISE EXCEPTION 'Phase 4 increase requires conserved PostgreSQL funding sources';
          END IF;

          IF NEW.disposition IN ('selected', 'rollback') AND NEW.target_weight < NEW.current_weight THEN
            IF source_count = 0 OR source_count <> 1 OR NEW.funding_source IS NULL
               OR NEW.funding_source !~ '^TRIM:broker-position:[0-9]+$'
               OR NEW.trace->>'trim_position_id' IS DISTINCT FROM split_part(NEW.funding_source, ':', 2) || ':' || split_part(NEW.funding_source, ':', 3) THEN
              RAISE EXCEPTION 'Phase 4 trim release is not bound to one persisted position';
            END IF;
          END IF;

          NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
            'allocation_item_id', NEW.allocation_item_id, 'allocation_id', NEW.allocation_id,
            'candidate_id', NEW.candidate_id, 'ticker', NEW.ticker,
            'strategy_forecast_id', NEW.strategy_forecast_id, 'action_id', NEW.action_id,
            'rank_id', NEW.rank_id, 'hypothesis_id', NEW.hypothesis_id,
            'disposition', NEW.disposition, 'target_weight', NEW.target_weight,
            'current_weight', NEW.current_weight, 'marginal_book_utility', NEW.marginal_book_utility,
            'trace', NEW.trace, 'blockers', NEW.blockers, 'funding_source', NEW.funding_source,
            'funding_amount', NEW.funding_amount, 'funding_sources', NEW.funding_sources));

          IF NEW.strategy_forecast_id IS NOT NULL THEN
            SELECT forecast.input_cutoff, forecast.id, revision.hypothesis_id
              INTO expected_cutoff, expected_forecast, expected_hypothesis
              FROM analysis.strategy_forecast forecast
              JOIN analysis.strategy_revision revision ON revision.id = forecast.strategy_revision_id
             WHERE forecast.id = NEW.strategy_forecast_id;
            IF expected_forecast IS NULL
               OR expected_cutoff > (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id)
               OR NEW.hypothesis_id IS DISTINCT FROM expected_hypothesis THEN
              RAISE EXCEPTION 'Phase 4 allocation forecast lineage is invalid';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$;

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_allocation_item_funding_lineage()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          allocation_meta JSONB;
          allocation_cutoff TIMESTAMPTZ;
        BEGIN
          SELECT snapshot.metadata, snapshot.input_cutoff
            INTO allocation_meta, allocation_cutoff
            FROM analysis.portfolio_allocation_snapshot snapshot
           WHERE snapshot.allocation_id = NEW.allocation_id;
          IF NEW.disposition IN ('selected', 'rollback') AND NEW.ticker <> 'CASH' THEN
            IF allocation_meta IS NULL
               OR allocation_meta->>'authority' <> 'postgresql'
               OR allocation_meta->>'execution_status' <> 'calibrated'
               OR NEW.strategy_forecast_id IS NULL OR NEW.action_id IS NULL OR NEW.rank_id IS NULL
               OR NEW.trace->>'source_decision_id' IS NULL
               OR NEW.trace->>'source_decision_input_hash' IS NULL
               OR NOT EXISTS (
                 SELECT 1 FROM raw.broker_account_snapshot account
                  WHERE ('broker-account:' || account.id::TEXT) = allocation_meta->>'authority_snapshot_id'
                    AND account.observed_at <= allocation_cutoff
               )
               OR NOT EXISTS (
                 SELECT 1
                   FROM analysis.execution_model_snapshot model
                   JOIN analysis.portfolio_allocation_snapshot model_allocation
                     ON model_allocation.allocation_id = model.allocation_id
                  WHERE model.execution_model_snapshot_id = allocation_meta->>'execution_model_snapshot_id'
                    AND model.model_version = 'paper-telemetry.v1'
                    AND model.calibration_status = 'calibrated'
                    AND model.sample_count > 0
                    AND model.input_cutoff > model_allocation.input_cutoff
                    AND model.input_cutoff <= allocation_cutoff
                    AND model.available_at <= allocation_cutoff
                    AND model.metadata->>'source' = 'paper_execution_observation'
                    AND jsonb_typeof(model.metadata->'paper_observation_ids') = 'array'
                    AND jsonb_array_length(model.metadata->'paper_observation_ids') = model.sample_count
                    AND CASE WHEN model.metadata->>'genuine_fill_count' ~ '^[0-9]+$'
                             THEN (model.metadata->>'genuine_fill_count')::INTEGER = model.sample_count
                             ELSE false END
               )
               OR NOT EXISTS (
                 SELECT 1
                   FROM analysis.ticker_decision decision
                   JOIN analysis.strategy_forecast forecast ON forecast.id = NEW.strategy_forecast_id
                  WHERE decision.id::TEXT = NEW.trace->>'source_decision_id'
                    AND decision.input_hash = NEW.trace->>'source_decision_input_hash'
                    AND decision.instrument_id = forecast.instrument_id
                    AND decision.status = 'published' AND decision.published_at IS NOT NULL
                    AND decision.as_of <= allocation_cutoff
                    AND decision.input_manifest->'trade_plan'->>'trade_plan_id' = NEW.action_id
                    AND decision.input_manifest->'trade_plan'->>'rank_id' = NEW.rank_id
                    AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id
                    AND forecast.status = 'available'
                    AND forecast.available_at <= forecast.input_cutoff
                    AND forecast.input_cutoff <= allocation_cutoff
                    AND decision.published_at <= allocation_cutoff
               )
               OR jsonb_typeof(allocation_meta->'source_hashes') IS DISTINCT FROM 'array'
               OR NOT EXISTS (
                 SELECT 1
                   FROM jsonb_array_elements_text(allocation_meta->'source_hashes') hash
                  WHERE hash = NEW.trace->>'source_decision_input_hash'
               ) THEN
              RAISE EXCEPTION 'Phase 4 allocation authority is not bound to PostgreSQL action lineage';
            END IF;
          END IF;
          NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
            'allocation_item_id', NEW.allocation_item_id, 'allocation_id', NEW.allocation_id,
            'candidate_id', NEW.candidate_id, 'ticker', NEW.ticker,
            'strategy_forecast_id', NEW.strategy_forecast_id, 'action_id', NEW.action_id,
            'rank_id', NEW.rank_id, 'hypothesis_id', NEW.hypothesis_id,
            'disposition', NEW.disposition, 'target_weight', NEW.target_weight,
            'current_weight', NEW.current_weight, 'marginal_book_utility', NEW.marginal_book_utility,
            'trace', NEW.trace, 'blockers', NEW.blockers, 'funding_source', NEW.funding_source,
            'funding_amount', NEW.funding_amount, 'funding_sources', NEW.funding_sources));
          RETURN NEW;
        END;
        $$;
        DROP TRIGGER IF EXISTS portfolio_allocation_item_lineage ON analysis.portfolio_allocation_item;
        CREATE TRIGGER portfolio_allocation_item_lineage
          BEFORE INSERT ON analysis.portfolio_allocation_item
          FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_allocation_item_funding_lineage();

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_source_lineage()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          allocation_meta JSONB;
          expected_hashes JSONB;
          expected_forecasts JSONB;
          expected_actions JSONB;
          stored_forecasts JSONB;
          stored_actions JSONB;
        BEGIN
          SELECT snapshot.metadata, snapshot.forecast_ids, snapshot.action_ids
            INTO allocation_meta, stored_forecasts, stored_actions
            FROM analysis.portfolio_allocation_snapshot snapshot
           WHERE snapshot.allocation_id = NEW.allocation_id;
          IF allocation_meta IS NULL THEN
            RAISE EXCEPTION 'Phase 4 source lineage has no persisted allocation authority';
          END IF;
          SELECT coalesce(jsonb_agg(DISTINCT item.trace->>'source_decision_input_hash'
                                    ORDER BY item.trace->>'source_decision_input_hash'), '[]'::jsonb),
                 coalesce(jsonb_agg(DISTINCT item.strategy_forecast_id ORDER BY item.strategy_forecast_id), '[]'::jsonb),
                 coalesce(jsonb_agg(DISTINCT item.action_id ORDER BY item.action_id), '[]'::jsonb)
            INTO expected_hashes, expected_forecasts, expected_actions
            FROM analysis.portfolio_allocation_item item
           WHERE item.allocation_id = NEW.allocation_id
             AND item.ticker <> 'CASH'
             AND item.disposition IN ('selected', 'rollback');
          IF expected_hashes <> '[]'::jsonb
             AND (allocation_meta->'source_hashes' IS DISTINCT FROM expected_hashes
                  OR stored_forecasts IS DISTINCT FROM expected_forecasts
                  OR stored_actions IS DISTINCT FROM expected_actions) THEN
            RAISE EXCEPTION 'Phase 4 source hashes or action identities are not canonical';
          END IF;
          IF expected_hashes = '[]'::jsonb
             AND (stored_forecasts <> '[]'::jsonb OR stored_actions <> '[]'::jsonb) THEN
            RAISE EXCEPTION 'Phase 4 empty allocation has non-empty action identities';
          END IF;
          RETURN NULL;
        END;
        $$;
        DROP TRIGGER IF EXISTS phase4_source_lineage ON analysis.portfolio_allocation_item;
        CREATE CONSTRAINT TRIGGER phase4_source_lineage
          AFTER INSERT ON analysis.portfolio_allocation_item
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_source_lineage();

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_funding_conservation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          allocation_cutoff TIMESTAMPTZ;
          authority_account BIGINT;
          account_nav DOUBLE PRECISION;
          source_key TEXT;
          claimed DOUBLE PRECISION;
          available DOUBLE PRECISION;
          source_capacity DOUBLE PRECISION;
          cash_balance DOUBLE PRECISION;
          position_id BIGINT;
        BEGIN
          SELECT snapshot.input_cutoff,
                 NULLIF(split_part(snapshot.metadata->>'authority_snapshot_id', ':', 2), '')::BIGINT
            INTO allocation_cutoff, authority_account
            FROM analysis.portfolio_allocation_snapshot snapshot
           WHERE snapshot.allocation_id = NEW.allocation_id;
          IF allocation_cutoff IS NULL OR authority_account IS NULL THEN
            RAISE EXCEPTION 'Phase 4 funding conservation has no authority account';
          END IF;
          SELECT account.net_liquidation, account.cash_balance
            INTO account_nav, cash_balance
            FROM raw.broker_account_snapshot account
           WHERE account.id = authority_account
             AND account.observed_at <= allocation_cutoff;
          IF account_nav IS NULL OR account_nav <= 0 OR cash_balance IS NULL OR cash_balance < 0 THEN
            RAISE EXCEPTION 'Phase 4 funding conservation has incomplete account evidence';
          END IF;

          IF EXISTS (
            SELECT 1
              FROM analysis.portfolio_allocation_item item
              CROSS JOIN LATERAL jsonb_each(item.funding_sources) source
              JOIN raw.broker_position_snapshot position
                ON source.key LIKE 'TRIM:broker-position:%'
               AND position.id = split_part(source.key, ':', 3)::BIGINT
              JOIN raw.broker_account_snapshot account
                ON account.id = position.account_snapshot_id
             WHERE item.allocation_id = NEW.allocation_id
               AND item.disposition IN ('selected', 'rollback')
               AND item.target_weight < item.current_weight
               AND account.id = authority_account
               AND account.observed_at <= allocation_cutoff
               AND position.quantity > 0
               AND position.market_value IS NOT NULL
               AND (source.value #>> '{}')::DOUBLE PRECISION
                   > least(greatest(item.current_weight - item.target_weight, 0) * account_nav,
                           abs(position.market_value)) + 0.000000001
          ) THEN
            RAISE EXCEPTION 'Phase 4 trim funding overdraws a released source';
          END IF;

          FOR source_key, claimed IN
            SELECT source.key, sum((source.value #>> '{}')::DOUBLE PRECISION)
              FROM analysis.portfolio_allocation_item item
              CROSS JOIN LATERAL jsonb_each(item.funding_sources) source
             WHERE item.allocation_id = NEW.allocation_id
               AND item.disposition = 'selected'
               AND item.ticker <> 'CASH'
               AND item.target_weight > item.current_weight
             GROUP BY source.key
          LOOP
            IF source_key LIKE 'CASH:%' THEN
              SELECT account.cash_balance INTO available
                FROM raw.broker_account_snapshot account
               WHERE account.id = split_part(source_key, ':', 3)::BIGINT
                 AND account.id = authority_account
                 AND account.observed_at <= allocation_cutoff;
              IF available IS NULL OR claimed > available + 0.000000001 THEN
                RAISE EXCEPTION 'Phase 4 cash funding is over-allocated for source %', source_key;
              END IF;
            ELSE
              position_id := split_part(source_key, ':', 3)::BIGINT;
              SELECT abs(position.market_value) INTO available
                FROM raw.broker_position_snapshot position
                JOIN raw.broker_account_snapshot account
                  ON account.id = position.account_snapshot_id
               WHERE position.id = position_id
                 AND position.quantity > 0
                 AND position.market_value IS NOT NULL
                 AND account.id = authority_account
                 AND account.observed_at <= allocation_cutoff;
              source_capacity := available;
              IF source_capacity IS NULL THEN
                RAISE EXCEPTION 'Phase 4 trim funding has no persisted source %', source_key;
              END IF;
              IF EXISTS (
                SELECT 1
                  FROM analysis.portfolio_allocation_item item
                 WHERE item.allocation_id = NEW.allocation_id
                   AND item.funding_sources ? source_key
                   AND item.disposition IN ('selected', 'rollback')
                   AND item.target_weight < item.current_weight
                   AND (item.funding_sources->source_key #>> '{}')::DOUBLE PRECISION
                       > least(greatest(item.current_weight - item.target_weight, 0) * account_nav,
                               source_capacity) + 0.000000001
              ) THEN
                RAISE EXCEPTION 'Phase 4 trim funding overdraws released source %', source_key;
              END IF;
              SELECT coalesce(sum(
                       least(greatest(item.current_weight - item.target_weight, 0) * account_nav,
                             source_capacity)
                     ), 0)
                INTO available
                FROM analysis.portfolio_allocation_item item
               WHERE item.allocation_id = NEW.allocation_id
                 AND item.disposition IN ('selected', 'rollback')
                 AND item.target_weight < item.current_weight
                 AND item.trace->>'trim_position_id' = 'broker-position:' || position_id::TEXT;
              available := least(source_capacity, available);
              IF claimed > available + 0.000000001 THEN
                RAISE EXCEPTION 'Phase 4 trim funding exceeds released source %', source_key;
              END IF;
            END IF;
          END LOOP;
          RETURN NULL;
        END;
        $$;
        DROP TRIGGER IF EXISTS phase4_funding_conservation ON analysis.portfolio_allocation_item;
        CREATE CONSTRAINT TRIGGER phase4_funding_conservation
          AFTER INSERT ON analysis.portfolio_allocation_item
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_funding_conservation();

        CREATE OR REPLACE FUNCTION analysis.write_phase4_execution(p JSONB, sig TEXT)
        RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, analysis, app, public AS $$
        DECLARE
          allocation_cutoff TIMESTAMPTZ;
          derived_cutoff TIMESTAMPTZ;
          target_allocation_id TEXT;
          observation_ids JSONB;
          canonical_metadata JSONB;
          sample_count INTEGER;
          derived_status TEXT;
          derived_fill_probability DOUBLE PRECISION;
          derived_spread DOUBLE PRECISION;
          derived_latency DOUBLE PRECISION;
          derived_impact DOUBLE PRECISION;
          expected_id TEXT;
          expected_input_hash TEXT;
          expected_content_hash TEXT;
        BEGIN
          IF NOT analysis.phase4_telemetry_authorized('phase4-execution.v1', p, sig) THEN
            RAISE EXCEPTION 'Phase 4 execution authorization signature is invalid';
          END IF;
          IF jsonb_typeof(p) IS DISTINCT FROM 'object'
             OR p->>'allocation_id' IS NULL
             OR p->>'model_version' IS DISTINCT FROM 'paper-telemetry.v1' THEN
            RAISE EXCEPTION 'Phase 4 execution writer payload is malformed';
          END IF;
          target_allocation_id := p->>'allocation_id';
          SELECT snapshot.input_cutoff INTO allocation_cutoff
            FROM analysis.portfolio_allocation_snapshot snapshot
           WHERE snapshot.allocation_id = target_allocation_id;
          IF allocation_cutoff IS NULL THEN
            RAISE EXCEPTION 'Phase 4 execution snapshot allocation is not persisted';
          END IF;
          observation_ids := coalesce(p->'metadata'->'paper_observation_ids', '[]'::jsonb);
          IF jsonb_typeof(observation_ids) IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Phase 4 execution observations must be an array';
          END IF;
          IF (SELECT count(*) FROM jsonb_array_elements_text(observation_ids))
             <> (SELECT count(DISTINCT value) FROM jsonb_array_elements_text(observation_ids) AS ids(value)) THEN
            RAISE EXCEPTION 'Phase 4 execution observations contain duplicate IDs';
          END IF;
          SELECT count(*)::INTEGER, max(observation.available_at)
            INTO sample_count, derived_cutoff
            FROM jsonb_array_elements_text(observation_ids) AS ids(value)
            JOIN app.paper_execution_observation observation
              ON observation.paper_execution_observation_id = ids.value
            JOIN analysis.portfolio_allocation_item item
              ON item.allocation_item_id = observation.allocation_item_id
           WHERE item.allocation_id = target_allocation_id;
          IF jsonb_array_length(observation_ids) <> sample_count THEN
            RAISE EXCEPTION 'Phase 4 execution observations are not bound to the allocation';
          END IF;
          IF EXISTS (
            SELECT 1
              FROM jsonb_array_elements_text(observation_ids) AS ids(value)
              LEFT JOIN app.paper_execution_observation observation
                ON observation.paper_execution_observation_id = ids.value
              LEFT JOIN analysis.portfolio_allocation_item item
                ON item.allocation_item_id = observation.allocation_item_id
              LEFT JOIN app.paper_order paper ON paper.id = observation.paper_order_id
             WHERE observation.paper_execution_observation_id IS NULL
                OR item.allocation_id IS DISTINCT FROM target_allocation_id
                OR observation.available_at <= allocation_cutoff
                OR observation.available_at > (p->>'input_cutoff')::TIMESTAMPTZ
                OR observation.execution_mode <> 'paper' OR NOT observation.paper_only
                OR observation.status NOT IN ('partial', 'filled', 'partial_exited', 'exited')
                OR observation.filled_quantity <= 0 OR observation.fill_price IS NULL
                OR observation.contract_multiplier IS NULL OR observation.event_fee IS NULL
                OR paper.submitted_at IS NULL OR paper.filled_at IS NULL
                OR paper.fill_evidence_at IS NULL OR paper.execution_quote IS NULL
                OR paper.actual_fill_price IS NULL OR paper.contract_multiplier IS NULL
                OR paper.status NOT IN ('open', 'entered', 'partial_exited', 'exited', 'closed', 'invalidated')
          ) THEN
            RAISE EXCEPTION 'Phase 4 execution snapshot requires genuine post-allocation paper fills';
          END IF;
          IF sample_count > 0 THEN
            derived_status := 'calibrated';
            IF derived_cutoff IS DISTINCT FROM (p->>'input_cutoff')::TIMESTAMPTZ THEN
              RAISE EXCEPTION 'Phase 4 execution cutoff must equal maximum observation availability';
            END IF;
            SELECT avg((observation.filled_quantity > 0)::INTEGER)::DOUBLE PRECISION,
                   avg(observation.spread_bps) FILTER (WHERE observation.spread_bps IS NOT NULL),
                   avg(extract(epoch FROM (paper.filled_at - paper.submitted_at)) * 1000),
                   avg(abs(observation.fill_price - observation.requested_price)
                       / observation.requested_price * 10000)
                     FILTER (WHERE observation.requested_price IS NOT NULL AND observation.requested_price <> 0)
              INTO derived_fill_probability, derived_spread, derived_latency, derived_impact
              FROM jsonb_array_elements_text(observation_ids) AS ids(value)
              JOIN app.paper_execution_observation observation
                ON observation.paper_execution_observation_id = ids.value
              JOIN app.paper_order paper ON paper.id = observation.paper_order_id;
          ELSE
            derived_status := 'calibration_pending';
            derived_cutoff := (p->>'input_cutoff')::TIMESTAMPTZ;
            derived_fill_probability := NULL;
            derived_spread := NULL;
            derived_latency := NULL;
            derived_impact := NULL;
          END IF;
          IF derived_cutoff <= allocation_cutoff THEN
            RAISE EXCEPTION 'Phase 4 execution cutoff must follow its allocation';
          END IF;
          canonical_metadata := jsonb_build_object(
            'paper_observation_ids', (SELECT coalesce(jsonb_agg(value ORDER BY value), '[]'::jsonb)
                                        FROM jsonb_array_elements_text(observation_ids) AS ids(value)),
            'genuine_fill_count', sample_count,
            'source', 'paper_execution_observation');
          IF p->>'calibration_status' IS DISTINCT FROM derived_status
             OR (p->>'sample_count')::INTEGER IS DISTINCT FROM sample_count
             OR (p->>'fill_probability')::DOUBLE PRECISION IS DISTINCT FROM derived_fill_probability
             OR (p->>'spread_bps')::DOUBLE PRECISION IS DISTINCT FROM derived_spread
             OR (p->>'latency_ms')::DOUBLE PRECISION IS DISTINCT FROM derived_latency
             OR (p->>'impact_bps')::DOUBLE PRECISION IS DISTINCT FROM derived_impact
             OR (p->>'input_cutoff')::TIMESTAMPTZ IS DISTINCT FROM derived_cutoff
             OR p->'metadata' IS DISTINCT FROM canonical_metadata THEN
            RAISE EXCEPTION 'Phase 4 execution metrics are not derived from persisted paper fills';
          END IF;
          expected_id := 'execution:' || analysis.phase4_content_digest(jsonb_build_object(
            'allocation_id', target_allocation_id,
            'input_cutoff', analysis.phase4_canonical_timestamp(derived_cutoff),
            'model_version', 'paper-telemetry.v1', 'calibration_status', derived_status,
            'sample_count', sample_count, 'fill_probability', derived_fill_probability,
            'spread_bps', derived_spread, 'latency_ms', derived_latency, 'impact_bps', derived_impact,
            'metadata', canonical_metadata));
          expected_input_hash := split_part(expected_id, ':', 2);
          expected_content_hash := analysis.phase4_content_digest(jsonb_build_object(
            'execution_model_snapshot_id', expected_id, 'allocation_id', target_allocation_id,
            'model_version', 'paper-telemetry.v1', 'calibration_status', derived_status,
            'sample_count', sample_count, 'fill_probability', derived_fill_probability,
            'spread_bps', derived_spread, 'latency_ms', derived_latency, 'impact_bps', derived_impact,
            'input_cutoff', analysis.phase4_canonical_timestamp(derived_cutoff), 'metadata', canonical_metadata));
          IF p->>'execution_model_snapshot_id' IS DISTINCT FROM expected_id
             OR p->>'input_hash' IS DISTINCT FROM expected_input_hash
             OR p->>'content_hash' IS DISTINCT FROM expected_content_hash THEN
            RAISE EXCEPTION 'Phase 4 execution identity or content hash is not database-canonical';
          END IF;
          INSERT INTO analysis.execution_model_snapshot
            (execution_model_snapshot_id, allocation_id, model_version, calibration_status, sample_count,
             fill_probability, spread_bps, latency_ms, impact_bps, input_cutoff, input_hash, content_hash, metadata)
          VALUES (expected_id, target_allocation_id, 'paper-telemetry.v1', derived_status, sample_count,
                  derived_fill_probability, derived_spread, derived_latency, derived_impact,
                  derived_cutoff, expected_input_hash, expected_content_hash, canonical_metadata)
          ON CONFLICT (execution_model_snapshot_id) DO NOTHING;
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        DROP TRIGGER IF EXISTS phase4_funding_conservation ON analysis.portfolio_allocation_item;
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_funding_conservation();
        DROP TRIGGER IF EXISTS phase4_source_lineage ON analysis.portfolio_allocation_item;
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_source_lineage();
        DROP TRIGGER IF EXISTS portfolio_allocation_item_lineage ON analysis.portfolio_allocation_item;
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_allocation_item_funding_lineage();
        CREATE TRIGGER portfolio_allocation_item_lineage
          BEFORE INSERT ON analysis.portfolio_allocation_item
          FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();
        DROP FUNCTION IF EXISTS analysis.write_phase4_execution(JSONB, TEXT);
        ALTER TABLE app.paper_execution_observation DROP CONSTRAINT IF EXISTS phase4_paper_observation_status;
        ALTER TABLE analysis.portfolio_allocation_item DROP CONSTRAINT IF EXISTS phase4_allocation_item_funding_amount_shape;
        ALTER TABLE analysis.portfolio_allocation_item DROP CONSTRAINT IF EXISTS phase4_allocation_item_funding_sources_shape;
        ALTER TABLE app.paper_execution_observation
          ADD CONSTRAINT paper_execution_observation_status_check
          CHECK (status IN ('planned', 'submitted', 'partial', 'filled', 'exited', 'cancelled', 'unavailable'));
        ALTER TABLE analysis.portfolio_allocation_item
          ADD CONSTRAINT phase4_allocation_item_funding_sources_shape
          CHECK (jsonb_typeof(funding_sources) = 'object'),
          ADD CONSTRAINT phase4_allocation_item_funding_sources_required
          CHECK (ticker = 'CASH' OR disposition <> 'selected' OR target_weight <= current_weight
                 OR (funding_amount IS NOT NULL AND funding_amount > 0 AND funding_sources <> '{}'::jsonb));

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
                   OR (NEW.funding_source NOT LIKE 'CASH:broker-account:%'
                       AND NEW.funding_source NOT LIKE 'TRIM:broker-position:%') THEN
                    RAISE EXCEPTION 'Phase 4 increase requires persisted PostgreSQL cash';
                END IF;
                IF NEW.funding_source LIKE 'CASH:broker-account:%' AND NOT EXISTS (
                    SELECT 1 FROM raw.broker_account_snapshot account
                    WHERE ('CASH:broker-account:' || account.id::text) = NEW.funding_source
                      AND account.cash_balance >= NEW.funding_amount
                      AND account.observed_at <= (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id)
                ) THEN
                    RAISE EXCEPTION 'Phase 4 cash funding is not persisted';
                END IF;
                IF NEW.funding_source LIKE 'TRIM:broker-position:%' AND NOT EXISTS (
                    SELECT 1
                    FROM raw.broker_position_snapshot position
                    JOIN raw.broker_account_snapshot account ON account.id = position.account_snapshot_id
                    WHERE position.id = split_part(NEW.funding_source, ':', 3)::BIGINT
                      AND position.quantity > 0
                      AND abs(coalesce(position.market_value, 0)) >= NEW.funding_amount
                      AND account.observed_at <= (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id)
                ) THEN
                    RAISE EXCEPTION 'Phase 4 trim funding is not persisted';
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

        CREATE OR REPLACE FUNCTION analysis.write_phase4_execution(p JSONB, sig TEXT)
        RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, analysis, app AS $$
        BEGIN
          IF NOT analysis.phase4_telemetry_authorized('phase4-execution.v1', p, sig) THEN
            RAISE EXCEPTION 'Phase 4 execution authorization signature is invalid';
          END IF;
          INSERT INTO analysis.execution_model_snapshot
            (execution_model_snapshot_id, allocation_id, model_version, calibration_status, sample_count,
             fill_probability, spread_bps, latency_ms, impact_bps, input_cutoff, input_hash, content_hash, metadata)
          VALUES (p->>'execution_model_snapshot_id', p->>'allocation_id', p->>'model_version', p->>'calibration_status',
            (p->>'sample_count')::integer, (p->>'fill_probability')::double precision,
            (p->>'spread_bps')::double precision, (p->>'latency_ms')::double precision,
            (p->>'impact_bps')::double precision, (p->>'input_cutoff')::timestamptz,
            p->>'input_hash', p->>'content_hash', jsonb_extract_path(p, 'metadata'))
          ON CONFLICT (execution_model_snapshot_id) DO NOTHING;
        END;
        $$;
        GRANT EXECUTE ON FUNCTION analysis.write_phase4_execution(JSONB, TEXT) TO market_app;
        """
    )
