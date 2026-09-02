"""Persist independent, content-addressed evidence for Phase 1 promotion."""

from __future__ import annotations

from alembic import op


revision = "20260902_0062"
down_revision = "20260902_0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE analysis.research_evidence_manifest (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            research_trial_id UUID NOT NULL REFERENCES analysis.research_trial(id),
            trial_result_id UUID NOT NULL REFERENCES analysis.trial_result(id),
            evidence_kind TEXT NOT NULL CHECK (evidence_kind IN (
                'controls', 'cpcv_paths', 'neutralization',
                'parameter_stability', 'mechanism_falsification', 'multiple_testing'
            )),
            evaluator_id TEXT NOT NULL,
            sample_count INTEGER NOT NULL CHECK (sample_count > 0),
            domain_valid BOOLEAN NOT NULL,
            payload JSONB NOT NULL,
            evidence_hash CHAR(64) NOT NULL DEFAULT repeat('0', 64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (trial_result_id, evidence_kind),
            CHECK (jsonb_typeof(payload) = 'object'),
            CHECK (available_at >= created_at)
        );
        CREATE INDEX ix_research_evidence_trial_result
            ON analysis.research_evidence_manifest (trial_result_id, evidence_kind);

        CREATE OR REPLACE FUNCTION analysis.research_evidence_hash(
            trial_id UUID, result_id UUID, kind TEXT, evaluator TEXT,
            samples INTEGER, valid BOOLEAN, evidence JSONB
        ) RETURNS TEXT LANGUAGE sql IMMUTABLE AS $hash$
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
        $hash$;

        CREATE OR REPLACE FUNCTION analysis.enforce_research_evidence_manifest()
        RETURNS trigger LANGUAGE plpgsql AS $evidence$
        DECLARE
            result_trial UUID; result_hash TEXT; result_outcome JSONB; expected_hash TEXT;
            actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'research evidence manifests are immutable';
            END IF;
            SELECT research_trial_id, input_hash, outcome INTO result_trial, result_hash, result_outcome
            FROM analysis.trial_result WHERE id = NEW.trial_result_id;
            IF result_trial IS NULL OR result_trial <> NEW.research_trial_id
               OR NEW.evaluator_id IS NULL OR length(trim(NEW.evaluator_id)) = 0
               OR NEW.sample_count <= 0 OR NOT NEW.domain_valid
                   OR NEW.payload->>'trial_input_hash' IS DISTINCT FROM result_hash
                   OR NEW.payload->>'evaluator_id' IS DISTINCT FROM NEW.evaluator_id
                   OR result_outcome->>'passed' IS DISTINCT FROM 'true'
            THEN
                RAISE EXCEPTION 'research evidence requires linked non-empty domain-valid trial evidence';
            END IF;
            IF NEW.evidence_kind = 'controls' THEN
                IF jsonb_typeof(NEW.payload->'randomized_label_samples') <> 'array'
                   OR jsonb_typeof(NEW.payload->'white_noise_samples') <> 'array'
                   OR jsonb_array_length(NEW.payload->'randomized_label_samples') = 0
                   OR jsonb_array_length(NEW.payload->'white_noise_samples') = 0
                   OR NEW.sample_count <> jsonb_array_length(NEW.payload->'randomized_label_samples') + jsonb_array_length(NEW.payload->'white_noise_samples')
                   OR NEW.payload->'randomized_label_samples' IS DISTINCT FROM result_outcome->'checks'->'negative_controls'->'randomized_label_samples'
                   OR NEW.payload->'white_noise_samples' IS DISTINCT FROM result_outcome->'checks'->'negative_controls'->'white_noise_samples'
                THEN RAISE EXCEPTION 'control evidence samples are incomplete'; END IF;
            ELSIF NEW.evidence_kind = 'cpcv_paths' THEN
                IF NOT analysis.research_combinatorial_paths_complete(NEW.payload)
                   OR NEW.sample_count <> (NEW.payload->>'path_count')::INTEGER
                   OR NEW.payload->>'path_count' IS DISTINCT FROM result_outcome->'checks'->'combinatorial_paths'->>'path_count'
                   OR NEW.payload->'path_records' IS DISTINCT FROM result_outcome->'checks'->'combinatorial_paths'->'path_records'
                THEN RAISE EXCEPTION 'CPCV evidence paths are incomplete'; END IF;
            ELSIF NEW.evidence_kind = 'neutralization' THEN
                IF jsonb_typeof(NEW.payload->'samples') <> 'array'
                   OR jsonb_array_length(NEW.payload->'samples') = 0
                   OR NEW.sample_count <> jsonb_array_length(NEW.payload->'samples')
                   OR NEW.payload->'samples' IS DISTINCT FROM result_outcome->'checks'->'neutralization'->'samples'
                THEN RAISE EXCEPTION 'neutralization evidence samples are incomplete'; END IF;
            ELSIF NEW.evidence_kind = 'parameter_stability' THEN
                IF jsonb_typeof(NEW.payload->'samples') <> 'array'
                   OR jsonb_array_length(NEW.payload->'samples') < 3
                   OR NEW.sample_count <> jsonb_array_length(NEW.payload->'samples')
                   OR NEW.payload->'samples' IS DISTINCT FROM result_outcome->'checks'->'parameter_stability'->'samples'
                THEN RAISE EXCEPTION 'parameter stability evidence samples are incomplete'; END IF;
            ELSIF NEW.evidence_kind = 'mechanism_falsification' THEN
                IF jsonb_typeof(NEW.payload->'samples') <> 'array'
                   OR jsonb_array_length(NEW.payload->'samples') = 0
                   OR NEW.sample_count <> jsonb_array_length(NEW.payload->'samples')
                   OR NEW.payload->'samples' IS DISTINCT FROM result_outcome->'checks'->'mechanism'->'evidence_samples'
                THEN RAISE EXCEPTION 'mechanism and falsification evidence samples are incomplete'; END IF;
            ELSIF NEW.evidence_kind = 'multiple_testing' THEN
                IF jsonb_typeof(NEW.payload->'path_returns') <> 'array'
                   OR jsonb_typeof(NEW.payload->'p_values') <> 'array'
                   OR jsonb_array_length(NEW.payload->'path_returns') = 0
                   OR jsonb_array_length(NEW.payload->'p_values') = 0
                   OR jsonb_typeof(NEW.payload->'metrics') <> 'object'
                   OR (NEW.payload->'metrics'->>'domain_valid') <> 'true'
                   OR NOT (NEW.payload->'metrics' ?& ARRAY['psr', 'dsr', 'pbo', 'data_snooping_probability', 'fdr_q_value'])
                   OR NEW.sample_count <> jsonb_array_length(NEW.payload->'path_returns')
                   OR NEW.payload->'path_returns' IS DISTINCT FROM result_outcome->'checks'->'multiple_testing'->'path_returns'
                   OR NEW.payload->'p_values' IS DISTINCT FROM result_outcome->'checks'->'multiple_testing'->'p_values'
                   OR NEW.payload->'metrics' IS DISTINCT FROM result_outcome->'checks'->'multiple_testing'
                THEN RAISE EXCEPTION 'multiple-testing evidence is incomplete'; END IF;
            END IF;
            NEW.created_at := actual;
            NEW.available_at := actual;
            expected_hash := analysis.research_evidence_hash(
                NEW.research_trial_id, NEW.trial_result_id, NEW.evidence_kind,
                NEW.evaluator_id, NEW.sample_count, NEW.domain_valid, NEW.payload
            );
            NEW.evidence_hash := expected_hash;
            RETURN NEW;
        END;
        $evidence$;
        CREATE TRIGGER enforce_research_evidence_manifest
            BEFORE INSERT OR UPDATE OR DELETE ON analysis.research_evidence_manifest
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_evidence_manifest();

        CREATE OR REPLACE FUNCTION analysis.research_evidence_complete(result_uuid UUID)
        RETURNS BOOLEAN LANGUAGE sql STABLE AS $complete$
            SELECT count(*) = 6 AND bool_and(
                evidence.research_trial_id = result.research_trial_id
                AND evidence.domain_valid
                AND evidence.sample_count > 0
                AND evidence.evidence_hash = analysis.research_evidence_hash(
                    evidence.research_trial_id, evidence.trial_result_id,
                    evidence.evidence_kind, evidence.evaluator_id,
                    evidence.sample_count, evidence.domain_valid, evidence.payload
                )
                AND evidence.payload->>'trial_input_hash' = result.input_hash
            )
            FROM analysis.research_evidence_manifest evidence
            JOIN analysis.trial_result result ON result.id = result_uuid
            WHERE evidence.trial_result_id = result_uuid;
        $complete$;

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
                  AND analysis.research_evidence_complete(result.id)
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
            WHERE gate.dossier_id = dossier_uuid;
        $gates$;

        CREATE OR REPLACE FUNCTION analysis.canonical_forecast_number(value DOUBLE PRECISION)
        RETURNS TEXT LANGUAGE plpgsql IMMUTABLE AS $number$
        DECLARE text_value TEXT;
        BEGIN
            IF value IS NULL THEN RETURN NULL; END IF;
            IF value <> value OR value = 'Infinity'::DOUBLE PRECISION OR value = '-Infinity'::DOUBLE PRECISION THEN
                RAISE EXCEPTION 'forecast numeric payload must be finite';
            END IF;
            text_value := to_char(value::NUMERIC, 'FM999999999999999999999999999999999999990D999999999999999999999999999999999999999');
            text_value := rtrim(rtrim(text_value, '0'), '.');
            RETURN CASE WHEN text_value IN ('', '-0') THEN '0' ELSE text_value END;
        END;
        $number$;

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
        $forecast$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS enforce_research_evidence_manifest ON analysis.research_evidence_manifest;
        DROP FUNCTION IF EXISTS analysis.enforce_research_evidence_manifest();
        DROP FUNCTION IF EXISTS analysis.research_evidence_complete(UUID);
        DROP FUNCTION IF EXISTS analysis.research_evidence_hash(UUID, UUID, TEXT, TEXT, INTEGER, BOOLEAN, JSONB);
        DROP FUNCTION IF EXISTS analysis.canonical_forecast_number(DOUBLE PRECISION);
        DROP TABLE IF EXISTS analysis.research_evidence_manifest;
        """
    )
