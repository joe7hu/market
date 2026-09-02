"""Make Phase 1 evidence and content identity database-authoritative."""

from __future__ import annotations

from alembic import op


revision = "20260902_0061"
down_revision = "20260902_0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION analysis.research_check_complete(checks JSONB, name TEXT)
        RETURNS BOOLEAN LANGUAGE plpgsql STABLE AS $check$
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
                RETURN jsonb_typeof(item->'path_count') = 'number'
                   AND (item->>'path_count')::INTEGER > 0
                   AND jsonb_typeof(item->'path_records') = 'array'
                   AND jsonb_array_length(item->'path_records') = (item->>'path_count')::INTEGER;
            ELSIF name = 'robustness' THEN
                RETURN true;
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
        $check$;

        CREATE OR REPLACE FUNCTION analysis.research_validation_evidence_complete(result_uuid UUID, expected_attempt_count INTEGER)
        RETURNS BOOLEAN LANGUAGE sql STABLE AS $evidence$
            SELECT EXISTS (
                SELECT 1
                FROM analysis.trial_result result
                JOIN analysis.research_trial trial ON trial.id = result.research_trial_id
                WHERE result.id = result_uuid
                  AND result.result_kind = 'validation'
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
            );
        $evidence$;

        CREATE OR REPLACE FUNCTION analysis.research_gate_evidence_complete(dossier_uuid UUID, result_uuid UUID)
        RETURNS BOOLEAN LANGUAGE sql STABLE AS $gates$
            SELECT count(*) = 5 AND bool_and(
                gate.verdict = 'pass'
                AND gate.metrics->>'passed' = 'true'
                AND gate.metrics->>'domain_valid' = 'true'
                AND gate.metrics->>'validation_result_id' = result_uuid::TEXT
                AND gate.metrics->>'validation_result_input_hash' = result.input_hash
                AND gate.evidence->>'trial_result_id' = result_uuid::TEXT
                AND gate.evidence->>'input_hash' = result.input_hash
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
            WHERE gate.dossier_id = dossier_uuid;
        $gates$;

        CREATE OR REPLACE FUNCTION analysis.enforce_validation_dossier_seal()
        RETURNS trigger LANGUAGE plpgsql AS $dossier$
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
        $dossier$;

        CREATE OR REPLACE FUNCTION analysis.enforce_research_revision_promotion_hardened()
        RETURNS trigger LANGUAGE plpgsql AS $promotion$
        DECLARE trial_cutoff TIMESTAMPTZ; dossier_id UUID; evaluation_id UUID; result_id UUID; expected_members INTEGER; forecast_count INTEGER;
        BEGIN
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
              AND dossier.sealed_at <= trial.input_cutoff
              AND trial.available_at <= trial.input_cutoff
              AND result.available_at <= trial.input_cutoff
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
              AND forecast.generated_at <= trial_cutoff
              AND forecast.available_at <= trial_cutoff
              AND forecast.artifact_hash = NEW.artifact_hash
              AND forecast.model_artifact_id = NEW.artifact_id
              AND forecast.input_hash = (SELECT input_hash FROM analysis.strategy_evaluation WHERE id = evaluation_id)
              AND EXISTS (SELECT 1 FROM jsonb_array_elements((SELECT metrics->'forecasts' FROM analysis.strategy_evaluation WHERE id = evaluation_id)) item WHERE item->>'horizon' = forecast.horizon AND (item->>'forecast_value')::DOUBLE PRECISION IS NOT DISTINCT FROM forecast.forecast_value AND item->'forecast_distribution' = forecast.forecast_distribution AND item->>'probability_semantics' IS NOT DISTINCT FROM forecast.probability_semantics);
            IF forecast_count <> expected_members * (SELECT jsonb_array_length(metrics->'forecasts') FROM analysis.strategy_evaluation WHERE id = evaluation_id) THEN
                RAISE EXCEPTION 'promotion requires exact persisted model forecast distribution for every universe member';
            END IF;
            RETURN NEW;
        END;
        $promotion$;
        DROP TRIGGER IF EXISTS enforce_research_revision_promotion_hardened ON analysis.strategy_revision;
        CREATE CONSTRAINT TRIGGER enforce_research_revision_promotion_hardened
            AFTER INSERT OR UPDATE ON analysis.strategy_revision
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_revision_promotion_hardened();

        CREATE OR REPLACE FUNCTION analysis.enforce_research_universe_actual_availability()
        RETURNS trigger LANGUAGE plpgsql AS $universe$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            IF NEW.observed_at > actual OR NEW.available_at > actual THEN
                RAISE EXCEPTION 'universe observation cannot be future-dated';
            END IF;
            NEW.observed_at := actual;
            NEW.available_at := actual;
            RETURN NEW;
        END;
        $universe$;
        DROP TRIGGER IF EXISTS enforce_research_universe_actual_availability ON analysis.universe_observation;
        CREATE TRIGGER enforce_research_universe_actual_availability
            BEFORE INSERT ON analysis.universe_observation
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_universe_actual_availability();

        CREATE OR REPLACE FUNCTION analysis.enforce_strategy_forecast_authority()
        RETURNS trigger LANGUAGE plpgsql AS $forecast$
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
                NEW.forecast_value::TEXT, NEW.forecast_range->>'low', NEW.forecast_range->>'high',
                COALESCE((SELECT string_agg(format('%s=%s', key, value) , '|' ORDER BY key)
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
               OR NEW.generated_at < date_trunc('day', actual)
               OR NEW.generated_at > actual
               OR NEW.available_at < NEW.generated_at
               OR NEW.available_at > actual
               OR (NEW.forecast_value IS NULL AND NEW.forecast_range IS NULL AND NEW.forecast_distribution IS NULL)
            THEN
                RAISE EXCEPTION 'strategy forecast requires canonical full-payload identity and authoritative actual availability';
            END IF;
            RETURN NEW;
        END;
        $forecast$;
        DROP TRIGGER IF EXISTS enforce_strategy_forecast_authority ON analysis.strategy_forecast;
        CREATE TRIGGER enforce_strategy_forecast_authority
            BEFORE INSERT OR UPDATE ON analysis.strategy_forecast
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_strategy_forecast_authority();
        """
    )


def downgrade() -> None:
    pass
