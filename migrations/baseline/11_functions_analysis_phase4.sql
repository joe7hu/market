-- Analysis functions: phase 4 guards and lineage.

CREATE FUNCTION analysis.enforce_phase4_allocation_item_funding_lineage() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$ DECLARE allocation_meta JSONB; allocation_cutoff TIMESTAMPTZ; BEGIN SELECT snapshot.metadata, snapshot.input_cutoff INTO allocation_meta, allocation_cutoff FROM analysis.portfolio_allocation_snapshot snapshot WHERE snapshot.allocation_id = NEW.allocation_id; IF NEW.disposition IN ('selected', 'rollback') AND NEW.ticker <> 'CASH' THEN IF allocation_meta IS NULL OR allocation_meta->>'authority' <> 'postgresql' OR allocation_meta->>'execution_status' <> 'calibrated' OR NEW.strategy_forecast_id IS NULL OR NEW.action_id IS NULL OR NEW.rank_id IS NULL OR NEW.trace->>'source_decision_id' IS NULL OR NEW.trace->>'source_decision_input_hash' IS NULL OR NOT analysis.phase4_account_authority_exists( allocation_meta->>'authority_snapshot_id', allocation_cutoff ) OR NOT EXISTS ( SELECT 1 FROM analysis.execution_model_snapshot model JOIN analysis.portfolio_allocation_snapshot model_allocation ON model_allocation.allocation_id = model.allocation_id WHERE model.execution_model_snapshot_id = allocation_meta->>'execution_model_snapshot_id' AND model.model_version = 'paper-telemetry.v1' AND model.calibration_status = 'calibrated' AND model.sample_count > 0 AND model.input_cutoff > model_allocation.input_cutoff AND model.input_cutoff <= allocation_cutoff AND model.available_at <= allocation_cutoff AND model.metadata->>'source' = 'paper_execution_observation' AND jsonb_typeof(model.metadata->'paper_observation_ids') = 'array' AND jsonb_array_length(model.metadata->'paper_observation_ids') = model.sample_count AND CASE WHEN model.metadata->>'genuine_fill_count' ~ '^[0-9]+$' THEN (model.metadata->>'genuine_fill_count')::INTEGER = model.sample_count ELSE false END ) OR NOT EXISTS ( SELECT 1 FROM analysis.ticker_decision decision JOIN analysis.strategy_forecast forecast ON forecast.id = NEW.strategy_forecast_id WHERE decision.id::TEXT = NEW.trace->>'source_decision_id' AND decision.input_hash = NEW.trace->>'source_decision_input_hash' AND decision.instrument_id = forecast.instrument_id AND decision.status = 'published' AND decision.published_at IS NOT NULL AND decision.as_of <= allocation_cutoff AND decision.input_manifest->'trade_plan'->>'trade_plan_id' = NEW.action_id AND decision.input_manifest->'trade_plan'->>'rank_id' = NEW.rank_id AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id AND forecast.status = 'available' AND forecast.available_at <= forecast.input_cutoff AND forecast.input_cutoff <= allocation_cutoff AND decision.published_at <= allocation_cutoff ) OR jsonb_typeof(allocation_meta->'source_hashes') IS DISTINCT FROM 'array' OR NOT EXISTS ( SELECT 1 FROM jsonb_array_elements_text(allocation_meta->'source_hashes') hash WHERE hash = NEW.trace->>'source_decision_input_hash' ) THEN RAISE EXCEPTION 'Phase 4 allocation authority is not bound to PostgreSQL action lineage'; END IF; END IF; NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'allocation_item_id', NEW.allocation_item_id, 'allocation_id', NEW.allocation_id, 'candidate_id', NEW.candidate_id, 'ticker', NEW.ticker, 'strategy_forecast_id', NEW.strategy_forecast_id, 'action_id', NEW.action_id, 'rank_id', NEW.rank_id, 'hypothesis_id', NEW.hypothesis_id, 'disposition', NEW.disposition, 'target_weight', NEW.target_weight, 'current_weight', NEW.current_weight, 'marginal_book_utility', NEW.marginal_book_utility, 'trace', NEW.trace, 'blockers', NEW.blockers, 'funding_source', NEW.funding_source, 'funding_amount', NEW.funding_amount, 'funding_sources', NEW.funding_sources)); RETURN NEW; END; $_$;

CREATE FUNCTION analysis.enforce_phase4_attribution_multiplier_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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

CREATE FUNCTION analysis.enforce_phase4_authority_lineage() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ DECLARE allocation_meta JSONB; allocation_cutoff TIMESTAMPTZ; BEGIN SELECT metadata, input_cutoff INTO allocation_meta, allocation_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id; IF NEW.disposition = 'selected' AND NEW.ticker <> 'CASH' THEN IF allocation_meta IS NULL OR allocation_meta->>'authority' <> 'postgresql' OR NOT analysis.phase4_account_authority_exists( allocation_meta->>'authority_snapshot_id', allocation_cutoff ) OR NOT EXISTS ( SELECT 1 FROM analysis.ticker_decision decision JOIN analysis.strategy_forecast forecast ON forecast.id = NEW.strategy_forecast_id WHERE decision.id::text = NEW.trace->>'source_decision_id' AND decision.input_hash = NEW.trace->>'source_decision_input_hash' AND decision.status = 'published' AND decision.published_at IS NOT NULL AND decision.input_manifest->'trade_plan'->>'trade_plan_id' = NEW.action_id AND decision.input_manifest->'trade_plan'->>'rank_id' = NEW.rank_id AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id AND forecast.input_cutoff <= allocation_cutoff ) OR jsonb_typeof(allocation_meta->'source_hashes') <> 'array' OR NOT EXISTS ( SELECT 1 FROM jsonb_array_elements_text(allocation_meta->'source_hashes') hash WHERE hash = NEW.trace->>'source_decision_input_hash' ) THEN RAISE EXCEPTION 'Phase 4 allocation authority is not bound to PostgreSQL action lineage'; END IF; END IF; IF TG_OP = 'INSERT' THEN IF EXISTS ( SELECT 1 FROM analysis.portfolio_allocation_item item WHERE item.allocation_id = NEW.allocation_id AND item.disposition = 'selected' AND item.ticker <> 'CASH' AND NOT EXISTS ( SELECT 1 FROM jsonb_array_elements_text(allocation_meta->'source_hashes') hash WHERE hash = item.trace->>'source_decision_input_hash' ) ) THEN RAISE EXCEPTION 'Phase 4 allocation source hashes are incomplete'; END IF; END IF; RETURN NULL; END; $$;

CREATE FUNCTION analysis.enforce_phase4_authority_snapshot_complete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.status <> 'cash_only' AND NOT EXISTS (
                SELECT 1 FROM analysis.portfolio_allocation_item item
                 WHERE item.allocation_id = NEW.allocation_id
                   AND item.disposition = 'selected' AND item.ticker <> 'CASH'
            ) THEN RAISE EXCEPTION 'Phase 4 available allocation requires PostgreSQL-bound actions'; END IF;
            RETURN NULL;
        END;
        $$;

CREATE FUNCTION analysis.enforce_phase4_execution_snapshot_evidence() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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

CREATE FUNCTION analysis.enforce_phase4_execution_snapshot_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE allocation_cutoff TIMESTAMPTZ;
        BEGIN
          SELECT input_cutoff INTO allocation_cutoff
            FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id;
          IF allocation_cutoff IS NULL OR NEW.input_cutoff <= allocation_cutoff THEN
            RAISE EXCEPTION 'Phase 4 execution snapshot cutoff must follow its allocation';
          END IF;
          IF NEW.calibration_status = 'calibrated' AND (
            NEW.sample_count <= 0 OR jsonb_typeof(NEW.metadata->'paper_observation_ids') IS DISTINCT FROM 'array'
            OR jsonb_array_length(NEW.metadata->'paper_observation_ids') <> NEW.sample_count
            OR EXISTS (
              SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'paper_observation_ids') id
              LEFT JOIN app.paper_execution_observation observation ON observation.paper_execution_observation_id = id
              LEFT JOIN analysis.portfolio_allocation_item item ON item.allocation_item_id = observation.allocation_item_id
              LEFT JOIN app.paper_order paper ON paper.id = observation.paper_order_id
              WHERE observation.paper_execution_observation_id IS NULL OR item.allocation_id <> NEW.allocation_id
                OR observation.available_at <= allocation_cutoff OR observation.available_at > NEW.input_cutoff
                OR observation.filled_quantity <= 0 OR observation.fill_price IS NULL
                OR observation.contract_multiplier IS NULL OR observation.event_fee IS NULL
                OR paper.submitted_at IS NULL OR paper.filled_at IS NULL OR paper.fill_evidence_at IS NULL
                OR paper.execution_quote IS NULL OR paper.actual_fill_price IS NULL)) THEN
            RAISE EXCEPTION 'Phase 4 calibrated snapshot requires matching post-allocation paper fills';
          END IF;
          RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_phase4_funding_conservation() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$
        DECLARE
          allocation_cutoff TIMESTAMPTZ;
          authority_id TEXT;
          account_nav DOUBLE PRECISION;
          source_key TEXT;
          claimed DOUBLE PRECISION;
          available DOUBLE PRECISION;
          source_capacity DOUBLE PRECISION;
        BEGIN
          SELECT snapshot.input_cutoff, snapshot.metadata->>'authority_snapshot_id'
            INTO allocation_cutoff, authority_id
            FROM analysis.portfolio_allocation_snapshot snapshot
           WHERE snapshot.allocation_id = NEW.allocation_id;
          IF allocation_cutoff IS NULL OR NOT analysis.phase4_account_authority_exists(authority_id, allocation_cutoff) THEN
            RAISE EXCEPTION 'Phase 4 funding conservation has no reconciled account authority';
          END IF;
          IF authority_id ~ '^broker-account:[0-9]+$' THEN
            SELECT net_liquidation::DOUBLE PRECISION INTO account_nav
              FROM raw.broker_account_snapshot
             WHERE ('broker-account:' || id::TEXT) = authority_id AND observed_at <= allocation_cutoff;
          ELSE
            SELECT net_liquidation::DOUBLE PRECISION INTO account_nav
              FROM app.manual_account_snapshot
             WHERE ('manual-account:' || id::TEXT) = authority_id
               AND reconciliation_state = 'reconciled' AND effective_at <= allocation_cutoff;
          END IF;
          IF account_nav IS NULL OR account_nav <= 0 THEN
            RAISE EXCEPTION 'Phase 4 funding conservation has incomplete account evidence';
          END IF;
          FOR source_key, claimed IN
            SELECT source.key, sum((source.value #>> '{}')::DOUBLE PRECISION)
              FROM analysis.portfolio_allocation_item item
              CROSS JOIN LATERAL jsonb_each(item.funding_sources) source
             WHERE item.allocation_id = NEW.allocation_id
               AND item.disposition = 'selected' AND item.ticker <> 'CASH'
               AND item.target_weight > item.current_weight
             GROUP BY source.key
          LOOP
            source_capacity := analysis.phase4_funding_source_capacity(source_key, authority_id, allocation_cutoff);
            IF source_capacity IS NULL OR claimed > source_capacity + 0.000000001 THEN
              RAISE EXCEPTION 'Phase 4 funding source is unavailable or over-allocated: %', source_key;
            END IF;
            IF source_key LIKE 'TRIM:%' THEN
              SELECT coalesce(sum(
                       least(greatest(item.current_weight - item.target_weight, 0) * account_nav,
                             source_capacity)
                     ), 0)
                INTO available
                FROM analysis.portfolio_allocation_item item
               WHERE item.allocation_id = NEW.allocation_id
                 AND item.disposition IN ('selected', 'rollback')
                 AND item.target_weight < item.current_weight
                 AND item.trace->>'trim_position_id' = replace(source_key, 'TRIM:', '');
              IF claimed > least(source_capacity, available) + 0.000000001 THEN
                RAISE EXCEPTION 'Phase 4 trim funding exceeds released source %', source_key;
              END IF;
            END IF;
          END LOOP;
          RETURN NULL;
        END;
        $_$;

CREATE FUNCTION analysis.enforce_phase4_funding_content() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
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

CREATE FUNCTION analysis.enforce_phase4_lineage() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ DECLARE expected_cutoff TIMESTAMPTZ; expected_allocation TEXT; expected_forecast TEXT; expected_hypothesis UUID; expected_action TEXT; expected_rank TEXT; expected_expression JSONB; expected_experiment TEXT; expected_trial UUID; expected_result UUID; scenario JSONB; probability_total DOUBLE PRECISION := 0; expected_realized_pnl DOUBLE PRECISION; BEGIN IF TG_TABLE_NAME = 'portfolio_allocation_snapshot' THEN NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'allocation_id', NEW.allocation_id, 'as_of', analysis.phase4_canonical_timestamp(NEW.as_of), 'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff), 'status', NEW.status, 'cash_hurdle', NEW.cash_hurdle, 'forecast_ids', NEW.forecast_ids, 'action_ids', NEW.action_ids, 'strategy_registry_ids', NEW.strategy_registry_ids, 'metadata', NEW.metadata )); ELSIF TG_TABLE_NAME = 'portfolio_allocation_item' THEN NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'allocation_item_id', NEW.allocation_item_id, 'allocation_id', NEW.allocation_id, 'candidate_id', NEW.candidate_id, 'ticker', NEW.ticker, 'strategy_forecast_id', NEW.strategy_forecast_id, 'action_id', NEW.action_id, 'rank_id', NEW.rank_id, 'hypothesis_id', NEW.hypothesis_id, 'disposition', NEW.disposition, 'target_weight', NEW.target_weight, 'current_weight', NEW.current_weight, 'marginal_book_utility', NEW.marginal_book_utility, 'trace', NEW.trace, 'blockers', NEW.blockers, 'funding_source', NEW.funding_source, 'funding_amount', NEW.funding_amount )); ELSIF TG_TABLE_NAME = 'probabilistic_portfolio_scenario_artifact' THEN NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'scenario_artifact_id', NEW.scenario_artifact_id, 'allocation_id', NEW.allocation_id, 'model_version', NEW.model_version, 'probability_semantics', NEW.probability_semantics, 'scenarios', NEW.scenarios, 'tail_dependence', NEW.tail_dependence, 'simultaneous_unwind', NEW.simultaneous_unwind, 'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff) )); ELSIF TG_TABLE_NAME = 'execution_model_snapshot' THEN NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'execution_model_snapshot_id', NEW.execution_model_snapshot_id, 'allocation_id', NEW.allocation_id, 'model_version', NEW.model_version, 'calibration_status', NEW.calibration_status, 'sample_count', NEW.sample_count, 'fill_probability', NEW.fill_probability, 'spread_bps', NEW.spread_bps, 'latency_ms', NEW.latency_ms, 'impact_bps', NEW.impact_bps, 'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff), 'metadata', NEW.metadata )); ELSIF TG_TABLE_NAME = 'book_attribution' THEN NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'book_attribution_id', NEW.book_attribution_id, 'allocation_id', NEW.allocation_id, 'allocation_item_id', NEW.allocation_item_id, 'strategy_forecast_id', NEW.strategy_forecast_id, 'hypothesis_id', NEW.hypothesis_id, 'action_id', NEW.action_id, 'rank_id', NEW.rank_id, 'expression', NEW.expression, 'experiment_id', NEW.experiment_id, 'trial_id', NEW.trial_id, 'result_id', NEW.result_id, 'paper_execution_observation_id', NEW.paper_execution_observation_id, 'pnl_status', NEW.pnl_status, 'realized_pnl', NEW.realized_pnl, 'attribution', NEW.attribution, 'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff) )); ELSIF TG_TABLE_NAME = 'portfolio_drift_evidence' THEN NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'decision_id', NEW.decision_id, 'allocation_id', NEW.allocation_id, 'allocation_item_id', NEW.allocation_item_id, 'drift_score', NEW.drift_score, 'rollback_threshold', NEW.rollback_threshold, 'proposed_weight', NEW.proposed_weight, 'action', NEW.action, 'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff), 'metadata', NEW.metadata )); END IF; IF TG_TABLE_NAME = 'portfolio_allocation_item' THEN IF NEW.disposition = 'selected' AND NEW.ticker <> 'CASH' THEN IF NEW.funding_amount IS NULL OR NEW.funding_amount <= 0 THEN RAISE EXCEPTION 'Phase 4 funded item requires a positive funding amount'; ELSIF coalesce((SELECT metadata->>'execution_status' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id), '') <> 'calibrated' THEN RAISE EXCEPTION 'Phase 4 funded item requires a fresh calibrated execution snapshot'; ELSIF NEW.funding_source LIKE ANY (ARRAY['CASH:broker-account:%', 'CASH:manual-account:%']) AND (analysis.phase4_funding_source_capacity( NEW.funding_source, (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id), (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) ) IS NULL OR analysis.phase4_funding_source_capacity( NEW.funding_source, (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id), (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) ) < NEW.funding_amount) THEN RAISE EXCEPTION 'Phase 4 cash funding source is not an actual PostgreSQL account snapshot'; ELSIF NEW.funding_source LIKE ANY (ARRAY['TRIM:broker-position:%', 'TRIM:manual-position:%']) AND (analysis.phase4_funding_source_capacity( NEW.funding_source, (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id), (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) ) IS NULL OR analysis.phase4_funding_source_capacity( NEW.funding_source, (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id), (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) ) < NEW.funding_amount) THEN RAISE EXCEPTION 'Phase 4 trim funding source is not an actual PostgreSQL position'; ELSIF NOT (NEW.funding_source LIKE ANY (ARRAY[ 'CASH:broker-account:%', 'CASH:manual-account:%', 'TRIM:broker-position:%', 'TRIM:manual-position:%'])) THEN RAISE EXCEPTION 'Phase 4 funded item has no authoritative funding source'; END IF; END IF; IF NEW.strategy_forecast_id IS NOT NULL THEN SELECT forecast.input_cutoff, forecast.id, revision.hypothesis_id INTO expected_cutoff, expected_forecast, expected_hypothesis FROM analysis.strategy_forecast forecast JOIN analysis.strategy_revision revision ON revision.id = forecast.strategy_revision_id WHERE forecast.id = NEW.strategy_forecast_id; IF expected_forecast IS NULL OR expected_cutoff > (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) OR NEW.hypothesis_id IS DISTINCT FROM expected_hypothesis THEN RAISE EXCEPTION 'Phase 4 allocation forecast or PIT lineage is invalid'; END IF; IF NEW.action_id IS NOT NULL AND NOT EXISTS ( SELECT 1 FROM analysis.ticker_decision decision WHERE decision.input_manifest->'trade_plan'->>'trade_plan_id' = NEW.action_id AND decision.input_manifest->'trade_plan'->>'rank_id' = NEW.rank_id AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id AND decision.status = 'published' AND decision.published_at IS NOT NULL AND decision.as_of <= (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) AND decision.published_at <= (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) ) THEN RAISE EXCEPTION 'Phase 4 allocation action or rank lineage is invalid'; END IF; END IF; ELSIF TG_TABLE_NAME = 'probabilistic_portfolio_scenario_artifact' THEN IF jsonb_array_length(NEW.scenarios) = 0 OR jsonb_array_length(NEW.scenarios) > 64 THEN RAISE EXCEPTION 'Phase 4 scenario paths must be bounded and non-empty'; END IF; FOR scenario IN SELECT value FROM jsonb_array_elements(NEW.scenarios) LOOP IF jsonb_typeof(scenario->'probability') IS DISTINCT FROM 'number' OR jsonb_typeof(scenario->'returns') IS DISTINCT FROM 'object' OR scenario->'returns' = '{}'::jsonb OR jsonb_typeof(scenario->'shocks') IS DISTINCT FROM 'object' OR scenario->'shocks' = '{}'::jsonb OR scenario->'returns' = scenario->'shocks' THEN RAISE EXCEPTION 'Phase 4 scenario path requires probability, returns, and shocks'; END IF; IF jsonb_typeof(scenario->'provenance') IS DISTINCT FROM 'array' OR jsonb_array_length(scenario->'provenance') = 0 OR EXISTS ( SELECT 1 FROM jsonb_array_elements(scenario->'provenance') source WHERE jsonb_typeof(source->'strategy_pnl_tape_id') IS DISTINCT FROM 'string' OR jsonb_typeof(source->'pnl_date') IS DISTINCT FROM 'string' OR jsonb_typeof(source->'input_cutoff') IS DISTINCT FROM 'string' OR jsonb_typeof(source->'available_at') IS DISTINCT FROM 'string' OR jsonb_typeof(source->'input_hash') IS DISTINCT FROM 'string' ) THEN RAISE EXCEPTION 'Phase 4 scenario path requires complete persisted tape provenance'; END IF; probability_total := probability_total + (scenario->>'probability')::DOUBLE PRECISION; END LOOP; IF EXISTS ( SELECT 1 FROM jsonb_array_elements(NEW.scenarios) path CROSS JOIN LATERAL jsonb_array_elements(path->'provenance') source WHERE NOT EXISTS ( SELECT 1 FROM analysis.strategy_pnl_tape tape JOIN analysis.portfolio_allocation_item item ON item.allocation_id = NEW.allocation_id AND item.strategy_forecast_id = tape.strategy_forecast_id AND item.disposition = 'selected' WHERE tape.id::text = source->>'strategy_pnl_tape_id' AND tape.input_hash::text = source->>'input_hash' AND tape.input_cutoff <= NEW.input_cutoff AND tape.available_at <= NEW.input_cutoff ) ) THEN RAISE EXCEPTION 'Phase 4 scenario provenance is not bound to a persisted tape row'; END IF; IF probability_total < 0.999999 OR probability_total > 1.000001 THEN RAISE EXCEPTION 'Phase 4 scenario probabilities must sum to one'; END IF; IF NOT (NEW.tail_dependence ? 'negative_return_co_exceedance' OR NEW.tail_dependence ? 'co_exceedance') THEN RAISE EXCEPTION 'Phase 4 scenario artifact requires persisted tail co-exceedance results'; END IF; IF NOT (NEW.simultaneous_unwind ? 'probability' AND NEW.simultaneous_unwind ? 'observations') THEN RAISE EXCEPTION 'Phase 4 scenario artifact requires simultaneous-unwind results'; END IF; SELECT input_cutoff INTO expected_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id; IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN RAISE EXCEPTION 'Phase 4 scenario input cutoff does not match allocation lineage'; END IF; ELSIF TG_TABLE_NAME = 'execution_model_snapshot' THEN IF NEW.allocation_id IS NULL THEN RAISE EXCEPTION 'Phase 4 execution snapshot requires allocation lineage'; END IF; SELECT input_cutoff INTO expected_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id; IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN RAISE EXCEPTION 'Phase 4 execution input cutoff does not match allocation lineage'; END IF; IF NEW.sample_count > 0 AND ( jsonb_typeof(NEW.metadata->'paper_observation_ids') IS DISTINCT FROM 'array' OR jsonb_array_length(NEW.metadata->'paper_observation_ids') <> NEW.sample_count OR EXISTS ( SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'paper_observation_ids') observation_id WHERE NOT EXISTS ( SELECT 1 FROM app.paper_execution_observation observation JOIN app.paper_order paper ON paper.id = observation.paper_order_id WHERE observation.paper_execution_observation_id = observation_id AND observation.allocation_item_id IN ( SELECT allocation_item_id FROM analysis.portfolio_allocation_item WHERE allocation_id = NEW.allocation_id ) AND observation.paper_only AND observation.execution_mode = 'paper' AND observation.filled_quantity > 0 AND observation.fill_price IS NOT NULL AND paper.status IN ('open', 'entered', 'partial_exited', 'exited', 'closed', 'invalidated') ) ) ) THEN RAISE EXCEPTION 'Phase 4 execution snapshot is not bound to genuine paper fills'; END IF; ELSIF TG_TABLE_NAME = 'book_attribution' THEN SELECT item.allocation_id, item.strategy_forecast_id, item.hypothesis_id, item.action_id, item.rank_id, item.trace->'expression', forecast.research_trial_id, forecast.trial_result_id INTO expected_allocation, expected_forecast, expected_hypothesis, expected_action, expected_rank, expected_expression, expected_trial, expected_result FROM analysis.portfolio_allocation_item item JOIN analysis.strategy_forecast forecast ON forecast.id = item.strategy_forecast_id WHERE item.allocation_item_id = NEW.allocation_item_id; IF expected_allocation IS NULL OR NEW.allocation_id IS DISTINCT FROM expected_allocation THEN RAISE EXCEPTION 'Phase 4 attribution item does not match allocation lineage'; END IF; IF NEW.pnl_status = 'realized' AND (NEW.paper_execution_observation_id IS NULL OR NOT EXISTS ( SELECT 1 FROM app.paper_execution_observation observation JOIN app.paper_order paper ON paper.id = observation.paper_order_id WHERE observation.paper_execution_observation_id = NEW.paper_execution_observation_id AND observation.allocation_item_id = NEW.allocation_item_id AND observation.paper_only AND observation.execution_mode = 'paper' AND observation.filled_quantity > 0 AND observation.fill_price IS NOT NULL AND observation.exit_price IS NOT NULL AND paper.status IN ('exited', 'closed') )) THEN RAISE EXCEPTION 'Phase 4 realized attribution requires a genuine linked paper fill'; END IF; IF NEW.strategy_forecast_id IS DISTINCT FROM expected_forecast OR NEW.hypothesis_id IS DISTINCT FROM expected_hypothesis OR NEW.action_id IS DISTINCT FROM expected_action OR NEW.rank_id IS DISTINCT FROM expected_rank OR NEW.expression IS DISTINCT FROM expected_expression OR NEW.trial_id IS DISTINCT FROM expected_trial OR NEW.result_id IS DISTINCT FROM expected_result THEN RAISE EXCEPTION 'Phase 4 attribution does not match allocation lineage'; END IF; IF NOT EXISTS ( SELECT 1 FROM analysis.ticker_decision decision WHERE decision.input_manifest->'trade_plan'->>'trade_plan_id' = ( SELECT action_id FROM analysis.portfolio_allocation_item WHERE allocation_item_id = NEW.allocation_item_id) AND decision.input_manifest->'trade_plan'->>'rank_id' = ( SELECT rank_id FROM analysis.portfolio_allocation_item WHERE allocation_item_id = NEW.allocation_item_id) AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id AND decision.status = 'published' AND decision.published_at IS NOT NULL ) THEN RAISE EXCEPTION 'Phase 4 attribution requires a published action and rank lineage'; END IF; SELECT decision.experiment_id INTO expected_experiment FROM analysis.ticker_decision decision WHERE decision.input_manifest->'trade_plan'->>'trade_plan_id' = NEW.action_id AND decision.input_manifest->'trade_plan'->>'rank_id' = NEW.rank_id AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = NEW.strategy_forecast_id AND decision.status = 'published' AND decision.published_at IS NOT NULL ORDER BY decision.published_at DESC, decision.id DESC LIMIT 1; IF expected_experiment IS NULL OR NEW.experiment_id IS DISTINCT FROM expected_experiment THEN RAISE EXCEPTION 'Phase 4 attribution experiment lineage is invalid'; END IF; SELECT input_cutoff INTO expected_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id; IF expected_cutoff IS NULL OR NEW.input_cutoff IS DISTINCT FROM expected_cutoff THEN RAISE EXCEPTION 'Phase 4 attribution input cutoff does not match allocation lineage'; END IF; IF NEW.pnl_status = 'realized' THEN SELECT CASE WHEN observation.side = 'buy' THEN 1 ELSE -1 END * (observation.exit_price - observation.fill_price) * observation.filled_quantity - coalesce(paper.fees, 0) INTO expected_realized_pnl FROM app.paper_execution_observation observation JOIN app.paper_order paper ON paper.id = observation.paper_order_id WHERE observation.paper_execution_observation_id = NEW.paper_execution_observation_id; IF expected_realized_pnl IS NULL OR abs(NEW.realized_pnl - expected_realized_pnl) > 0.000000001 THEN RAISE EXCEPTION 'Phase 4 attribution P&L does not match the persisted fill and fee lineage'; END IF; END IF; END IF; RETURN NEW; END; $$;

CREATE FUNCTION analysis.enforce_phase4_paper_execution() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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

CREATE FUNCTION analysis.enforce_phase4_paper_execution_evidence() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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

CREATE FUNCTION analysis.enforce_phase4_paper_execution_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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

CREATE FUNCTION analysis.enforce_phase4_review_item_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$ DECLARE expected_cutoff TIMESTAMPTZ; expected_forecast TEXT; expected_hypothesis UUID; source_key TEXT; source_value JSONB; source_amount DOUBLE PRECISION; source_total DOUBLE PRECISION := 0; source_count INTEGER := 0; authority_account BIGINT; BEGIN SELECT input_cutoff INTO expected_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id; IF expected_cutoff IS NULL THEN RAISE EXCEPTION 'Phase 4 allocation item has no persisted allocation cutoff'; END IF; IF NEW.funding_sources IS NULL OR jsonb_typeof(NEW.funding_sources) IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'Phase 4 funding sources must be an object'; END IF; FOR source_key, source_value IN SELECT key, value FROM jsonb_each(NEW.funding_sources) LOOP source_count := source_count + 1; IF source_key !~ '^(CASH:(broker-account|manual-account):[0-9]+|TRIM:(broker-position|manual-position):[0-9]+)$' OR jsonb_typeof(source_value) IS DISTINCT FROM 'number' THEN RAISE EXCEPTION 'Phase 4 funding source is not a canonical PostgreSQL source: %', source_key; END IF; source_amount := (source_value #>> '{}')::DOUBLE PRECISION; IF source_amount IS NULL OR source_amount <= 0 OR source_amount = 'Infinity'::DOUBLE PRECISION OR source_amount = '-Infinity'::DOUBLE PRECISION THEN RAISE EXCEPTION 'Phase 4 funding source amount is invalid: %', source_key; END IF; source_total := source_total + source_amount; IF (SELECT count(*) FROM jsonb_object_keys(NEW.funding_sources)) = 1 AND NEW.funding_amount IS NOT NULL AND abs(source_amount - NEW.funding_amount) > 0.000000001 THEN RAISE EXCEPTION 'Phase 4 funding sources do not conserve funding_amount'; END IF; IF analysis.phase4_funding_source_capacity( source_key, (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id), expected_cutoff ) IS NULL OR analysis.phase4_funding_source_capacity( source_key, (SELECT metadata->>'authority_snapshot_id' FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id), expected_cutoff ) < source_amount THEN RAISE EXCEPTION 'Phase 4 funding source is unavailable or over-allocated: %', source_key; END IF; END LOOP; IF source_count > 0 AND (NEW.funding_amount IS NULL OR abs(source_total - NEW.funding_amount) > 0.000000001) THEN RAISE EXCEPTION 'Phase 4 funding sources do not conserve funding_amount'; END IF; IF NEW.funding_amount IS NOT NULL AND source_count = 0 AND NEW.disposition IN ('selected', 'rollback') AND NEW.ticker <> 'CASH' AND NEW.target_weight <> NEW.current_weight THEN RAISE EXCEPTION 'Phase 4 funded item must name every source'; END IF; IF source_count = 1 AND NEW.funding_source IS DISTINCT FROM ( SELECT min(source.key) FROM jsonb_object_keys(NEW.funding_sources) AS source(key) ) THEN RAISE EXCEPTION 'Phase 4 single-source display does not match its source map'; END IF; IF source_count > 1 AND NEW.funding_source IS DISTINCT FROM 'MULTI_SOURCE' THEN RAISE EXCEPTION 'Phase 4 multi-source funding must use display marker MULTI_SOURCE'; END IF; IF NEW.disposition = 'selected' AND NEW.ticker <> 'CASH' AND NEW.target_weight > NEW.current_weight AND (NEW.funding_amount IS NULL OR NEW.funding_amount <= 0 OR source_count = 0) THEN RAISE EXCEPTION 'Phase 4 increase requires conserved PostgreSQL funding sources'; END IF; IF NEW.disposition IN ('selected', 'rollback') AND NEW.target_weight < NEW.current_weight THEN IF source_count = 0 OR source_count <> 1 OR NEW.funding_source IS NULL OR NEW.funding_source !~ '^TRIM:broker-position:[0-9]+$' OR NEW.trace->>'trim_position_id' IS DISTINCT FROM split_part(NEW.funding_source, ':', 2) || ':' || split_part(NEW.funding_source, ':', 3) THEN RAISE EXCEPTION 'Phase 4 trim release is not bound to one persisted position'; END IF; END IF; NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object( 'allocation_item_id', NEW.allocation_item_id, 'allocation_id', NEW.allocation_id, 'candidate_id', NEW.candidate_id, 'ticker', NEW.ticker, 'strategy_forecast_id', NEW.strategy_forecast_id, 'action_id', NEW.action_id, 'rank_id', NEW.rank_id, 'hypothesis_id', NEW.hypothesis_id, 'disposition', NEW.disposition, 'target_weight', NEW.target_weight, 'current_weight', NEW.current_weight, 'marginal_book_utility', NEW.marginal_book_utility, 'trace', NEW.trace, 'blockers', NEW.blockers, 'funding_source', NEW.funding_source, 'funding_amount', NEW.funding_amount, 'funding_sources', NEW.funding_sources)); IF NEW.strategy_forecast_id IS NOT NULL THEN SELECT forecast.input_cutoff, forecast.id, revision.hypothesis_id INTO expected_cutoff, expected_forecast, expected_hypothesis FROM analysis.strategy_forecast forecast JOIN analysis.strategy_revision revision ON revision.id = forecast.strategy_revision_id WHERE forecast.id = NEW.strategy_forecast_id; IF expected_forecast IS NULL OR expected_cutoff > (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id) OR NEW.hypothesis_id IS DISTINCT FROM expected_hypothesis THEN RAISE EXCEPTION 'Phase 4 allocation forecast lineage is invalid'; END IF; END IF; RETURN NEW; END; $_$;

CREATE FUNCTION analysis.enforce_phase4_review_snapshot_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $_$
        BEGIN
          IF coalesce(NEW.metadata->>'authority', '') <> 'postgresql'
             OR NEW.metadata->>'authority_snapshot_id' !~ '^(broker-account|manual-account):[0-9]+$'
             OR jsonb_typeof(NEW.metadata->'source_hashes') IS DISTINCT FROM 'array'
             OR (NEW.status <> 'cash_only' AND jsonb_array_length(NEW.metadata->'source_hashes') = 0)
             OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'source_hashes') hash
                        WHERE hash !~ '^[0-9a-f]{64}$' OR hash = repeat('0', 64))
             OR NOT analysis.phase4_account_authority_exists(
                  NEW.metadata->>'authority_snapshot_id', NEW.input_cutoff)
          THEN
            RAISE EXCEPTION 'Phase 4 allocation requires PostgreSQL authority evidence';
          END IF;
          NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
            'allocation_id', NEW.allocation_id, 'as_of', analysis.phase4_canonical_timestamp(NEW.as_of),
            'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff), 'status', NEW.status,
            'cash_hurdle', NEW.cash_hurdle, 'forecast_ids', NEW.forecast_ids, 'action_ids', NEW.action_ids,
            'strategy_registry_ids', NEW.strategy_registry_ids, 'metadata', NEW.metadata));
          RETURN NEW;
        END;
        $_$;

CREATE FUNCTION analysis.enforce_phase4_source_lineage() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
