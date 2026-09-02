"""Close Phase 1 research authority, PIT, and promotion gaps."""

from __future__ import annotations

from alembic import op


revision = "20260902_0060"
down_revision = "20260901_0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS enforce_research_trial_terminal_immutability ON analysis.research_trial;
        DROP TRIGGER IF EXISTS enforce_research_authority_availability ON analysis.hypothesis;
        DROP TRIGGER IF EXISTS enforce_research_authority_availability ON analysis.experiment_family;
        DROP TRIGGER IF EXISTS enforce_research_authority_availability ON analysis.experiment_manifest;
        DROP TRIGGER IF EXISTS enforce_research_authority_availability ON analysis.trial_universe_manifest;

        CREATE OR REPLACE FUNCTION analysis.enforce_research_authority_availability()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            NEW.created_at := actual;
            NEW.available_at := actual;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER enforce_research_authority_availability
            BEFORE INSERT ON analysis.hypothesis
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_authority_availability();
        CREATE TRIGGER enforce_research_authority_availability
            BEFORE INSERT ON analysis.experiment_family
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_authority_availability();
        CREATE TRIGGER enforce_research_authority_availability
            BEFORE INSERT ON analysis.experiment_manifest
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_authority_availability();
        CREATE TRIGGER enforce_research_authority_availability
            BEFORE INSERT ON analysis.trial_universe_manifest
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_authority_availability();

        CREATE OR REPLACE FUNCTION analysis.research_trial_universe_complete(trial_id UUID)
        RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
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
                      digest(replace(manifest.expected_members::text, ' ', ''), 'sha256'), 'hex'
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

        CREATE OR REPLACE FUNCTION analysis.research_family_complete(family_id UUID)
        RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
            SELECT EXISTS (
                SELECT 1 FROM analysis.experiment_manifest manifest
                WHERE manifest.experiment_family_id = family_id
                  AND lower(manifest.manifest_hash) = encode(
                      digest(replace(manifest.expected_trial_keys::text, ' ', ''), 'sha256'), 'hex'
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

        CREATE OR REPLACE FUNCTION analysis.enforce_research_trial_terminal_immutability()
        RETURNS trigger LANGUAGE plpgsql AS $$
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
        CREATE TRIGGER enforce_research_trial_terminal_immutability
            BEFORE INSERT OR UPDATE OR DELETE ON analysis.research_trial
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_trial_terminal_immutability();

        CREATE OR REPLACE FUNCTION analysis.research_validation_evidence_complete(result_uuid UUID, expected_attempt_count INTEGER)
        RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
            SELECT EXISTS (
                SELECT 1
                FROM analysis.trial_result result
                WHERE result.id = result_uuid
                  AND result.result_kind = 'validation'
                  AND result.outcome->>'passed' = 'true'
                  AND (result.outcome->'checks'->'pit'->>'passed') = 'true'
                  AND (result.outcome->'checks'->'pit'->>'domain_valid') = 'true'
                  AND (result.outcome->'checks'->'denominator'->>'passed') = 'true'
                  AND (result.outcome->'checks'->'denominator'->>'domain_valid') = 'true'
                  AND (result.outcome->'checks'->'attempt_manifest'->>'passed') = 'true'
                  AND (result.outcome->'checks'->'attempt_manifest'->>'domain_valid') = 'true'
                  AND (result.outcome->'checks'->'negative_controls'->>'passed') = 'true'
                  AND (result.outcome->'checks'->'negative_controls'->>'domain_valid') = 'true'
                  AND (result.outcome->'checks'->'negative_controls'->>'controls_present') = 'true'
                  AND (result.outcome->'checks'->'mechanism'->>'passed') = 'true'
                  AND (result.outcome->'checks'->'mechanism'->>'domain_valid') = 'true'
                  AND (result.outcome->'checks'->'mechanism'->>'evidence_count')::INTEGER > 0
                  AND (result.outcome->'checks'->'parameter_stability'->>'passed') = 'true'
                  AND (result.outcome->'checks'->'parameter_stability'->>'domain_valid') = 'true'
                  AND (result.outcome->'checks'->'parameter_stability'->>'sample_size')::INTEGER >= 3
                  AND (result.outcome->'checks'->'neutralization'->>'passed') = 'true'
                  AND (result.outcome->'checks'->'neutralization'->>'domain_valid') = 'true'
                  AND (result.outcome->'checks'->'neutralization'->>'result_exists') = 'true'
                  AND (result.outcome->'checks'->'combinatorial_paths'->>'passed') = 'true'
                  AND (result.outcome->'checks'->'combinatorial_paths'->>'domain_valid') = 'true'
                  AND (result.outcome->'checks'->'combinatorial_paths'->>'path_count')::INTEGER > 0
                  AND (result.outcome->'checks'->'robustness'->>'passed') = 'true'
                  AND (result.outcome->'checks'->'multiple_testing'->>'domain_valid') = 'true'
                  AND (result.outcome->'checks'->'multiple_testing'->>'paths_domain_valid') = 'true'
                  AND (result.outcome->'checks'->'multiple_testing'->>'p_values_domain_valid') = 'true'
                  AND (result.outcome->'checks'->'multiple_testing'->>'trials_tested')::INTEGER = expected_attempt_count
                  AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'psr') = 'number'
                  AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'dsr') = 'number'
                  AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'pbo') = 'number'
                  AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'data_snooping_probability') = 'number'
                  AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'fdr_q_value') = 'number'
                  AND (result.outcome->'checks'->'multiple_testing'->>'psr')::DOUBLE PRECISION BETWEEN 0 AND 1
                  AND (result.outcome->'checks'->'multiple_testing'->>'dsr')::DOUBLE PRECISION BETWEEN 0 AND 1
                  AND (result.outcome->'checks'->'multiple_testing'->>'pbo')::DOUBLE PRECISION BETWEEN 0 AND 1
                  AND (result.outcome->'checks'->'multiple_testing'->>'data_snooping_probability')::DOUBLE PRECISION BETWEEN 0 AND 1
                  AND (result.outcome->'checks'->'multiple_testing'->>'fdr_q_value')::DOUBLE PRECISION BETWEEN 0 AND 1
                  AND (result.outcome->'checks'->'cost_capacity'->>'passed') = 'true'
                  AND (result.outcome->'checks'->'cost_capacity'->>'domain_valid') = 'true'
                  AND jsonb_typeof(result.outcome->'checks'->'cost_capacity'->'multiples'->'1x'->'net_return') = 'number'
                  AND jsonb_typeof(result.outcome->'checks'->'cost_capacity'->'multiples'->'2x'->'net_return') = 'number'
                  AND jsonb_typeof(result.outcome->'checks'->'cost_capacity'->'multiples'->'3x'->'net_return') = 'number'
            );
        $$;

        CREATE OR REPLACE FUNCTION analysis.enforce_validation_dossier_seal()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE gate_count INTEGER; passing_count INTEGER; result_id UUID; actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            IF TG_OP = 'DELETE' OR (TG_OP <> 'INSERT' AND OLD.status IN ('sealed', 'rejected')) THEN
                RAISE EXCEPTION 'validation dossiers are immutable';
            END IF;
            IF TG_OP = 'INSERT' THEN
                NEW.created_at := actual;
            END IF;
            IF NEW.status <> 'sealed' THEN
                RETURN NEW;
            END IF;
            IF NEW.research_trial_id IS NULL THEN
                RAISE EXCEPTION 'sealed validation dossier requires a research trial';
            END IF;
            IF jsonb_typeof(NEW.sections) <> 'object'
               OR NOT (NEW.sections ?& ARRAY['hypothesis', 'mechanism', 'falsification', 'controls', 'validation', 'economics', 'lineage']) THEN
                RAISE EXCEPTION 'validation dossier mandatory sections are incomplete';
            END IF;
            SELECT count(*), count(*) FILTER (WHERE verdict = 'pass' AND metrics->>'passed' = 'true'
                                                  AND metrics->>'domain_valid' = 'true'
                                                  AND jsonb_typeof(metrics->'checks') = 'object'
                                                  AND metrics->'checks' <> '{}'::jsonb
                                                  AND CASE gate_code
                                                      WHEN 'pit_integrity' THEN metrics->'checks' ? 'pit'
                                                      WHEN 'denominator_completeness' THEN metrics->'checks' ?& ARRAY['denominator', 'attempt_manifest']
                                                      WHEN 'oos_predictive_validity' THEN metrics->'checks' ?& ARRAY['predictive', 'multiple_testing']
                                                      WHEN 'falsification_and_robustness' THEN metrics->'checks' ?& ARRAY['mechanism', 'negative_controls', 'parameter_stability', 'neutralization', 'combinatorial_paths', 'robustness']
                                                      WHEN 'economic_promotability' THEN metrics->'checks' ?& ARRAY['cost_capacity', 'neutralization']
                                                      ELSE false
                                                  END
                                                  AND evidence->>'trial_result_id' IS NOT NULL)
              INTO gate_count, passing_count
              FROM analysis.validation_gate_result gate WHERE gate.dossier_id = NEW.id;
            IF gate_count <> 5 OR passing_count <> 5 THEN
                RAISE EXCEPTION 'validation dossier requires all five passing gates with evidence metrics';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM analysis.research_trial trial
                WHERE trial.id = NEW.research_trial_id
                  AND trial.status = 'succeeded'
                  AND analysis.research_trial_universe_complete(trial.id)
                  AND analysis.research_family_complete(trial.experiment_family_id)
                  AND NEW.artifact_id IS NOT NULL AND NEW.artifact_hash IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'validation dossier research trial or manifest is incomplete';
            END IF;
            SELECT result.id INTO result_id
            FROM analysis.trial_result result
            WHERE result.research_trial_id = NEW.research_trial_id
              AND result.result_kind = 'validation'
              AND result.outcome->>'passed' = 'true'
              AND result.outcome ? 'checks'
            ORDER BY result.result_version DESC LIMIT 1;
            IF result_id IS NULL OR NOT analysis.research_validation_evidence_complete(
                result_id,
                (SELECT expected_trial_count
                 FROM analysis.experiment_manifest manifest
                 JOIN analysis.research_trial trial ON trial.experiment_family_id = manifest.experiment_family_id
                 WHERE trial.id = NEW.research_trial_id)
            ) OR (
                SELECT count(*) FROM analysis.validation_gate_result gate
                WHERE gate.dossier_id = NEW.id
                  AND gate.verdict = 'pass'
                  AND gate.metrics->>'passed' = 'true'
                  AND gate.evidence->>'trial_result_id' = result_id::text
            ) <> 5 THEN
                RAISE EXCEPTION 'validation dossier requires non-empty evidence-backed validation result for every gate';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM analysis.trial_result result
                WHERE result.id = result_id
                  AND (result.outcome->'checks'->'multiple_testing') ?& ARRAY[
                      'psr', 'dsr', 'pbo', 'data_snooping_probability', 'fdr_q_value'
                  ]
                  AND (result.outcome->'checks'->'cost_capacity'->'multiples') ?& ARRAY['1x', '2x', '3x']
                  AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'psr') = 'number'
                  AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'dsr') = 'number'
                  AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'pbo') = 'number'
                  AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'data_snooping_probability') = 'number'
                  AND jsonb_typeof(result.outcome->'checks'->'multiple_testing'->'fdr_q_value') = 'number'
                  AND result.outcome->'checks'->'multiple_testing'->>'domain_valid' = 'true'
                  AND result.outcome->'checks'->'multiple_testing'->>'paths_domain_valid' = 'true'
                  AND result.outcome->'checks'->'multiple_testing'->>'p_values_domain_valid' = 'true'
                  AND (result.outcome->'checks'->'multiple_testing'->>'trials_tested')::integer = (
                      SELECT manifest.expected_trial_count
                      FROM analysis.experiment_manifest manifest
                      JOIN analysis.research_trial trial ON trial.experiment_family_id = manifest.experiment_family_id
                      WHERE trial.id = NEW.research_trial_id
                  )
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
            ) THEN
                RAISE EXCEPTION 'validation dossier mandatory bounded metrics and cost stress are missing or invalid';
            END IF;
            NEW.sealed_at := actual;
            RETURN NEW;
        END;
        $$;

        CREATE OR REPLACE FUNCTION analysis.enforce_research_revision_promotion()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
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
                      AND evaluation.evaluated_at <= trial.input_cutoff
                    WHERE dossier.strategy_revision_id = NEW.id
                      AND dossier.status = 'sealed'
                      AND trial.status = 'succeeded'
                      AND trial.experiment_family_id = NEW.experiment_family_id
                      AND dossier.artifact_id = NEW.artifact_id
                      AND dossier.artifact_hash = NEW.artifact_hash
                      AND dossier.compiled_policy->>'paper_only' = 'true'
                      AND trial.available_at <= trial.input_cutoff
                      AND dossier.sealed_at <= trial.input_cutoff
                      AND experiment_manifest.available_at <= trial.input_cutoff
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
                            AND (family_trial.available_at > trial.input_cutoff
                                 OR family_trial.finished_at > trial.input_cutoff
                                 OR family_result.available_at > trial.input_cutoff)
                      )
                      AND analysis.research_trial_universe_complete(trial.id)
                      AND NOT EXISTS (
                          SELECT 1 FROM analysis.trial_universe_manifest manifest
                          WHERE manifest.research_trial_id = trial.id
                            AND (manifest.available_at > trial.input_cutoff
                                 OR lower(manifest.manifest_hash) <> encode(
                                     digest(replace(manifest.expected_members::text, ' ', ''), 'sha256'), 'hex'
                                 ))
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM analysis.universe_observation observation
                          WHERE observation.research_trial_id = trial.id
                            AND observation.available_at > trial.input_cutoff
                      )
                      AND EXISTS (
                          SELECT 1 FROM analysis.trial_result result
                          WHERE result.id = (
                              SELECT candidate.id FROM analysis.trial_result candidate
                              WHERE candidate.research_trial_id = trial.id AND candidate.result_kind = 'validation'
                              ORDER BY candidate.result_version DESC LIMIT 1
                          )
                            AND result.available_at <= trial.input_cutoff
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
                             AND gate.available_at <= trial.input_cutoff
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
                            AND forecast.generated_at <= trial.input_cutoff
                            AND forecast.available_at <= trial.input_cutoff
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

        CREATE OR REPLACE FUNCTION analysis.enforce_research_result_actual_availability()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            NEW.available_at := actual;
            NEW.observed_at := GREATEST(NEW.observed_at, actual);
            RETURN NEW;
        END;
        $$;
        DROP TRIGGER IF EXISTS enforce_research_result_actual_availability ON analysis.trial_result;
        CREATE TRIGGER enforce_research_result_actual_availability
            BEFORE INSERT ON analysis.trial_result
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_result_actual_availability();

        CREATE OR REPLACE FUNCTION analysis.enforce_research_gate_actual_availability()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            NEW.evaluated_at := actual;
            NEW.available_at := actual;
            RETURN NEW;
        END;
        $$;
        DROP TRIGGER IF EXISTS enforce_research_gate_actual_availability ON analysis.validation_gate_result;
        CREATE TRIGGER enforce_research_gate_actual_availability
            BEFORE INSERT ON analysis.validation_gate_result
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_gate_actual_availability();

        CREATE OR REPLACE FUNCTION analysis.enforce_strategy_forecast_authority()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.id IS DISTINCT FROM NEW.id
                   OR OLD.strategy_revision_id IS DISTINCT FROM NEW.strategy_revision_id
                   OR OLD.strategy_evaluation_id IS DISTINCT FROM NEW.strategy_evaluation_id
                   OR OLD.instrument_id IS DISTINCT FROM NEW.instrument_id
                   OR OLD.opportunity_episode_id IS DISTINCT FROM NEW.opportunity_episode_id
                   OR OLD.target IS DISTINCT FROM NEW.target
                   OR OLD.horizon IS DISTINCT FROM NEW.horizon
                   OR OLD.forecast_value IS DISTINCT FROM NEW.forecast_value
                   OR OLD.forecast_range IS DISTINCT FROM NEW.forecast_range
                   OR OLD.forecast_distribution IS DISTINCT FROM NEW.forecast_distribution
                   OR OLD.probability_semantics IS DISTINCT FROM NEW.probability_semantics
                   OR OLD.model_artifact_id IS DISTINCT FROM NEW.model_artifact_id
                   OR OLD.artifact_hash IS DISTINCT FROM NEW.artifact_hash
                   OR OLD.input_hash IS DISTINCT FROM NEW.input_hash
                   OR OLD.as_of IS DISTINCT FROM NEW.as_of
                   OR OLD.input_cutoff IS DISTINCT FROM NEW.input_cutoff
                   OR OLD.generated_at IS DISTINCT FROM NEW.generated_at
                   OR OLD.available_at IS DISTINCT FROM NEW.available_at
                THEN
                    RAISE EXCEPTION 'strategy forecast immutable content cannot be changed';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.id !~ '^forecast:strategy-forecast:[0-9a-f]{32}$'
               OR NEW.artifact_hash !~ '^[0-9a-fA-F]{64}$'
               OR NEW.input_hash !~ '^[0-9a-fA-F]{64}$'
               OR lower(NEW.artifact_hash) = repeat('0', 64)
               OR lower(NEW.input_hash) = repeat('0', 64)
               OR NEW.as_of <> NEW.input_cutoff
               OR NEW.generated_at < actual - interval '5 seconds'
               OR NEW.generated_at > actual + interval '5 seconds'
               OR NEW.available_at < NEW.generated_at
               OR NEW.available_at > actual + interval '5 seconds'
               OR (NEW.forecast_value IS NULL AND NEW.forecast_range IS NULL AND NEW.forecast_distribution IS NULL)
            THEN
                RAISE EXCEPTION 'strategy forecast requires current immutable content, non-zero hashes, and actual availability';
            END IF;
            RETURN NEW;
        END;
        $$;
        DROP TRIGGER IF EXISTS enforce_strategy_forecast_authority ON analysis.strategy_forecast;
        CREATE TRIGGER enforce_strategy_forecast_authority
            BEFORE INSERT OR UPDATE ON analysis.strategy_forecast
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_strategy_forecast_authority();
        """
    )


def downgrade() -> None:
    # The preceding migration owns the Phase 1 tables and base triggers.
    pass
