-- Analysis functions: research validation and strategy guards.

CREATE FUNCTION analysis.enforce_research_authority_availability() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            NEW.created_at := actual;
            NEW.available_at := actual;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_evaluator_output() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis'
    AS $_$
        DECLARE
            result_trial UUID; result_hash TEXT; result_outcome JSONB; trial_cutoff TIMESTAMPTZ;
            run_input_hash TEXT; run_cutoff TIMESTAMPTZ; run_code TEXT;
            universe_digest TEXT; actual TIMESTAMPTZ := clock_timestamp();
            expected_hash TEXT; expected_signature TEXT; signing_key TEXT;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'research evaluator outputs are immutable';
            END IF;
            SELECT result.research_trial_id, result.input_hash, result.outcome, trial.input_cutoff
              INTO result_trial, result_hash, result_outcome, trial_cutoff
            FROM analysis.trial_result result
            JOIN analysis.research_trial trial ON trial.id = result.research_trial_id
            WHERE result.id = NEW.trial_result_id;
            SELECT input_hash, input_cutoff, code_version
              INTO run_input_hash, run_cutoff, run_code
            FROM analysis.run WHERE id = NEW.analysis_run_id;
            SELECT manifest.manifest_hash INTO universe_digest
            FROM analysis.trial_universe_manifest manifest
            WHERE manifest.research_trial_id = result_trial;
            signing_key := analysis.research_evaluator_signing_key();
            IF signing_key IS NULL OR length(signing_key) < 16 THEN
                RAISE EXCEPTION 'protected research evaluator signing key is not configured';
            END IF;
            IF result_trial IS NULL OR result_trial <> NEW.research_trial_id
               OR result_outcome->>'passed' IS DISTINCT FROM 'true'
               OR run_input_hash IS DISTINCT FROM result_hash
               OR run_cutoff IS DISTINCT FROM trial_cutoff
               OR run_code IS DISTINCT FROM NEW.evaluator_code_version
               OR NEW.input_hash IS DISTINCT FROM result_hash
               OR NEW.universe_hash IS DISTINCT FROM universe_digest
               OR NEW.raw_output->>'trial_input_hash' IS DISTINCT FROM result_hash
               OR NEW.raw_output->>'input_hash' IS DISTINCT FROM NEW.input_hash
               OR NEW.raw_output->>'universe_hash' IS DISTINCT FROM NEW.universe_hash
               OR NEW.raw_output->>'feature_hash' IS DISTINCT FROM NEW.feature_hash
               OR NEW.raw_output->>'evaluator_id' IS DISTINCT FROM NEW.evaluator_id
               OR NEW.raw_output->>'evaluator_code_version' IS DISTINCT FROM NEW.evaluator_code_version
               OR NEW.raw_output->>'evidence_kind' IS DISTINCT FROM NEW.evidence_kind
               OR NEW.evaluator_id IS NULL OR length(trim(NEW.evaluator_id)) < 3
               OR NEW.evaluator_id LIKE '%' || chr(31) || '%'
               OR NEW.evaluator_code_version IS NULL OR length(trim(NEW.evaluator_code_version)) < 3
               OR NEW.evaluator_code_version LIKE '%' || chr(31) || '%'
               OR NEW.signature !~ '^[0-9a-fA-F]{64}$'
               OR NEW.available_at > actual
               OR NOT NEW.domain_valid OR NEW.sample_count <= 0
            THEN
                RAISE EXCEPTION 'signed evaluator output is not linked to the protected authoritative trial and run';
            END IF;
            IF NEW.evidence_kind = 'controls' THEN
                IF jsonb_typeof(NEW.raw_output->'randomized_label_samples') <> 'array'
                   OR jsonb_typeof(NEW.raw_output->'white_noise_samples') <> 'array'
                   OR jsonb_array_length(NEW.raw_output->'randomized_label_samples') = 0
                   OR jsonb_array_length(NEW.raw_output->'white_noise_samples') = 0
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.raw_output->'randomized_label_samples') value WHERE jsonb_typeof(value) <> 'number')
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.raw_output->'white_noise_samples') value WHERE jsonb_typeof(value) <> 'number')
                   OR NEW.sample_count <> jsonb_array_length(NEW.raw_output->'randomized_label_samples') + jsonb_array_length(NEW.raw_output->'white_noise_samples')
                THEN RAISE EXCEPTION 'control evaluator output is incomplete'; END IF;
            ELSIF NEW.evidence_kind = 'cpcv_paths' THEN
                IF NOT analysis.research_combinatorial_paths_complete(NEW.raw_output)
                   OR NEW.sample_count <> (NEW.raw_output->>'path_count')::INTEGER
                THEN RAISE EXCEPTION 'CPCV evaluator output is incomplete'; END IF;
            ELSIF NEW.evidence_kind = 'neutralization' THEN
                IF jsonb_typeof(NEW.raw_output->'samples') <> 'array'
                   OR jsonb_array_length(NEW.raw_output->'samples') = 0
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.raw_output->'samples') value WHERE jsonb_typeof(value) <> 'number')
                   OR NEW.sample_count <> jsonb_array_length(NEW.raw_output->'samples')
                THEN RAISE EXCEPTION 'neutralization evaluator output is incomplete'; END IF;
            ELSIF NEW.evidence_kind = 'parameter_stability' THEN
                IF jsonb_typeof(NEW.raw_output->'samples') <> 'array'
                   OR jsonb_array_length(NEW.raw_output->'samples') < 3
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.raw_output->'samples') value WHERE jsonb_typeof(value) <> 'number')
                   OR NEW.sample_count <> jsonb_array_length(NEW.raw_output->'samples')
                THEN RAISE EXCEPTION 'stability evaluator output is incomplete'; END IF;
            ELSIF NEW.evidence_kind = 'mechanism_falsification' THEN
                IF jsonb_typeof(NEW.raw_output->'samples') <> 'array'
                   OR jsonb_array_length(NEW.raw_output->'samples') = 0
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.raw_output->'samples') value WHERE jsonb_typeof(value) <> 'number')
                   OR NEW.sample_count <> jsonb_array_length(NEW.raw_output->'samples')
                THEN RAISE EXCEPTION 'mechanism evaluator output is incomplete'; END IF;
            ELSIF NEW.evidence_kind = 'multiple_testing' THEN
                IF jsonb_typeof(NEW.raw_output->'path_returns') <> 'array'
                   OR jsonb_typeof(NEW.raw_output->'p_values') <> 'array'
                   OR jsonb_array_length(NEW.raw_output->'path_returns') = 0
                   OR jsonb_array_length(NEW.raw_output->'p_values') = 0
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.raw_output->'path_returns') value WHERE jsonb_typeof(value) <> 'number')
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.raw_output->'p_values') value WHERE jsonb_typeof(value) <> 'number' OR value::TEXT::DOUBLE PRECISION NOT BETWEEN 0 AND 1)
                   OR jsonb_typeof(NEW.raw_output->'metrics') <> 'object'
                   OR NEW.raw_output->'metrics'->>'domain_valid' <> 'true'
                   OR NOT (NEW.raw_output->'metrics' ?& ARRAY['psr', 'dsr', 'pbo', 'data_snooping_probability', 'fdr_q_value'])
                   OR NEW.sample_count <> jsonb_array_length(NEW.raw_output->'path_returns')
                THEN RAISE EXCEPTION 'multiple-testing evaluator output is incomplete'; END IF;
            END IF;
            expected_hash := analysis.research_evaluator_output_hash_v2(
                NEW.research_trial_id, NEW.trial_result_id, NEW.analysis_run_id,
                NEW.evidence_kind, NEW.evaluator_id, NEW.evaluator_code_version,
                NEW.input_hash, NEW.universe_hash, NEW.feature_hash,
                NEW.sample_count, NEW.domain_valid, NEW.raw_output, NEW.available_at
            );
            expected_signature := encode(public.hmac(
                convert_to(analysis.research_evaluator_signature_payload(
                    NEW.research_trial_id, NEW.trial_result_id, NEW.analysis_run_id,
                    NEW.evidence_kind, NEW.evaluator_id, NEW.evaluator_code_version,
                    NEW.input_hash, NEW.universe_hash, NEW.feature_hash,
                    NEW.sample_count, NEW.domain_valid, expected_hash, NEW.available_at
                ), 'UTF8'), convert_to(signing_key, 'UTF8'), 'sha256'::TEXT
            ), 'hex');
            IF lower(NEW.output_hash) <> expected_hash OR lower(NEW.signature) <> expected_signature THEN
                RAISE EXCEPTION 'evaluator output signature or content hash is invalid';
            END IF;
            NEW.created_at := NEW.available_at;
            NEW.output_hash := expected_hash;
            NEW.signature := expected_signature;
            RETURN NEW;
        END;
        $_$;

CREATE FUNCTION analysis.enforce_research_evidence_manifest() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis'
    AS $$
        DECLARE source analysis.research_evaluator_output%ROWTYPE;
            result_trial UUID; result_hash TEXT; result_outcome JSONB;
            actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'research evidence manifests are immutable';
            END IF;
            SELECT research_trial_id, input_hash, outcome INTO result_trial, result_hash, result_outcome
            FROM analysis.trial_result WHERE id = NEW.trial_result_id;
            SELECT * INTO source FROM analysis.research_evaluator_output
            WHERE id = NEW.evaluator_output_id;
            IF source.id IS NULL
               OR source.research_trial_id <> NEW.research_trial_id
               OR source.trial_result_id <> NEW.trial_result_id
               OR source.evidence_kind <> NEW.evidence_kind
               OR source.evaluator_id IS DISTINCT FROM NEW.evaluator_id
               OR source.evaluator_code_version IS DISTINCT FROM NEW.evaluator_code_version
               OR source.input_hash IS DISTINCT FROM NEW.input_hash
               OR source.universe_hash IS DISTINCT FROM NEW.universe_hash
               OR source.feature_hash IS DISTINCT FROM NEW.feature_hash
               OR source.output_hash IS DISTINCT FROM NEW.evidence_hash
               OR NEW.payload IS DISTINCT FROM source.raw_output
               OR result_trial IS NULL OR result_trial <> NEW.research_trial_id
               OR result_hash IS DISTINCT FROM source.input_hash
               OR result_outcome->>'passed' IS DISTINCT FROM 'true'
            THEN
                RAISE EXCEPTION 'research evidence must bind exactly to an immutable evaluator output';
            END IF;
            IF NEW.evidence_kind = 'controls'
               AND (NEW.payload->'randomized_label_samples' IS DISTINCT FROM result_outcome->'checks'->'negative_controls'->'randomized_label_samples'
                    OR NEW.payload->'white_noise_samples' IS DISTINCT FROM result_outcome->'checks'->'negative_controls'->'white_noise_samples')
            THEN RAISE EXCEPTION 'control validation differs from evaluator output';
            ELSIF NEW.evidence_kind = 'cpcv_paths'
               AND (NEW.payload->>'path_count' IS DISTINCT FROM result_outcome->'checks'->'combinatorial_paths'->>'path_count'
                    OR NEW.payload->'path_records' IS DISTINCT FROM result_outcome->'checks'->'combinatorial_paths'->'path_records')
            THEN RAISE EXCEPTION 'CPCV validation differs from evaluator output';
            ELSIF NEW.evidence_kind = 'neutralization'
               AND NEW.payload->'samples' IS DISTINCT FROM result_outcome->'checks'->'neutralization'->'samples'
            THEN RAISE EXCEPTION 'neutralization validation differs from evaluator output';
            ELSIF NEW.evidence_kind = 'parameter_stability'
               AND NEW.payload->'samples' IS DISTINCT FROM result_outcome->'checks'->'parameter_stability'->'samples'
            THEN RAISE EXCEPTION 'stability validation differs from evaluator output';
            ELSIF NEW.evidence_kind = 'mechanism_falsification'
               AND NEW.payload->'samples' IS DISTINCT FROM result_outcome->'checks'->'mechanism'->'evidence_samples'
            THEN RAISE EXCEPTION 'mechanism validation differs from evaluator output';
            ELSIF NEW.evidence_kind = 'multiple_testing'
               AND (NEW.payload->'path_returns' IS DISTINCT FROM result_outcome->'checks'->'multiple_testing'->'path_returns'
                    OR NEW.payload->'p_values' IS DISTINCT FROM result_outcome->'checks'->'multiple_testing'->'p_values'
                    OR NEW.payload->'metrics' IS DISTINCT FROM result_outcome->'checks'->'multiple_testing')
            THEN RAISE EXCEPTION 'multiple-testing validation differs from evaluator output';
            END IF;
            -- Preserve the old domain checks, but compare their values with
            -- the source output instead of trusting a validation projection.
            IF NEW.sample_count <> source.sample_count OR NOT NEW.domain_valid OR NOT source.domain_valid THEN
                RAISE EXCEPTION 'research evidence source domain is invalid';
            END IF;
            NEW.created_at := actual;
            NEW.available_at := actual;
            NEW.evidence_hash := source.output_hash;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_gate_actual_availability() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            NEW.evaluated_at := actual;
            NEW.available_at := actual;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_gate_pit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE cutoff TIMESTAMPTZ;
        BEGIN
            SELECT trial.input_cutoff INTO cutoff
            FROM analysis.validation_dossier dossier
            JOIN analysis.research_trial trial ON trial.id = dossier.research_trial_id
            WHERE dossier.id = NEW.dossier_id;
            IF cutoff IS NOT NULL AND NEW.available_at > NEW.evaluated_at THEN
                RAISE EXCEPTION 'validation gate has invalid actual availability';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_gate_promotion_clock() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE promotion_decision_cutoff TIMESTAMPTZ;
        BEGIN

            IF NEW.status = 'active' THEN
            SELECT (promotion.metrics->>'promotion_cutoff')::timestamptz
              INTO promotion_decision_cutoff
            FROM analysis.strategy_evaluation promotion
            WHERE NEW.strategy_key = 'ticker-stock-alpha'
              AND NEW.parameters->>'model_version' = 'ticker-stock-alpha.v3'
              AND NEW.parameters->>'target_version' = 'stock-counterfactual-20-session-net.v1'
              AND promotion.strategy_revision_id = NEW.id
              AND promotion.evaluation_type = 'paper_advisory_promotion'
              AND promotion.verdict = 'pass'
              AND promotion.artifact_id = NEW.artifact_id
              AND promotion.artifact_hash = NEW.artifact_hash
              AND promotion.input_hash = NEW.parameters->>'input_hash'
              AND promotion.metrics->>'authorization_mode' = 'PAPER'
              AND promotion.metrics->>'artifact_hash' = NEW.artifact_hash
              AND promotion.metrics->>'input_hash' = NEW.parameters->>'input_hash'
              AND promotion.evaluated_at <= clock_timestamp()
              AND promotion.available_at <= clock_timestamp()
            ORDER BY promotion.evaluated_at DESC, promotion.id DESC LIMIT 1;
            IF promotion_decision_cutoff > clock_timestamp() THEN
                RAISE EXCEPTION 'paper promotion cutoff cannot be future-dated';
            END IF;
            END IF;

            IF NEW.status = 'active' AND (NEW.research_required OR NEW.hypothesis_id IS NOT NULL OR NEW.experiment_family_id IS NOT NULL) AND EXISTS (
                SELECT 1
                FROM analysis.validation_dossier dossier
                JOIN analysis.research_trial trial ON trial.id = dossier.research_trial_id
                JOIN analysis.validation_gate_result gate ON gate.dossier_id = dossier.id
                WHERE dossier.strategy_revision_id = NEW.id
                  AND (gate.evaluated_at > COALESCE(promotion_decision_cutoff, trial.input_cutoff) OR gate.available_at > COALESCE(promotion_decision_cutoff, trial.input_cutoff))
            ) THEN
                RAISE EXCEPTION 'promotion requires database-authoritative gate timestamps at or before trial cutoff';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_manifest_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'research manifests are immutable';
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_result_actual_availability() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            IF NEW.observed_at > actual OR NEW.available_at > actual THEN
                RAISE EXCEPTION 'trial result observed or available timestamp cannot be future-dated';
            END IF;
            -- These are evidence-publication timestamps. A caller cannot
            -- backdate them to make a historical result appear PIT-available.
            NEW.observed_at := actual;
            NEW.available_at := actual;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_result_pit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE cutoff TIMESTAMPTZ;
        BEGIN
            SELECT input_cutoff INTO cutoff FROM analysis.research_trial WHERE id = NEW.research_trial_id;
            IF cutoff IS NULL OR NEW.available_at > NEW.observed_at THEN
                RAISE EXCEPTION 'research result has invalid actual availability';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_revision_promotion() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        DECLARE promotion_decision_cutoff TIMESTAMPTZ;
        BEGIN

            IF NEW.status = 'active' THEN
            SELECT (promotion.metrics->>'promotion_cutoff')::timestamptz
              INTO promotion_decision_cutoff
            FROM analysis.strategy_evaluation promotion
            WHERE NEW.strategy_key = 'ticker-stock-alpha'
              AND NEW.parameters->>'model_version' = 'ticker-stock-alpha.v3'
              AND NEW.parameters->>'target_version' = 'stock-counterfactual-20-session-net.v1'
              AND promotion.strategy_revision_id = NEW.id
              AND promotion.evaluation_type = 'paper_advisory_promotion'
              AND promotion.verdict = 'pass'
              AND promotion.artifact_id = NEW.artifact_id
              AND promotion.artifact_hash = NEW.artifact_hash
              AND promotion.input_hash = NEW.parameters->>'input_hash'
              AND promotion.metrics->>'authorization_mode' = 'PAPER'
              AND promotion.metrics->>'artifact_hash' = NEW.artifact_hash
              AND promotion.metrics->>'input_hash' = NEW.parameters->>'input_hash'
              AND promotion.evaluated_at <= clock_timestamp()
              AND promotion.available_at <= clock_timestamp()
            ORDER BY promotion.evaluated_at DESC, promotion.id DESC LIMIT 1;
            IF promotion_decision_cutoff > clock_timestamp() THEN
                RAISE EXCEPTION 'paper promotion cutoff cannot be future-dated';
            END IF;
            END IF;

            IF NEW.status = 'active' AND (
                NEW.research_required OR NEW.hypothesis_id IS NOT NULL OR
                NEW.experiment_family_id IS NOT NULL
            ) THEN
                IF NEW.hypothesis_id IS NULL OR NEW.experiment_family_id IS NULL THEN
                    RAISE EXCEPTION 'research strategy promotion requires hypothesis and experiment family lineage';
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM analysis.validation_dossier dossier
                    JOIN analysis.research_trial trial ON trial.id = dossier.research_trial_id
                    JOIN analysis.experiment_family family
                      ON family.id = trial.experiment_family_id
                     AND family.hypothesis_id = NEW.hypothesis_id
                    JOIN analysis.experiment_manifest experiment_manifest
                      ON experiment_manifest.experiment_family_id = family.id
                    JOIN analysis.strategy_evaluation evaluation
                      ON evaluation.strategy_revision_id = NEW.id
                     AND evaluation.evaluation_type = 'out_of_sample'
                     AND evaluation.verdict = 'pass'
                     AND evaluation.research_trial_id = trial.id
                     AND evaluation.validation_dossier_id = dossier.id
                     AND evaluation.artifact_id = NEW.artifact_id
                     AND evaluation.artifact_hash = NEW.artifact_hash
                      AND evaluation.input_hash = NEW.parameters->>'input_hash'
                      AND evaluation.evaluated_at <= COALESCE(promotion_decision_cutoff, trial.input_cutoff)
                    WHERE dossier.strategy_revision_id = NEW.id
                      AND dossier.status = 'sealed'
                      AND trial.status = 'succeeded'
                      AND trial.experiment_family_id = NEW.experiment_family_id
                      AND dossier.artifact_id = NEW.artifact_id
                      AND dossier.artifact_hash = NEW.artifact_hash
                      AND dossier.compiled_policy->>'paper_only' = 'true'
                      AND trial.available_at <= COALESCE(promotion_decision_cutoff, trial.input_cutoff)
                      AND dossier.sealed_at <= COALESCE(promotion_decision_cutoff, trial.input_cutoff)
                      AND experiment_manifest.available_at <= COALESCE(promotion_decision_cutoff, trial.input_cutoff)
                      AND lower(experiment_manifest.manifest_hash) = encode(
                          digest(replace(experiment_manifest.expected_trial_keys::text, ' ', ''), 'sha256'), 'hex'
                      )
                      AND analysis.research_family_complete(trial.experiment_family_id)
                      AND NOT EXISTS (
                          SELECT 1 FROM analysis.research_trial family_trial
                          LEFT JOIN analysis.trial_result family_result
                            ON family_result.research_trial_id = family_trial.id
                           AND family_result.result_kind = 'validation'
                          WHERE family_trial.experiment_family_id = trial.experiment_family_id
                            AND (family_trial.available_at > COALESCE(promotion_decision_cutoff, trial.input_cutoff)
                                 OR family_trial.finished_at > COALESCE(promotion_decision_cutoff, trial.input_cutoff)
                                 OR family_result.available_at > COALESCE(promotion_decision_cutoff, trial.input_cutoff))
                      )
                      AND analysis.research_trial_universe_complete(trial.id)
                      AND NOT EXISTS (
                          SELECT 1 FROM analysis.trial_universe_manifest manifest
                          WHERE manifest.research_trial_id = trial.id
                            AND (manifest.available_at > COALESCE(promotion_decision_cutoff, trial.input_cutoff)
                                 OR lower(manifest.manifest_hash) <> encode(
                                     digest(replace(manifest.expected_members::text, ' ', ''), 'sha256'), 'hex'
                                 ))
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM analysis.universe_observation observation
                          WHERE observation.research_trial_id = trial.id
                            AND observation.available_at > COALESCE(promotion_decision_cutoff, trial.input_cutoff)
                      )
                      AND EXISTS (
                          SELECT 1 FROM analysis.trial_result result
                          WHERE result.id = (
                              SELECT candidate.id FROM analysis.trial_result candidate
                              WHERE candidate.research_trial_id = trial.id AND candidate.result_kind = 'validation'
                              ORDER BY candidate.result_version DESC LIMIT 1
                          )
                            AND result.available_at <= COALESCE(promotion_decision_cutoff, trial.input_cutoff)
                            AND result.outcome->>'passed' = 'true'
                            AND analysis.research_validation_evidence_complete(result.id, experiment_manifest.expected_trial_count)
                            AND result.outcome->'checks' ? 'multiple_testing'
                            AND result.outcome->'checks' ? 'cost_capacity'
                            AND result.outcome->'checks'->'multiple_testing'->>'domain_valid' = 'true'
                            AND result.outcome->'checks'->'multiple_testing'->>'paths_domain_valid' = 'true'
                      AND result.outcome->'checks'->'multiple_testing'->>'p_values_domain_valid' = 'true'
                            AND (result.outcome->'checks'->'multiple_testing'->>'trials_tested')::integer = experiment_manifest.expected_trial_count
                            AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'psr') = 'number'
                            AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'dsr') = 'number'
                            AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'pbo') = 'number'
                            AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'data_snooping_probability') = 'number'
                            AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'fdr_q_value') = 'number'
                            AND (result.outcome->'checks'->'multiple_testing'->>'psr')::double precision BETWEEN 0 AND 1
                            AND (result.outcome->'checks'->'multiple_testing'->>'dsr')::double precision BETWEEN 0 AND 1
                            AND (result.outcome->'checks'->'multiple_testing'->>'pbo')::double precision BETWEEN 0 AND 1
                            AND (result.outcome->'checks'->'multiple_testing'->>'data_snooping_probability')::double precision BETWEEN 0 AND 1
                            AND (result.outcome->'checks'->'multiple_testing'->>'fdr_q_value')::double precision BETWEEN 0 AND 1
                            AND (result.outcome->'checks'->'cost_capacity'->'multiples'->'1x'->>'net_return') IS NOT NULL
                            AND (result.outcome->'checks'->'cost_capacity'->'multiples'->'2x'->>'net_return') IS NOT NULL
                            AND (result.outcome->'checks'->'cost_capacity'->'multiples'->'3x'->>'net_return') IS NOT NULL
                            AND jsonb_typeof(result.outcome->'checks'->'cost_capacity'->'multiples'->'1x'->'net_return') = 'number'
                            AND jsonb_typeof(result.outcome->'checks'->'cost_capacity'->'multiples'->'2x'->'net_return') = 'number'
                            AND jsonb_typeof(result.outcome->'checks'->'cost_capacity'->'multiples'->'3x'->'net_return') = 'number'
                      )
                      AND (SELECT count(*) FROM analysis.validation_gate_result gate
                           WHERE gate.dossier_id = dossier.id
                             AND gate.verdict = 'pass'
                             AND gate.metrics->>'passed' = 'true'
                             AND gate.metrics->>'domain_valid' = 'true'
                             AND jsonb_typeof(gate.metrics->'checks') = 'object'
                             AND gate.metrics->'checks' <> '{}'::jsonb
                             AND CASE gate.gate_code
                                 WHEN 'pit_integrity' THEN gate.metrics->'checks' ? 'pit'
                                 WHEN 'denominator_completeness' THEN gate.metrics->'checks' ?& ARRAY['denominator', 'attempt_manifest']
                                 WHEN 'oos_predictive_validity' THEN gate.metrics->'checks' ?& ARRAY['predictive', 'multiple_testing']
                                 WHEN 'falsification_and_robustness' THEN gate.metrics->'checks' ?& ARRAY['mechanism', 'negative_controls', 'parameter_stability', 'neutralization', 'combinatorial_paths', 'robustness']
                                 WHEN 'economic_promotability' THEN gate.metrics->'checks' ?& ARRAY['cost_capacity', 'neutralization']
                                 ELSE false
                             END
                             AND gate.available_at <= COALESCE(promotion_decision_cutoff, trial.input_cutoff)
                             AND gate.evidence->>'trial_result_id' = (
                                 SELECT result.id::text FROM analysis.trial_result result
                                 WHERE result.research_trial_id = trial.id AND result.result_kind = 'validation'
                                 ORDER BY result.result_version DESC LIMIT 1
                             )) = 5
                      AND EXISTS (
                          SELECT 1
                          FROM analysis.strategy_forecast forecast
                          WHERE forecast.strategy_evaluation_id = evaluation.id
                            AND forecast.strategy_revision_id = NEW.id
                            AND forecast.status = 'available'
                            AND forecast.input_cutoff = trial.input_cutoff
                            AND forecast.as_of = trial.input_cutoff
                            AND forecast.model_artifact_id = NEW.artifact_id
                            AND forecast.target = evaluation.metrics->>'target'
                            AND forecast.artifact_hash = NEW.artifact_hash
                            AND forecast.input_hash = evaluation.input_hash
                            AND forecast.generated_at <= COALESCE(promotion_decision_cutoff, trial.input_cutoff)
                            AND forecast.available_at <= COALESCE(promotion_decision_cutoff, trial.input_cutoff)
                            AND forecast.forecast_distribution IS NOT NULL
                            AND EXISTS (
                                SELECT 1 FROM jsonb_array_elements(evaluation.metrics->'forecasts') item
                                WHERE item->>'horizon' = forecast.horizon
                                  AND (item->>'forecast_value')::double precision IS NOT DISTINCT FROM forecast.forecast_value
                                  AND item->'forecast_distribution' = forecast.forecast_distribution
                                  AND (item->>'probability_semantics') IS NOT DISTINCT FROM forecast.probability_semantics
                            )
                      )
                      AND jsonb_typeof(evaluation.metrics->'forecasts') = 'array'
                      AND jsonb_array_length(evaluation.metrics->'forecasts') >= 1
                      AND (SELECT count(*)
                           FROM analysis.strategy_forecast forecast
                           WHERE forecast.strategy_evaluation_id = evaluation.id
                             AND forecast.strategy_revision_id = NEW.id
                             AND forecast.status = 'available'
                             AND forecast.input_cutoff = trial.input_cutoff
                             AND forecast.as_of = trial.input_cutoff
                            AND forecast.model_artifact_id = NEW.artifact_id
                            AND forecast.target = evaluation.metrics->>'target'
                             AND forecast.artifact_hash = NEW.artifact_hash
                             AND forecast.input_hash = evaluation.input_hash
                      ) = (SELECT expected_member_count
                           FROM analysis.trial_universe_manifest universe_manifest
                           WHERE universe_manifest.research_trial_id = trial.id)
                            * jsonb_array_length(evaluation.metrics->'forecasts')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM jsonb_array_elements_text((SELECT expected_members
                                                          FROM analysis.trial_universe_manifest universe_manifest
                                                          WHERE universe_manifest.research_trial_id = trial.id)) expected(member)
                          CROSS JOIN jsonb_array_elements(evaluation.metrics->'forecasts') item
                          WHERE NOT EXISTS (
                              SELECT 1 FROM analysis.strategy_forecast forecast
                              WHERE forecast.strategy_evaluation_id = evaluation.id
                                AND forecast.strategy_revision_id = NEW.id
                                AND forecast.instrument_id::text = expected.member
                                AND forecast.horizon = item->>'horizon'
                                AND forecast.input_cutoff = trial.input_cutoff
                                AND forecast.status = 'available'
                          )
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM jsonb_array_elements(evaluation.metrics->'forecasts') item
                          WHERE NOT EXISTS (
                              SELECT 1 FROM analysis.strategy_forecast forecast
                              WHERE forecast.strategy_evaluation_id = evaluation.id
                                AND forecast.strategy_revision_id = NEW.id
                                AND forecast.status = 'available'
                                AND forecast.input_cutoff = trial.input_cutoff
                                AND item->>'horizon' = forecast.horizon
                                AND (item->>'forecast_value')::double precision IS NOT DISTINCT FROM forecast.forecast_value
                                AND item->'forecast_distribution' = forecast.forecast_distribution
                                AND (item->>'probability_semantics') IS NOT DISTINCT FROM forecast.probability_semantics
                          )
                      )
                ) THEN
                    RAISE EXCEPTION 'research strategy promotion requires sealed dossier, complete manifests, evidence-backed five gates, PIT data, and exact forecast lineage';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_revision_promotion_hardened() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        DECLARE promotion_decision_cutoff TIMESTAMPTZ; trial_cutoff TIMESTAMPTZ; dossier_id UUID; evaluation_id UUID; result_id UUID; expected_members INTEGER; forecast_count INTEGER;
        BEGIN

            IF NEW.status = 'active' THEN
            SELECT (promotion.metrics->>'promotion_cutoff')::timestamptz
              INTO promotion_decision_cutoff
            FROM analysis.strategy_evaluation promotion
            WHERE NEW.strategy_key = 'ticker-stock-alpha'
              AND NEW.parameters->>'model_version' = 'ticker-stock-alpha.v3'
              AND NEW.parameters->>'target_version' = 'stock-counterfactual-20-session-net.v1'
              AND promotion.strategy_revision_id = NEW.id
              AND promotion.evaluation_type = 'paper_advisory_promotion'
              AND promotion.verdict = 'pass'
              AND promotion.artifact_id = NEW.artifact_id
              AND promotion.artifact_hash = NEW.artifact_hash
              AND promotion.input_hash = NEW.parameters->>'input_hash'
              AND promotion.metrics->>'authorization_mode' = 'PAPER'
              AND promotion.metrics->>'artifact_hash' = NEW.artifact_hash
              AND promotion.metrics->>'input_hash' = NEW.parameters->>'input_hash'
              AND promotion.evaluated_at <= clock_timestamp()
              AND promotion.available_at <= clock_timestamp()
            ORDER BY promotion.evaluated_at DESC, promotion.id DESC LIMIT 1;
            IF promotion_decision_cutoff > clock_timestamp() THEN
                RAISE EXCEPTION 'paper promotion cutoff cannot be future-dated';
            END IF;
            END IF;

            IF NEW.status <> 'active' OR NOT (NEW.research_required OR NEW.hypothesis_id IS NOT NULL OR NEW.experiment_family_id IS NOT NULL) THEN
                RETURN NEW;
            END IF;
            SELECT dossier.id, trial.input_cutoff, evaluation.id, result.id, universe.expected_member_count
              INTO dossier_id, trial_cutoff, evaluation_id, result_id, expected_members
            FROM analysis.validation_dossier dossier
            JOIN analysis.research_trial trial ON trial.id = dossier.research_trial_id
            JOIN analysis.strategy_evaluation evaluation ON evaluation.validation_dossier_id = dossier.id
                AND evaluation.strategy_revision_id = NEW.id AND evaluation.evaluation_type = 'out_of_sample'
            JOIN analysis.trial_result result ON result.research_trial_id = trial.id AND result.result_kind = 'validation'
            JOIN analysis.trial_universe_manifest universe ON universe.research_trial_id = trial.id
            WHERE dossier.strategy_revision_id = NEW.id
              AND dossier.status = 'sealed'
              AND trial.status = 'succeeded'
              AND trial.experiment_family_id = NEW.experiment_family_id
              AND NEW.hypothesis_id IS NOT NULL
              AND evaluation.verdict = 'pass'
              AND evaluation.artifact_id = NEW.artifact_id
              AND evaluation.artifact_hash = NEW.artifact_hash
              AND evaluation.input_hash = NEW.parameters->>'input_hash'
              AND dossier.artifact_id = evaluation.artifact_id
              AND dossier.artifact_hash = evaluation.artifact_hash
              AND dossier.sealed_at <= COALESCE(promotion_decision_cutoff, trial.input_cutoff)
              AND trial.available_at <= COALESCE(promotion_decision_cutoff, trial.input_cutoff)
              AND result.available_at <= COALESCE(promotion_decision_cutoff, trial.input_cutoff)
            ORDER BY result.result_version DESC LIMIT 1;
            IF dossier_id IS NULL
               OR NOT analysis.research_validation_evidence_complete(result_id, (SELECT expected_trial_count FROM analysis.experiment_manifest manifest JOIN analysis.research_trial trial ON trial.experiment_family_id = manifest.experiment_family_id WHERE trial.id = (SELECT research_trial_id FROM analysis.validation_dossier WHERE id = dossier_id)))
               OR NOT analysis.research_gate_evidence_complete(dossier_id, result_id)
               OR NEW.artifact_id IS NULL OR NEW.artifact_hash IS NULL
               OR expected_members IS NULL
            THEN
                RAISE EXCEPTION 'promotion requires complete evidence-backed research dossier';
            END IF;
            SELECT count(*) INTO forecast_count
            FROM analysis.strategy_forecast forecast
            WHERE forecast.strategy_revision_id = NEW.id
              AND forecast.strategy_evaluation_id = evaluation_id
              AND forecast.status = 'available'
              AND forecast.input_cutoff = trial_cutoff
              AND forecast.as_of = trial_cutoff
              AND forecast.generated_at <= COALESCE(promotion_decision_cutoff, trial_cutoff)
              AND forecast.available_at <= COALESCE(promotion_decision_cutoff, trial_cutoff)
              AND forecast.artifact_hash = NEW.artifact_hash
              AND forecast.model_artifact_id = NEW.artifact_id
              AND forecast.input_hash = (SELECT input_hash FROM analysis.strategy_evaluation WHERE id = evaluation_id)
              AND EXISTS (SELECT 1 FROM jsonb_array_elements((SELECT metrics->'forecasts' FROM analysis.strategy_evaluation WHERE id = evaluation_id)) item WHERE item->>'horizon' = forecast.horizon AND (item->>'forecast_value')::DOUBLE PRECISION IS NOT DISTINCT FROM forecast.forecast_value AND item->'forecast_distribution' = forecast.forecast_distribution AND item->>'probability_semantics' IS NOT DISTINCT FROM forecast.probability_semantics);
            IF forecast_count <> expected_members * (SELECT jsonb_array_length(metrics->'forecasts') FROM analysis.strategy_evaluation WHERE id = evaluation_id) THEN
                RAISE EXCEPTION 'promotion requires exact persisted model forecast distribution for every universe member';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_rows_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'research authority is immutable';
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_trial_terminal_immutability() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status <> 'running' THEN
                    RAISE EXCEPTION 'terminal research trials are immutable';
                END IF;
                RETURN OLD;
            END IF;
            IF TG_OP = 'INSERT' THEN
                NEW.started_at := actual;
                NEW.available_at := actual;
            END IF;
            IF TG_OP = 'UPDATE' AND (
                OLD.status <> 'running'
                OR NEW.experiment_family_id IS DISTINCT FROM OLD.experiment_family_id
                OR NEW.trial_key IS DISTINCT FROM OLD.trial_key
                OR NEW.input_cutoff IS DISTINCT FROM OLD.input_cutoff
                OR NEW.input_hash IS DISTINCT FROM OLD.input_hash
                OR NEW.parameters IS DISTINCT FROM OLD.parameters
                OR NEW.started_at IS DISTINCT FROM OLD.started_at
                OR NEW.available_at IS DISTINCT FROM OLD.available_at
            ) THEN
                RAISE EXCEPTION 'research trial authority is immutable after creation';
            END IF;
            IF NEW.status <> 'running' AND (
                NOT EXISTS (
                    SELECT 1 FROM analysis.trial_result result
                    WHERE result.research_trial_id = NEW.id
                      AND result.result_kind = 'validation'
                )
                OR NOT analysis.research_trial_universe_complete(NEW.id)
                OR NOT EXISTS (
                    SELECT 1 FROM analysis.experiment_manifest manifest
                    WHERE manifest.experiment_family_id = NEW.experiment_family_id
                      AND manifest.expected_trial_keys ? NEW.trial_key
                      AND manifest.expected_trial_count = jsonb_array_length(manifest.expected_trial_keys)
                      AND manifest.expected_trial_count = (
                          SELECT count(DISTINCT expected.key)
                          FROM jsonb_array_elements_text(manifest.expected_trial_keys) expected(key)
                      )
                      AND lower(manifest.manifest_hash) = encode(
                          digest(replace(manifest.expected_trial_keys::text, ' ', ''), 'sha256'), 'hex'
                      )
                    )
            ) THEN
                RAISE EXCEPTION 'terminal research trial requires a validation result and complete universe manifest';
            END IF;
            IF NEW.status <> 'running' THEN
                NEW.finished_at := actual;
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_universe_actual_availability() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            IF NEW.observed_at > actual OR NEW.available_at > actual THEN
                RAISE EXCEPTION 'universe observation cannot be future-dated';
            END IF;
            NEW.observed_at := actual;
            NEW.available_at := actual;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_research_universe_pit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE manifest_cutoff TIMESTAMPTZ;
        BEGIN
            SELECT manifest.cutoff INTO manifest_cutoff
            FROM analysis.trial_universe_manifest manifest
            WHERE manifest.research_trial_id = NEW.research_trial_id;
            IF manifest_cutoff IS NULL OR NEW.cutoff <> manifest_cutoff
               OR NEW.observed_at IS NULL OR NEW.available_at IS NULL THEN
                RAISE EXCEPTION 'universe observation has invalid PIT authority';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_strategy_evaluation_availability() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                NEW.available_at := clock_timestamp();
            ELSIF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'strategy evaluation authority is immutable';
            ELSIF NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'strategy evaluation authority is immutable';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_strategy_forecast_authority() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $_$
        DECLARE actual TIMESTAMPTZ := clock_timestamp(); expected_id TEXT; canonical JSONB;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD IS DISTINCT FROM NEW THEN
                    RAISE EXCEPTION 'strategy forecast immutable content cannot be changed';
                END IF;
                RETURN NEW;
            END IF;
            canonical := jsonb_build_array(
                'strategy-forecast.v1',
                (SELECT upper(symbol) FROM catalog.instrument WHERE id = NEW.instrument_id),
                NEW.opportunity_episode_id, NEW.strategy_revision_id::TEXT,
                NEW.strategy_evaluation_id::TEXT, NEW.target, NEW.horizon,
                analysis.canonical_forecast_number(NEW.forecast_value),
                analysis.canonical_forecast_number((NEW.forecast_range->>'low')::DOUBLE PRECISION),
                analysis.canonical_forecast_number((NEW.forecast_range->>'high')::DOUBLE PRECISION),
                COALESCE((SELECT string_agg(format('%s=%s', key, analysis.canonical_forecast_number(value::DOUBLE PRECISION)), '|' ORDER BY key)
                          FROM jsonb_each_text(COALESCE(NEW.forecast_distribution, '{}'::JSONB))), ''),
                NEW.probability_semantics, NEW.model_artifact_id,
                NEW.artifact_hash, NEW.input_hash,
                to_char(NEW.as_of AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00',
                to_char(NEW.input_cutoff AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00',
                to_char(NEW.generated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00',
                to_char(NEW.available_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '+00:00'
            );
            expected_id := 'forecast:strategy-forecast:' || left(encode(digest(canonical::TEXT, 'sha256'), 'hex'), 32);
            IF NEW.id IS DISTINCT FROM expected_id
               OR NEW.artifact_hash !~ '^[0-9a-fA-F]{64}$'
               OR NEW.input_hash !~ '^[0-9a-fA-F]{64}$'
               OR lower(NEW.artifact_hash) = repeat('0', 64)
               OR lower(NEW.input_hash) = repeat('0', 64)
               OR NEW.as_of <> NEW.input_cutoff
               OR NEW.generated_at <= date_trunc('day', actual AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
               OR NEW.generated_at > actual
               OR NEW.available_at < NEW.generated_at
               OR NEW.available_at > actual
               OR (NEW.forecast_value IS NULL AND NEW.forecast_range IS NULL AND NEW.forecast_distribution IS NULL)
            THEN
                RAISE EXCEPTION 'strategy forecast requires canonical full-payload identity and authoritative actual availability';
            END IF;
            RETURN NEW;
        END;
        $_$;

CREATE FUNCTION analysis.enforce_strategy_revision_parameters_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.parameters IS DISTINCT FROM OLD.parameters THEN
                RAISE EXCEPTION 'strategy revision parameters are immutable';
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION analysis.enforce_validation_dossier_seal() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'analysis', 'public'
    AS $$
        DECLARE result_id UUID; expected_attempts INTEGER;
        BEGIN
            IF TG_OP = 'DELETE' OR (TG_OP <> 'INSERT' AND OLD.status IN ('sealed', 'rejected')) THEN
                RAISE EXCEPTION 'validation dossiers are immutable';
            END IF;
            IF TG_OP = 'INSERT' THEN
                NEW.created_at := clock_timestamp();
            END IF;
            IF NEW.status <> 'sealed' THEN
                RETURN NEW;
            END IF;
            IF NEW.research_trial_id IS NULL
               OR jsonb_typeof(NEW.sections) <> 'object'
               OR NOT (NEW.sections ?& ARRAY['hypothesis', 'mechanism', 'falsification', 'controls', 'validation', 'economics', 'lineage'])
               OR EXISTS (SELECT 1 FROM jsonb_each_text(NEW.sections) item WHERE item.value IS NULL OR length(trim(item.value)) = 0)
            THEN
                RAISE EXCEPTION 'validation dossier mandatory sections are incomplete';
            END IF;
            SELECT result.id, manifest.expected_trial_count
              INTO result_id, expected_attempts
            FROM analysis.trial_result result
            JOIN analysis.research_trial trial ON trial.id = result.research_trial_id
            JOIN analysis.experiment_manifest manifest ON manifest.experiment_family_id = trial.experiment_family_id
            WHERE result.research_trial_id = NEW.research_trial_id
              AND result.result_kind = 'validation'
            ORDER BY result.result_version DESC LIMIT 1;
            IF result_id IS NULL
               OR NOT analysis.research_validation_evidence_complete(result_id, expected_attempts)
               OR NOT analysis.research_gate_evidence_complete(NEW.id, result_id)
               OR NOT EXISTS (
                   SELECT 1 FROM analysis.research_trial trial
                   WHERE trial.id = NEW.research_trial_id AND trial.status = 'succeeded'
                     AND trial.finished_at IS NOT NULL
                     AND analysis.research_trial_universe_complete(trial.id)
                     AND analysis.research_family_complete(trial.experiment_family_id)
               )
            THEN
                RAISE EXCEPTION 'validation dossier requires complete evidence-backed five gates';
            END IF;
            NEW.sealed_at := clock_timestamp();
            RETURN NEW;
        END;
        $$;
