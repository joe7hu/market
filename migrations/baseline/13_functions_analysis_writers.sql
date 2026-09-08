-- Analysis functions: writers, hashes, and authorization helpers.

CREATE FUNCTION analysis.insert_phase4_allocation_item(p jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'analysis', 'raw', 'public'
    AS $$
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

CREATE FUNCTION analysis.insert_phase4_allocation_snapshot(p jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'analysis', 'raw', 'public'
    AS $$
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

CREATE FUNCTION analysis.insert_phase4_book_attribution(p jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'analysis', 'app', 'public'
    AS $$
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

CREATE FUNCTION analysis.insert_phase4_execution(p_id text, p_allocation_id text, p_model_version text, p_calibration_status text, p_sample_count integer, p_fill_probability double precision, p_spread_bps double precision, p_latency_ms double precision, p_impact_bps double precision, p_input_cutoff timestamp with time zone, p_input_hash text, p_content_hash text, p_metadata jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'analysis', 'app', 'public'
    AS $$
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

CREATE FUNCTION analysis.insert_phase4_paper_execution_observation(p jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'analysis', 'app', 'public'
    AS $$
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

CREATE FUNCTION analysis.insert_phase4_scenario(p_id text, p_allocation_id text, p_model_version text, p_probability_semantics text, p_scenarios jsonb, p_tail_dependence jsonb, p_simultaneous_unwind jsonb, p_input_cutoff timestamp with time zone, p_input_hash text, p_content_hash text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'analysis', 'app', 'public'
    AS $$
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

CREATE FUNCTION analysis.phase3_factor_evidence_valid(result_uuid uuid, result_hash text, evidence_cutoff timestamp with time zone) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$
            SELECT EXISTS (
                SELECT 1
                  FROM analysis.trial_result result
                  JOIN analysis.research_evaluator_output factor_output
                    ON factor_output.trial_result_id = result.id
                   AND factor_output.research_trial_id = result.research_trial_id
                   AND factor_output.evidence_kind = 'neutralization'
                 WHERE result.id = result_uuid
                   AND result.input_hash::TEXT = result_hash
                   AND factor_output.available_at <= evidence_cutoff
                   AND factor_output.raw_output->>'producer' = 'postgresql'
                   AND factor_output.raw_output->>'trial_result_id' = result.id::TEXT
                   AND factor_output.raw_output->>'result_hash' = result.input_hash::TEXT
                   AND jsonb_typeof(factor_output.raw_output->'factor_exposure') = 'object'
                   AND factor_output.raw_output->'factor_exposure' <> '{}'::jsonb
                   AND NOT EXISTS (
                       SELECT 1 FROM jsonb_each(factor_output.raw_output->'factor_exposure') factor
                       WHERE jsonb_typeof(factor.value) <> 'number'
                   )
                   AND jsonb_typeof(factor_output.raw_output->'neutralized_result') = 'object'
                   AND jsonb_typeof(factor_output.raw_output->'neutralized') = 'boolean'
                   AND (factor_output.raw_output->>'neutralized')::BOOLEAN IS TRUE
                   AND factor_output.raw_output->>'factor_exposure_hash' = analysis.phase3_json_hash(factor_output.raw_output->'factor_exposure')
                   AND factor_output.raw_output->>'neutralized_result_hash' = analysis.phase3_json_hash(factor_output.raw_output->'neutralized_result')
            )
        $$;

CREATE FUNCTION analysis.phase3_json_hash(payload jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT
    AS $$
            SELECT encode(public.digest(convert_to(payload::text, 'UTF8'), 'sha256'), 'hex')
        $$;

CREATE FUNCTION analysis.phase4_account_authority_exists(authority_id text, cutoff timestamp with time zone) RETURNS boolean
    LANGUAGE plpgsql STABLE
    AS $_$
BEGIN
  IF authority_id ~ '^broker-account:[0-9]+$' THEN
    RETURN EXISTS (
      SELECT 1 FROM raw.broker_account_snapshot account
       WHERE ('broker-account:' || account.id::TEXT) = authority_id
         AND account.observed_at <= cutoff
    );
  ELSIF authority_id ~ '^manual-account:[0-9]+$' THEN
    RETURN EXISTS (
      SELECT 1 FROM app.manual_account_snapshot account
       WHERE ('manual-account:' || account.id::TEXT) = authority_id
         AND account.reconciliation_state = 'reconciled'
         AND account.effective_at <= cutoff
         AND account.recorded_at <= cutoff
    );
  END IF;
  RETURN FALSE;
END;
$_$;

CREATE FUNCTION analysis.phase4_allocation_authorization_payload(p_snapshot jsonb, p_items jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
          SELECT analysis.phase4_canonical_json(jsonb_build_object(
            'contract', 'phase4-allocation-writer.v1', 'snapshot', p_snapshot, 'items', p_items))
        $$;

CREATE FUNCTION analysis.phase4_allocation_signing_key() RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
          SELECT convert_from(secret, 'UTF8') FROM analysis.phase4_allocation_signing_secret WHERE singleton
        $$;

CREATE FUNCTION analysis.phase4_canonical_json(payload jsonb) RETURNS text
    LANGUAGE plpgsql IMMUTABLE STRICT
    AS $$
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

CREATE FUNCTION analysis.phase4_canonical_timestamp(value timestamp with time zone) RETURNS jsonb
    LANGUAGE sql IMMUTABLE STRICT
    AS $$
            SELECT to_jsonb(value AT TIME ZONE 'UTC')
        $$;

CREATE FUNCTION analysis.phase4_content_digest(payload jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT
    AS $$
            SELECT encode(public.digest(convert_to(analysis.phase4_canonical_json(payload), 'UTF8'), 'sha256'), 'hex')
        $$;

CREATE FUNCTION analysis.phase4_funding_source_capacity(source_key text, authority_id text, cutoff timestamp with time zone) RETURNS double precision
    LANGUAGE plpgsql STABLE
    AS $_$
DECLARE
  capacity DOUBLE PRECISION;
  snapshot_effective_at TIMESTAMPTZ;
  cash_change DOUBLE PRECISION;
BEGIN
  IF NOT analysis.phase4_account_authority_exists(authority_id, cutoff) THEN
    RETURN NULL;
  END IF;
  IF source_key = 'CASH:' || authority_id THEN
    IF authority_id ~ '^broker-account:[0-9]+$' THEN
      SELECT account.cash_balance::DOUBLE PRECISION INTO capacity
        FROM raw.broker_account_snapshot account
       WHERE ('broker-account:' || account.id::TEXT) = authority_id
         AND account.observed_at <= cutoff;
      RETURN capacity;
    END IF;
    SELECT account.cash_balance::DOUBLE PRECISION, account.effective_at
      INTO capacity, snapshot_effective_at
      FROM app.manual_account_snapshot account
     WHERE ('manual-account:' || account.id::TEXT) = authority_id
       AND account.reconciliation_state = 'reconciled'
       AND account.effective_at <= cutoff
       AND account.recorded_at <= cutoff;
    IF capacity IS NULL OR snapshot_effective_at IS NULL THEN
      RETURN NULL;
    END IF;
    SELECT COALESCE(SUM(
        CASE
          WHEN transaction.transaction_type IN ('cash_deposit', 'dividend', 'sell')
            THEN COALESCE(transaction.amount, 0) - COALESCE(transaction.fees, 0)
          WHEN transaction.transaction_type IN ('cash_withdrawal', 'fee', 'buy')
            THEN -(COALESCE(transaction.amount, 0) + COALESCE(transaction.fees, 0))
          ELSE 0
        END
      ), 0)
      INTO cash_change
      FROM app.portfolio_transaction transaction
     WHERE transaction.executed_at > snapshot_effective_at
       AND transaction.executed_at <= cutoff
       AND transaction.created_at <= cutoff
       AND transaction.reverses_transaction_id IS NULL
       AND NOT EXISTS (
         SELECT 1 FROM app.portfolio_transaction reversal
          WHERE reversal.reverses_transaction_id = transaction.id
            AND reversal.executed_at <= cutoff
            AND reversal.created_at <= cutoff
       );
    capacity := capacity + cash_change;
    SELECT COALESCE(SUM(
        CASE
          WHEN original.transaction_type IN ('cash_deposit', 'dividend', 'sell')
            THEN COALESCE(original.amount, 0) - COALESCE(original.fees, 0)
          WHEN original.transaction_type IN ('cash_withdrawal', 'fee', 'buy')
            THEN -(COALESCE(original.amount, 0) + COALESCE(original.fees, 0))
          ELSE 0
        END
      ), 0)
      INTO cash_change
      FROM app.portfolio_transaction original
      JOIN app.portfolio_transaction reversal
        ON reversal.reverses_transaction_id = original.id
     WHERE original.reverses_transaction_id IS NULL
       AND original.executed_at <= snapshot_effective_at
       AND original.created_at <= cutoff
       AND reversal.executed_at > snapshot_effective_at
       AND reversal.executed_at <= cutoff
       AND reversal.created_at <= cutoff;
    RETURN capacity - cash_change;
  END IF;
  IF source_key ~ '^TRIM:broker-position:[0-9]+$' THEN
    SELECT abs(position.market_value)::DOUBLE PRECISION INTO capacity
      FROM raw.broker_position_snapshot position
      JOIN raw.broker_account_snapshot account ON account.id = position.account_snapshot_id
     WHERE position.id = split_part(source_key, ':', 3)::BIGINT
       AND ('broker-account:' || account.id::TEXT) = authority_id
       AND account.observed_at <= cutoff
       AND position.quantity > 0 AND position.market_value IS NOT NULL;
    RETURN capacity;
  END IF;
  IF source_key ~ '^TRIM:manual-position:[0-9]+$' THEN
    SELECT position.quantity::DOUBLE PRECISION * quote.price::DOUBLE PRECISION INTO capacity
      FROM app.portfolio_position position
      LEFT JOIN LATERAL (
        SELECT price FROM raw.current_price_at(cutoff, ARRAY[position.instrument_id]::BIGINT[])
        LIMIT 1
      ) quote ON TRUE
     WHERE position.instrument_id = split_part(source_key, ':', 3)::BIGINT
       AND position.quantity > 0
       AND NOT EXISTS (
         SELECT 1 FROM app.portfolio_transaction transaction
          WHERE transaction.instrument_id = position.instrument_id
            AND (transaction.executed_at > cutoff OR transaction.created_at > cutoff)
       );
    RETURN capacity;
  END IF;
  RETURN NULL;
END;
$_$;

CREATE FUNCTION analysis.phase4_telemetry_authorization_payload(p_contract text, p_payload jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
        SELECT analysis.phase4_canonical_json(jsonb_build_object('contract', p_contract, 'payload', p_payload))
      $$;

CREATE FUNCTION analysis.phase4_telemetry_authorized(p_contract text, p_payload jsonb, p_signature text) RETURNS boolean
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
      DECLARE key TEXT;
      BEGIN
        key := analysis.phase4_allocation_signing_key();
        RETURN key IS NOT NULL AND length(key) >= 16 AND p_signature = encode(public.hmac(
          convert_to(analysis.phase4_telemetry_authorization_payload(p_contract, p_payload), 'UTF8'),
          convert_to(key, 'UTF8'), 'sha256'), 'hex');
      END $$;

CREATE FUNCTION analysis.promote_phase3_strategy(revision_id bigint) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM analysis.strategy_revision
                 WHERE id = revision_id AND p3_enabled
            ) THEN
                RAISE EXCEPTION 'unknown or non-Phase 3 strategy revision: %', revision_id;
            END IF;
            PERFORM set_config('analysis.phase3_transition', 'canonical', true);
            UPDATE analysis.strategy_revision
               SET status = 'active', promoted_at = clock_timestamp()
             WHERE id = revision_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'unknown Phase 3 strategy revision: %', revision_id;
            END IF;
        END;
        $$;

CREATE FUNCTION analysis.reject_phase2_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN RAISE EXCEPTION 'Phase 2 artifacts are immutable'; END; $$;

CREATE FUNCTION analysis.reject_phase4_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'Phase 4 artifacts are immutable';
        END;
        $$;

CREATE FUNCTION analysis.research_check_complete(checks jsonb, name text) RETURNS boolean
    LANGUAGE plpgsql STABLE
    AS $$
        DECLARE item JSONB := checks -> name;
        BEGIN
            IF name = 'multiple_testing' THEN
                RETURN jsonb_typeof(item) = 'object'
                   AND item->>'domain_valid' = 'true'
                   AND jsonb_typeof(item->'sample_size') = 'number'
                   AND (item->>'sample_size')::INTEGER > 0
                   AND jsonb_typeof(item->'path_count') = 'number'
                   AND (item->>'path_count')::INTEGER > 0
                   AND jsonb_typeof(item->'p_value_count') = 'number'
                   AND (item->>'p_value_count')::INTEGER > 0
                   AND jsonb_typeof(item->'trials_tested') = 'number'
                   AND (item->>'trials_tested')::INTEGER > 0
                   AND (item->>'trials_tested')::INTEGER <= 10000
                   AND (item->>'psr')::DOUBLE PRECISION BETWEEN 0 AND 1
                   AND (item->>'dsr')::DOUBLE PRECISION BETWEEN 0 AND 1
                   AND (item->>'pbo')::DOUBLE PRECISION BETWEEN 0 AND 1
                   AND (item->>'data_snooping_probability')::DOUBLE PRECISION BETWEEN 0 AND 1
                   AND (item->>'fdr_q_value')::DOUBLE PRECISION BETWEEN 0 AND 1;
            END IF;
            IF jsonb_typeof(item) <> 'object'
               OR item->>'passed' <> 'true'
               OR item->>'domain_valid' <> 'true' THEN
                RETURN false;
            END IF;
            IF name = 'pit' THEN
                RETURN jsonb_typeof(item->'future_count') = 'number'
                   AND (item->>'future_count')::INTEGER = 0
                   AND jsonb_typeof(item->'observed_count') = 'number'
                   AND (item->>'observed_count')::INTEGER > 0;
            ELSIF name = 'denominator' THEN
                RETURN jsonb_typeof(item->'expected_count') = 'number'
                   AND jsonb_typeof(item->'observed_count') = 'number'
                   AND (item->>'expected_count')::INTEGER > 0
                   AND (item->>'expected_count')::INTEGER = (item->>'observed_count')::INTEGER;
            ELSIF name = 'attempt_manifest' THEN
                RETURN jsonb_typeof(item->'expected_count') = 'number'
                   AND jsonb_typeof(item->'completed_count') = 'number'
                   AND (item->>'expected_count')::INTEGER > 0
                   AND (item->>'expected_count')::INTEGER = (item->>'completed_count')::INTEGER;
            ELSIF name = 'negative_controls' THEN
                RETURN item->>'controls_present' = 'true'
                   AND jsonb_typeof(item->'randomized_sample_count') = 'number'
                   AND jsonb_typeof(item->'white_noise_sample_count') = 'number'
                   AND (item->>'randomized_sample_count')::INTEGER > 0
                   AND (item->>'white_noise_sample_count')::INTEGER > 0;
            ELSIF name = 'mechanism' THEN
                RETURN length(trim(item->>'mechanism_class')) > 0
                   AND length(trim(item->>'falsification_rule')) > 0
                   AND jsonb_typeof(item->'evidence_count') = 'number'
                   AND (item->>'evidence_count')::INTEGER > 0;
            ELSIF name = 'parameter_stability' THEN
                RETURN jsonb_typeof(item->'sample_size') = 'number'
                   AND (item->>'sample_size')::INTEGER >= 3;
            ELSIF name = 'neutralization' THEN
                RETURN item->>'result_exists' = 'true'
                   AND jsonb_typeof(item->'sample_size') = 'number'
                   AND (item->>'sample_size')::INTEGER > 0;
            ELSIF name = 'combinatorial_paths' THEN
                RETURN analysis.research_combinatorial_paths_complete(item);
            ELSIF name = 'robustness' THEN
                RETURN jsonb_typeof(item->'negative_controls') = 'object'
                   AND item->'negative_controls'->>'passed' = 'true'
                   AND item->'negative_controls'->>'domain_valid' = 'true'
                   AND item->'negative_controls'->>'controls_present' = 'true'
                   AND jsonb_typeof(item->'negative_controls'->'randomized_sample_count') = 'number'
                   AND jsonb_typeof(item->'negative_controls'->'white_noise_sample_count') = 'number'
                   AND (item->'negative_controls'->>'randomized_sample_count')::INTEGER > 0
                   AND (item->'negative_controls'->>'white_noise_sample_count')::INTEGER > 0
                   AND jsonb_typeof(item->'parameter_stability') = 'object'
                   AND item->'parameter_stability'->>'passed' = 'true'
                   AND item->'parameter_stability'->>'domain_valid' = 'true'
                   AND (item->'parameter_stability'->>'sample_size')::INTEGER >= 3
                   AND jsonb_typeof(item->'combinatorial_paths') = 'object'
                   AND analysis.research_combinatorial_paths_complete(item->'combinatorial_paths');
            ELSIF name = 'predictive' THEN
                RETURN jsonb_typeof(item->'metrics') = 'object'
                   AND item->'metrics'->>'domain_valid' = 'true'
                   AND analysis.research_check_complete(jsonb_build_object('multiple_testing', item->'metrics'), 'multiple_testing');
            ELSIF name = 'cost_capacity' THEN
                RETURN jsonb_typeof(item->'multiples') = 'object'
                   AND (item->'multiples') ?& ARRAY['1x', '2x', '3x']
                   AND (item->'multiples'->'1x'->>'net_return') IS NOT NULL
                   AND (item->'multiples'->'2x'->>'net_return') IS NOT NULL
                   AND (item->'multiples'->'3x'->>'net_return') IS NOT NULL
                   AND jsonb_typeof(item->'multiples'->'1x'->'net_return') = 'number'
                   AND jsonb_typeof(item->'multiples'->'2x'->'net_return') = 'number'
                   AND jsonb_typeof(item->'multiples'->'3x'->'net_return') = 'number'
                   AND jsonb_typeof(item->'multiples'->'1x'->'capacity') = 'number'
                   AND jsonb_typeof(item->'multiples'->'2x'->'capacity') = 'number'
                   AND jsonb_typeof(item->'multiples'->'3x'->'capacity') = 'number';
            END IF;
            RETURN false;
        END;
        $$;

CREATE FUNCTION analysis.research_combinatorial_paths_complete(item jsonb) RETURNS boolean
    LANGUAGE plpgsql STABLE
    AS $_$
        DECLARE
            record JSONB; path_id TEXT; seen_ids TEXT[] := ARRAY[]::TEXT[];
            test_folds JSONB; train_folds JSONB; metrics JSONB;
            path_count INTEGER := 0; expected_count INTEGER;
        BEGIN
            IF jsonb_typeof(item) <> 'object'
               OR jsonb_typeof(item->'path_count') <> 'number'
               OR (item->>'path_count')::INTEGER <= 0
               OR jsonb_typeof(item->'path_records') <> 'array'
            THEN
                RETURN false;
            END IF;
            expected_count := (item->>'path_count')::INTEGER;
            FOR record IN SELECT value FROM jsonb_array_elements(item->'path_records') LOOP
                path_id := NULLIF(trim(record->>'path_id'), '');
                test_folds := record->'test_folds';
                train_folds := record->'train_folds';
                metrics := record->'metrics';
                IF path_id IS NULL OR path_id = ANY(seen_ids)
                   OR jsonb_typeof(test_folds) <> 'array'
                   OR jsonb_typeof(train_folds) <> 'array'
                   OR jsonb_array_length(test_folds) = 0
                   OR jsonb_array_length(train_folds) = 0
                   OR jsonb_array_length(test_folds) <> (SELECT count(DISTINCT value) FROM jsonb_array_elements(test_folds))
                   OR jsonb_array_length(train_folds) <> (SELECT count(DISTINCT value) FROM jsonb_array_elements(train_folds))
                   OR EXISTS (
                       SELECT 1 FROM jsonb_array_elements_text(test_folds) test_fold
                       JOIN jsonb_array_elements_text(train_folds) train_fold ON train_fold.value = test_fold.value
                   )
                   OR EXISTS (
                       SELECT 1 FROM jsonb_array_elements_text(test_folds) fold
                       WHERE fold.value !~ '^[0-9]+$'
                   )
                   OR EXISTS (
                       SELECT 1 FROM jsonb_array_elements_text(train_folds) fold
                       WHERE fold.value !~ '^[0-9]+$'
                   )
                   OR jsonb_typeof(metrics) <> 'object'
                   OR metrics->>'domain_valid' <> 'true'
                   OR jsonb_typeof(metrics->'sample_size') <> 'number'
                   OR (metrics->>'sample_size')::INTEGER <= 0
                   OR jsonb_typeof(metrics->'fit_train_count') <> 'number'
                   OR (metrics->>'fit_train_count')::INTEGER <= 0
                   OR jsonb_typeof(metrics->'evaluated_test_count') <> 'number'
                   OR (metrics->>'evaluated_test_count')::INTEGER <= 0
                   OR jsonb_typeof(metrics->'mean_return') <> 'number'
                   OR jsonb_typeof(metrics->'psr') <> 'number'
                   OR (metrics->>'psr')::DOUBLE PRECISION NOT BETWEEN 0 AND 1
                   OR jsonb_typeof(metrics->'p_value') <> 'number'
                   OR (metrics->>'p_value')::DOUBLE PRECISION NOT BETWEEN 0 AND 1
                THEN
                    RETURN false;
                END IF;
                seen_ids := array_append(seen_ids, path_id);
                path_count := path_count + 1;
            END LOOP;
            RETURN path_count = expected_count;
        END;
        $_$;

CREATE FUNCTION analysis.research_evaluator_authorization_payload(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
            SELECT concat_ws(chr(31),
                'research-evaluator-authorization.v1', trial_id::TEXT,
                result_id::TEXT, run_id::TEXT, kind, evaluator, code_version,
                input_digest, universe_digest, feature_digest, samples::TEXT,
                CASE WHEN valid THEN 'true' ELSE 'false' END, output::TEXT
            );
        $$;

CREATE FUNCTION analysis.research_evaluator_output_hash(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
            SELECT encode(digest(jsonb_build_object(
                'contract', 'research-evaluator-output.v1',
                'research_trial_id', trial_id::TEXT,
                'trial_result_id', result_id::TEXT,
                'analysis_run_id', run_id::TEXT,
                'evidence_kind', kind,
                'evaluator_id', evaluator,
                'evaluator_code_version', code_version,
                'input_hash', input_digest,
                'universe_hash', universe_digest,
                'feature_hash', feature_digest,
                'sample_count', samples,
                'domain_valid', valid,
                'raw_output', output
            )::TEXT, 'sha256'), 'hex');
        $$;

CREATE FUNCTION analysis.research_evaluator_output_hash_v2(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb, available timestamp with time zone) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
            SELECT encode(public.digest(jsonb_build_object(
                'contract', 'research-evaluator-output.v2',
                'research_trial_id', trial_id::TEXT,
                'trial_result_id', result_id::TEXT,
                'analysis_run_id', run_id::TEXT,
                'evidence_kind', kind,
                'evaluator_id', evaluator,
                'evaluator_code_version', code_version,
                'input_hash', input_digest,
                'universe_hash', universe_digest,
                'feature_hash', feature_digest,
                'sample_count', samples,
                'domain_valid', valid,
                'raw_output', output,
                'available_at', to_char(available AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || 'Z'
            )::TEXT, 'sha256'), 'hex');
        $$;

CREATE FUNCTION analysis.research_evaluator_signature_payload(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output_digest text, available timestamp with time zone) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
            SELECT concat_ws(chr(31),
                'research-evaluator-signature.v1', trial_id::TEXT, result_id::TEXT,
                run_id::TEXT, kind, evaluator, code_version, input_digest,
                universe_digest, feature_digest, samples::TEXT,
                CASE WHEN valid THEN 'true' ELSE 'false' END, output_digest,
                to_char(available AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || 'Z'
            );
        $$;

CREATE FUNCTION analysis.research_evaluator_signing_key() RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
            SELECT convert_from(secret, 'UTF8')
            FROM analysis.research_evaluator_signing_secret
            WHERE singleton
        $$;

CREATE FUNCTION analysis.research_evidence_complete(result_uuid uuid) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
            SELECT count(*) = 6 AND bool_and(
                manifest.evaluator_output_id IS NOT NULL
                AND source.id IS NOT NULL
                AND source.output_hash = manifest.evidence_hash
                AND source.output_hash = analysis.research_evaluator_output_hash_v2(
                    source.research_trial_id, source.trial_result_id,
                    source.analysis_run_id, source.evidence_kind,
                    source.evaluator_id, source.evaluator_code_version,
                    source.input_hash, source.universe_hash, source.feature_hash,
                    source.sample_count, source.domain_valid, source.raw_output,
                    source.available_at
                )
                AND source.signature = encode(public.hmac(
                    convert_to(analysis.research_evaluator_signature_payload(
                        source.research_trial_id, source.trial_result_id,
                        source.analysis_run_id, source.evidence_kind,
                        source.evaluator_id, source.evaluator_code_version,
                        source.input_hash, source.universe_hash, source.feature_hash,
                        source.sample_count, source.domain_valid, source.output_hash,
                        source.available_at
                    ), 'UTF8'), convert_to(analysis.research_evaluator_signing_key(), 'UTF8'), 'sha256'::TEXT
                ), 'hex')
                AND manifest.payload = source.raw_output
                AND source.trial_result_id = result_uuid
                AND source.domain_valid AND source.sample_count > 0
            )
            FROM analysis.research_evidence_manifest manifest
            LEFT JOIN analysis.research_evaluator_output source ON source.id = manifest.evaluator_output_id
            WHERE manifest.trial_result_id = result_uuid;
        $$;

CREATE FUNCTION analysis.research_evidence_hash(trial_id uuid, result_id uuid, kind text, evaluator text, samples integer, valid boolean, evidence jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE
    AS $$
            SELECT encode(digest(jsonb_build_object(
                'contract', 'research-evidence.v1',
                'research_trial_id', trial_id::TEXT,
                'trial_result_id', result_id::TEXT,
                'evidence_kind', kind,
                'evaluator_id', evaluator,
                'sample_count', samples,
                'domain_valid', valid,
                'payload', evidence
            )::TEXT, 'sha256'), 'hex');
        $$;

CREATE FUNCTION analysis.research_family_complete(family_id uuid) RETURNS boolean
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
            SELECT EXISTS (
                SELECT 1 FROM analysis.experiment_manifest manifest
                WHERE manifest.experiment_family_id = family_id
                  AND lower(manifest.manifest_hash) = encode(
                      public.digest(convert_to(replace(manifest.expected_trial_keys::text, ' ', ''), 'UTF8'), 'sha256'), 'hex'
                  )
                  AND manifest.expected_trial_count = jsonb_array_length(manifest.expected_trial_keys)
                  AND manifest.expected_trial_count = (
                      SELECT count(DISTINCT expected.key)
                      FROM jsonb_array_elements_text(manifest.expected_trial_keys) expected(key)
                  )
                  AND manifest.expected_trial_count = (
                      SELECT count(*) FROM analysis.research_trial trial
                      WHERE trial.experiment_family_id = family_id
                  )
                  AND manifest.expected_trial_keys = COALESCE((
                      SELECT jsonb_agg(to_jsonb(trial.trial_key) ORDER BY trial.trial_key)
                      FROM analysis.research_trial trial
                      WHERE trial.experiment_family_id = family_id
                  ), '[]'::jsonb)
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis.research_trial trial
                      WHERE trial.experiment_family_id = family_id
                        AND (trial.status = 'running' OR trial.finished_at IS NULL OR NOT EXISTS (
                            SELECT 1 FROM analysis.trial_result result
                            WHERE result.research_trial_id = trial.id
                              AND result.result_kind = 'validation'
                              AND result.outcome ? 'passed'
                        ))
                  )
            );
        $$;

CREATE FUNCTION analysis.research_gate_evidence_complete(dossier_uuid uuid, result_uuid uuid) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$
            SELECT count(*) = 5 AND bool_and(
                gate.verdict = 'pass'
                AND gate.metrics->>'passed' = 'true'
                AND gate.metrics->>'domain_valid' = 'true'
                AND gate.metrics->>'validation_result_id' = result_uuid::TEXT
                AND gate.metrics->>'validation_result_input_hash' = result.input_hash
                AND gate.evidence->>'trial_result_id' = result_uuid::TEXT
                AND gate.evidence->>'input_hash' = result.input_hash
                AND gate.metrics->'evidence_manifest_hashes' = (
                    SELECT jsonb_agg(evidence_hash ORDER BY evidence_kind)
                    FROM analysis.research_evidence_manifest
                    WHERE trial_result_id = result_uuid
                )
                AND jsonb_typeof(gate.metrics->'checks') = 'object'
                AND gate.metrics->'checks' <> '{}'::JSONB
                AND CASE gate.gate_code
                    WHEN 'pit_integrity' THEN gate.metrics->'checks'->'pit' = result.outcome->'checks'->'pit'
                    WHEN 'denominator_completeness' THEN gate.metrics->'checks'->'denominator' = result.outcome->'checks'->'denominator'
                        AND gate.metrics->'checks'->'attempt_manifest' = result.outcome->'checks'->'attempt_manifest'
                    WHEN 'oos_predictive_validity' THEN gate.metrics->'checks'->'predictive' = result.outcome->'checks'->'predictive'
                        AND gate.metrics->'checks'->'multiple_testing' = result.outcome->'checks'->'multiple_testing'
                    WHEN 'falsification_and_robustness' THEN gate.metrics->'checks'->'mechanism' = result.outcome->'checks'->'mechanism'
                        AND gate.metrics->'checks'->'negative_controls' = result.outcome->'checks'->'negative_controls'
                        AND gate.metrics->'checks'->'parameter_stability' = result.outcome->'checks'->'parameter_stability'
                        AND gate.metrics->'checks'->'neutralization' = result.outcome->'checks'->'neutralization'
                        AND gate.metrics->'checks'->'combinatorial_paths' = result.outcome->'checks'->'combinatorial_paths'
                        AND gate.metrics->'checks'->'robustness' = result.outcome->'checks'->'robustness'
                    WHEN 'economic_promotability' THEN gate.metrics->'checks'->'cost_capacity' = result.outcome->'checks'->'cost_capacity'
                        AND gate.metrics->'checks'->'neutralization' = result.outcome->'checks'->'neutralization'
                    ELSE false
                END
            )
            FROM analysis.validation_gate_result gate
            JOIN analysis.trial_result result ON result.id = result_uuid
            JOIN analysis.research_trial trial ON trial.id = result.research_trial_id
            WHERE gate.dossier_id = dossier_uuid;
        $$;

CREATE FUNCTION analysis.research_trial_p3_denominator_complete(trial_uuid uuid) RETURNS boolean
    LANGUAGE sql STABLE
    AS $$
            SELECT analysis.research_trial_universe_complete(trial_uuid)
               AND NOT EXISTS (
                   SELECT 1
                   FROM analysis.research_trial trial
                   JOIN analysis.trial_universe_manifest manifest
                     ON manifest.research_trial_id = trial.id
                   WHERE trial.id = trial_uuid
                     AND (trial.available_at > trial.input_cutoff
                          OR manifest.available_at > trial.input_cutoff)
               )
               AND NOT EXISTS (
                   SELECT 1 FROM analysis.universe_observation observation
                   JOIN analysis.research_trial trial ON trial.id = observation.research_trial_id
                   WHERE observation.research_trial_id = trial_uuid
                     AND (observation.observed_at > trial.input_cutoff
                          OR observation.available_at > trial.input_cutoff
                          OR jsonb_typeof(observation.outcome) <> 'object'
                          OR observation.outcome = '{}'::jsonb)
               )
               AND NOT EXISTS (
                   SELECT 1 FROM analysis.trial_result result
                   JOIN analysis.research_trial trial ON trial.id = result.research_trial_id
                   WHERE result.research_trial_id = trial_uuid
                     AND result.available_at > trial.input_cutoff
               )
        $$;

CREATE FUNCTION analysis.research_trial_universe_complete(trial_id uuid) RETURNS boolean
    LANGUAGE sql STABLE
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
            SELECT EXISTS (
                SELECT 1
                FROM analysis.trial_universe_manifest manifest
                JOIN analysis.research_trial trial ON trial.id = manifest.research_trial_id
                WHERE manifest.research_trial_id = trial_id
                  AND manifest.cutoff = trial.input_cutoff
                  AND manifest.expected_member_count > 0
                  AND manifest.expected_member_count = jsonb_array_length(manifest.expected_members)
                  AND manifest.expected_member_count = (
                      SELECT count(DISTINCT expected.member)
                      FROM jsonb_array_elements_text(manifest.expected_members) expected(member)
                  )
                  AND lower(manifest.manifest_hash) = encode(
                      public.digest(convert_to(replace(manifest.expected_members::text, ' ', ''), 'UTF8'), 'sha256'), 'hex'
                  )
                  AND manifest.expected_member_count = (
                      SELECT count(*) FROM analysis.universe_observation observation
                      WHERE observation.research_trial_id = trial_id
                        AND observation.cutoff = trial.input_cutoff
                        AND observation.input_hash = trial.input_hash
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis.universe_observation observation
                      WHERE observation.research_trial_id = trial_id
                        AND (observation.cutoff <> trial.input_cutoff
                             OR observation.input_hash <> trial.input_hash)
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM jsonb_array_elements_text(manifest.expected_members) expected(member)
                      WHERE NOT EXISTS (
                          SELECT 1 FROM analysis.universe_observation observation
                          WHERE observation.research_trial_id = trial_id
                            AND observation.instrument_id::text = expected.member
                      )
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis.universe_observation observation
                      WHERE observation.research_trial_id = trial_id
                        AND NOT EXISTS (
                            SELECT 1 FROM jsonb_array_elements_text(manifest.expected_members) expected(member)
                            WHERE expected.member = observation.instrument_id::text
                        )
                  )
            );
        $$;

CREATE FUNCTION analysis.research_validation_evidence_complete(result_uuid uuid, expected_attempt_count integer) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
            SELECT EXISTS (
                SELECT 1 FROM analysis.trial_result result
                JOIN analysis.research_trial trial ON trial.id = result.research_trial_id
                WHERE result.id = result_uuid AND result.result_kind = 'validation'
                  AND result.input_hash = trial.input_hash
                  AND result.outcome->>'passed' = 'true'
                  AND jsonb_typeof(result.outcome->'checks') = 'object'
                  AND analysis.research_check_complete(result.outcome->'checks', 'pit')
                  AND analysis.research_check_complete(result.outcome->'checks', 'denominator')
                  AND analysis.research_check_complete(result.outcome->'checks', 'attempt_manifest')
                  AND analysis.research_check_complete(result.outcome->'checks', 'negative_controls')
                  AND analysis.research_check_complete(result.outcome->'checks', 'mechanism')
                  AND analysis.research_check_complete(result.outcome->'checks', 'parameter_stability')
                  AND analysis.research_check_complete(result.outcome->'checks', 'neutralization')
                  AND analysis.research_check_complete(result.outcome->'checks', 'combinatorial_paths')
                  AND analysis.research_check_complete(result.outcome->'checks', 'robustness')
                  AND analysis.research_check_complete(result.outcome->'checks', 'predictive')
                  AND analysis.research_check_complete(result.outcome->'checks', 'multiple_testing')
                  AND analysis.research_check_complete(result.outcome->'checks', 'cost_capacity')
                  AND (result.outcome->'checks'->'multiple_testing'->>'trials_tested')::INTEGER = expected_attempt_count
                  AND analysis.research_trial_universe_complete(trial.id)
                  AND analysis.research_family_complete(trial.experiment_family_id)
                  AND analysis.research_evidence_complete(result.id)
            );
        $$;

CREATE FUNCTION analysis.stamp_agent_task_payload_availability() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          IF TG_OP = 'INSERT' THEN
            NEW.result_available_at := CASE WHEN NEW.result IS NULL THEN NULL ELSE now() END;
            NEW.validation_available_at := CASE WHEN NEW.validation IS NULL THEN NULL ELSE now() END;
            RETURN NEW;
          END IF;
          IF OLD.result IS NOT NULL AND NEW.result IS NULL THEN
            RAISE EXCEPTION 'published agent result cannot be cleared';
          END IF;
          IF OLD.validation IS NOT NULL AND NEW.validation IS NULL THEN
            RAISE EXCEPTION 'published agent validation cannot be cleared';
          END IF;
          NEW.result_available_at := CASE
            WHEN OLD.result_available_at IS NOT NULL THEN OLD.result_available_at
            WHEN NEW.result IS NOT NULL THEN now()
            ELSE NULL
          END;
          NEW.validation_available_at := CASE
            WHEN OLD.validation_available_at IS NOT NULL THEN OLD.validation_available_at
            WHEN NEW.validation IS NOT NULL THEN now()
            ELSE NULL
          END;
          RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.write_phase4_allocation(p_snapshot jsonb, p_items jsonb, p_authorization_signature text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'raw', 'public'
    AS $_$
        DECLARE signing_key TEXT; expected_signature TEXT; account_id BIGINT;
                cutoff TIMESTAMPTZ; expected_hashes JSONB; expected_forecasts JSONB; expected_actions JSONB;
        BEGIN
          signing_key := analysis.phase4_allocation_signing_key();
          IF signing_key IS NULL OR length(signing_key) < 16 THEN
            RAISE EXCEPTION 'Phase 4 allocation signing key is not configured';
          END IF;
          expected_signature := encode(public.hmac(
            convert_to(analysis.phase4_allocation_authorization_payload(p_snapshot, p_items), 'UTF8'),
            convert_to(signing_key, 'UTF8'), 'sha256'::TEXT), 'hex');
          IF p_authorization_signature IS NULL OR lower(p_authorization_signature) <> expected_signature THEN
            RAISE EXCEPTION 'Phase 4 allocation authorization signature is invalid';
          END IF;
          IF jsonb_typeof(p_snapshot) <> 'object' OR jsonb_typeof(p_items) <> 'array'
             OR jsonb_array_length(p_items) = 0 OR p_snapshot->>'authority' IS NOT NULL THEN
            RAISE EXCEPTION 'Phase 4 allocation writer payload is malformed';
          END IF;
          cutoff := (p_snapshot->>'input_cutoff')::timestamptz;
          IF p_snapshot->'metadata'->>'authority' <> 'postgresql'
             OR p_snapshot->'metadata'->>'authority_snapshot_id' !~ '^broker-account:[0-9]+$' THEN
            RAISE EXCEPTION 'Phase 4 allocation authority metadata is invalid';
          END IF;
          account_id := split_part(p_snapshot->'metadata'->>'authority_snapshot_id', ':', 2)::BIGINT;
          IF NOT EXISTS (SELECT 1 FROM raw.broker_account_snapshot account
                         WHERE account.id = account_id AND account.observed_at <= cutoff) THEN
            RAISE EXCEPTION 'Phase 4 allocation authority account is not a PIT PostgreSQL row';
          END IF;
          IF EXISTS (SELECT 1 FROM jsonb_array_elements(p_items) item
                     WHERE item->>'allocation_id' IS DISTINCT FROM p_snapshot->>'allocation_id') THEN
            RAISE EXCEPTION 'Phase 4 allocation items cannot be recombined across snapshots';
          END IF;
          IF EXISTS (
            SELECT 1 FROM jsonb_array_elements(p_items) item
             WHERE item->>'ticker' <> 'CASH' AND item->>'disposition' IN ('selected', 'rollback')
               AND NOT EXISTS (
                 SELECT 1 FROM analysis.ticker_decision decision
                 JOIN analysis.strategy_forecast forecast ON forecast.id = item->>'strategy_forecast_id'
                  WHERE decision.id::text = item->'trace'->>'source_decision_id'
                    AND decision.input_hash = item->'trace'->>'source_decision_input_hash'
                    AND decision.status = 'published' AND decision.published_at IS NOT NULL
                    AND decision.input_manifest->'trade_plan'->>'trade_plan_id' = item->>'action_id'
                    AND decision.input_manifest->'trade_plan'->>'rank_id' = item->>'rank_id'
                    AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = item->>'strategy_forecast_id'
                    AND forecast.input_cutoff <= cutoff AND decision.published_at <= cutoff)) THEN
            RAISE EXCEPTION 'Phase 4 allocation source action lineage is invalid';
          END IF;
          SELECT coalesce(jsonb_agg(DISTINCT item->'trace'->>'source_decision_input_hash' ORDER BY item->'trace'->>'source_decision_input_hash'), '[]'::jsonb),
                 coalesce(jsonb_agg(DISTINCT item->>'strategy_forecast_id' ORDER BY item->>'strategy_forecast_id'), '[]'::jsonb),
                 coalesce(jsonb_agg(DISTINCT item->>'action_id' ORDER BY item->>'action_id'), '[]'::jsonb)
            INTO expected_hashes, expected_forecasts, expected_actions
            FROM jsonb_array_elements(p_items) item
           WHERE item->>'ticker' <> 'CASH' AND item->>'disposition' IN ('selected', 'rollback');
          IF (p_snapshot->>'status' <> 'cash_only' AND (
                coalesce(p_snapshot->'metadata'->'source_hashes', '[]'::jsonb) <> expected_hashes
             OR coalesce(p_snapshot->'forecast_ids', '[]'::jsonb) <> expected_forecasts
             OR coalesce(p_snapshot->'action_ids', '[]'::jsonb) <> expected_actions)) THEN
            RAISE EXCEPTION 'Phase 4 allocation source hashes or identities are not canonical';
          END IF;
          INSERT INTO analysis.portfolio_allocation_snapshot
            (allocation_id, as_of, input_cutoff, status, cash_hurdle, forecast_ids, action_ids,
             strategy_registry_ids, input_hash, content_hash, metadata)
          VALUES (p_snapshot->>'allocation_id', (p_snapshot->>'as_of')::timestamptz,
                  cutoff, p_snapshot->>'status', (p_snapshot->>'cash_hurdle')::double precision,
                  p_snapshot->'forecast_ids', p_snapshot->'action_ids', p_snapshot->'strategy_registry_ids',
                  p_snapshot->>'input_hash', p_snapshot->>'content_hash', p_snapshot->'metadata')
          ON CONFLICT (allocation_id) DO NOTHING;
          INSERT INTO analysis.portfolio_allocation_item
            (allocation_item_id, allocation_id, candidate_id, ticker, strategy_forecast_id, action_id, rank_id,
             hypothesis_id, disposition, target_weight, current_weight, marginal_book_utility, trace, blockers,
             funding_source, funding_amount, funding_sources, input_hash, content_hash)
          SELECT item->>'allocation_item_id', item->>'allocation_id', item->>'candidate_id', item->>'ticker',
                 nullif(item->>'strategy_forecast_id', ''), nullif(item->>'action_id', ''), nullif(item->>'rank_id', ''),
                 nullif(item->>'hypothesis_id', '')::uuid, item->>'disposition', (item->>'target_weight')::double precision,
                 (item->>'current_weight')::double precision, (item->>'marginal_book_utility')::double precision,
                 item->'trace', item->'blockers', nullif(item->>'funding_source', ''),
                 (item->>'funding_amount')::double precision, coalesce(item->'funding_sources', '{}'::jsonb),
                 item->>'input_hash', item->>'content_hash'
            FROM jsonb_array_elements(p_items) item
          ON CONFLICT (allocation_item_id) DO NOTHING;
        END;
        $_$;

CREATE FUNCTION analysis.write_phase4_book_attribution(p jsonb, sig text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'app'
    AS $$
      BEGIN
        IF NOT analysis.phase4_telemetry_authorized('phase4-book-attribution.v1', p, sig) THEN RAISE EXCEPTION 'Phase 4 attribution authorization signature is invalid'; END IF;
        INSERT INTO analysis.book_attribution
          (book_attribution_id, allocation_id, allocation_item_id, strategy_forecast_id, hypothesis_id, action_id, rank_id,
           expression, experiment_id, trial_id, result_id, paper_execution_observation_id, pnl_status, realized_pnl,
           attribution, input_cutoff, input_hash, content_hash)
        VALUES (p->>'book_attribution_id', p->>'allocation_id', p->>'allocation_item_id', p->>'strategy_forecast_id',
          (p->>'hypothesis_id')::uuid, p->>'action_id', p->>'rank_id', jsonb_extract_path(p, 'expression'), p->>'experiment_id',
          (p->>'trial_id')::uuid, (p->>'result_id')::uuid, p->>'paper_execution_observation_id', p->>'pnl_status',
          (p->>'realized_pnl')::double precision, jsonb_extract_path(p, 'attribution'), (p->>'input_cutoff')::timestamptz, p->>'input_hash', p->>'content_hash')
        ON CONFLICT (book_attribution_id) DO NOTHING;
      END $$;

CREATE FUNCTION analysis.write_phase4_execution(p jsonb, sig text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'app', 'public'
    AS $$
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

CREATE FUNCTION analysis.write_phase4_execution_0077(p jsonb, sig text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'app', 'public'
    AS $$
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

CREATE FUNCTION analysis.write_phase4_paper_execution(p jsonb, sig text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'app'
    AS $$
      BEGIN
        IF NOT analysis.phase4_telemetry_authorized('phase4-paper-execution.v1', p, sig) THEN RAISE EXCEPTION 'Phase 4 paper execution authorization signature is invalid'; END IF;
        INSERT INTO app.paper_execution_observation
          (paper_execution_observation_id, allocation_item_id, action_id, paper_order_id, execution_mode, paper_only, status,
           requested_quantity, filled_quantity, requested_price, fill_price, spread_bps, latency_ms, impact_bps, side,
           exit_price, event_fee, contract_multiplier, observed_at, available_at, metadata)
        VALUES (p->>'paper_execution_observation_id', p->>'allocation_item_id', p->>'action_id', (p->>'paper_order_id')::uuid,
          p->>'execution_mode', (p->>'paper_only')::boolean, p->>'status', (p->>'requested_quantity')::double precision,
          (p->>'filled_quantity')::double precision, (p->>'requested_price')::double precision, (p->>'fill_price')::double precision,
          (p->>'spread_bps')::double precision, (p->>'latency_ms')::double precision, (p->>'impact_bps')::double precision,
          p->>'side', (p->>'exit_price')::double precision, (p->>'event_fee')::double precision,
          (p->>'contract_multiplier')::double precision, (p->>'observed_at')::timestamptz, (p->>'available_at')::timestamptz, jsonb_extract_path(p, 'metadata'))
        ON CONFLICT (paper_execution_observation_id) DO NOTHING;
      END $$;

CREATE FUNCTION analysis.write_research_evaluator_output(p_trial_id uuid, p_result_id uuid, p_run_id uuid, p_kind text, p_evaluator text, p_code_version text, p_input_digest text, p_universe_digest text, p_feature_digest text, p_samples integer, p_valid boolean, p_output jsonb, p_authorization_signature text) RETURNS TABLE(id uuid, output_hash text, available_at timestamp with time zone)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
        DECLARE
            signing_key TEXT; expected_authorization TEXT;
            existing_output analysis.research_evaluator_output%ROWTYPE;
            actual TIMESTAMPTZ; content_digest TEXT; final_signature TEXT;
        BEGIN
            signing_key := analysis.research_evaluator_signing_key();
            IF signing_key IS NULL OR length(signing_key) < 16 THEN
                RAISE EXCEPTION 'protected research evaluator signing key is not configured';
            END IF;
            expected_authorization := encode(public.hmac(
                convert_to(analysis.research_evaluator_authorization_payload(
                    p_trial_id, p_result_id, p_run_id, p_kind, p_evaluator, p_code_version,
                    p_input_digest, p_universe_digest, p_feature_digest, p_samples,
                    p_valid, p_output
                ), 'UTF8'), convert_to(signing_key, 'UTF8'), 'sha256'::TEXT
            ), 'hex');
            IF p_authorization_signature IS NULL
               OR lower(p_authorization_signature) <> expected_authorization THEN
                RAISE EXCEPTION 'research evaluator authorization signature is invalid';
            END IF;

            SELECT output_row.* INTO existing_output
            FROM analysis.research_evaluator_output output_row
            WHERE output_row.trial_result_id = p_result_id
              AND output_row.evidence_kind = p_kind;
            IF existing_output.id IS NOT NULL THEN
                IF existing_output.research_trial_id IS DISTINCT FROM p_trial_id
                   OR existing_output.analysis_run_id IS DISTINCT FROM p_run_id
                   OR existing_output.evaluator_id IS DISTINCT FROM p_evaluator
                   OR existing_output.evaluator_code_version IS DISTINCT FROM p_code_version
                   OR existing_output.input_hash IS DISTINCT FROM p_input_digest
                   OR existing_output.universe_hash IS DISTINCT FROM p_universe_digest
                   OR existing_output.feature_hash IS DISTINCT FROM p_feature_digest
                   OR existing_output.sample_count IS DISTINCT FROM p_samples
                   OR existing_output.domain_valid IS DISTINCT FROM p_valid
                   OR existing_output.raw_output IS DISTINCT FROM p_output
                THEN
                    RAISE EXCEPTION 'immutable evaluator output conflicts with the protected persisted row';
                END IF;
                RETURN QUERY SELECT existing_output.id, existing_output.output_hash::TEXT, existing_output.available_at;
                RETURN;
            END IF;

            -- The only new timestamp is assigned inside this protected writer.
            actual := clock_timestamp();
            content_digest := analysis.research_evaluator_output_hash_v2(
                p_trial_id, p_result_id, p_run_id, p_kind, p_evaluator,
                p_code_version, p_input_digest, p_universe_digest,
                p_feature_digest, p_samples, p_valid, p_output, actual
            );
            final_signature := encode(public.hmac(
                convert_to(analysis.research_evaluator_signature_payload(
                    p_trial_id, p_result_id, p_run_id, p_kind, p_evaluator,
                    p_code_version, p_input_digest, p_universe_digest,
                    p_feature_digest, p_samples, p_valid, content_digest, actual
                ), 'UTF8'), convert_to(signing_key, 'UTF8'), 'sha256'::TEXT
            ), 'hex');
            RETURN QUERY
            INSERT INTO analysis.research_evaluator_output(
                research_trial_id, trial_result_id, analysis_run_id, evidence_kind,
                evaluator_id, evaluator_code_version, input_hash, universe_hash,
                feature_hash, sample_count, domain_valid, raw_output,
                output_hash, signature, available_at
            ) VALUES (
                p_trial_id, p_result_id, p_run_id, p_kind, p_evaluator,
                p_code_version, p_input_digest, p_universe_digest,
                p_feature_digest, p_samples, p_valid, p_output,
                content_digest, final_signature, actual
            )
            RETURNING analysis.research_evaluator_output.id,
                      analysis.research_evaluator_output.output_hash::TEXT,
                      analysis.research_evaluator_output.available_at;
        END;
        $$;
