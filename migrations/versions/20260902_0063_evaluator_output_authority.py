"""Make evaluator output the immutable source of research evidence."""

from __future__ import annotations

from alembic import op


revision = "20260902_0063"
down_revision = "20260902_0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE analysis.research_evaluator_output (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            research_trial_id UUID NOT NULL REFERENCES analysis.research_trial(id),
            trial_result_id UUID NOT NULL REFERENCES analysis.trial_result(id),
            analysis_run_id UUID NOT NULL REFERENCES analysis.run(id),
            evidence_kind TEXT NOT NULL CHECK (evidence_kind IN (
                'controls', 'cpcv_paths', 'neutralization',
                'parameter_stability', 'mechanism_falsification', 'multiple_testing'
            )),
            evaluator_id TEXT NOT NULL,
            evaluator_code_version TEXT NOT NULL,
            input_hash CHAR(64) NOT NULL,
            universe_hash CHAR(64) NOT NULL,
            feature_hash CHAR(64) NOT NULL,
            sample_count INTEGER NOT NULL CHECK (sample_count > 0),
            domain_valid BOOLEAN NOT NULL,
            raw_output JSONB NOT NULL CHECK (jsonb_typeof(raw_output) = 'object'),
            output_hash CHAR(64) NOT NULL DEFAULT repeat('0', 64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (trial_result_id, evidence_kind),
            CHECK (input_hash ~ '^[0-9a-fA-F]{64}$' AND lower(input_hash) <> repeat('0', 64)),
            CHECK (universe_hash ~ '^[0-9a-fA-F]{64}$' AND lower(universe_hash) <> repeat('0', 64)),
            CHECK (feature_hash ~ '^[0-9a-fA-F]{64}$' AND lower(feature_hash) <> repeat('0', 64)),
            CHECK (available_at >= created_at)
        );
        CREATE INDEX ix_research_evaluator_output_trial
            ON analysis.research_evaluator_output (research_trial_id, trial_result_id, evidence_kind);

        CREATE OR REPLACE FUNCTION analysis.research_evaluator_output_hash(
            trial_id UUID, result_id UUID, run_id UUID, kind TEXT,
            evaluator TEXT, code_version TEXT, input_digest TEXT,
            universe_digest TEXT, feature_digest TEXT, samples INTEGER,
            valid BOOLEAN, output JSONB
        ) RETURNS TEXT LANGUAGE sql IMMUTABLE AS $hash$
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
        $hash$;

        CREATE OR REPLACE FUNCTION analysis.enforce_research_evaluator_output()
        RETURNS trigger LANGUAGE plpgsql AS $output$
        DECLARE
            result_trial UUID; result_hash TEXT; trial_cutoff TIMESTAMPTZ;
            run_input_hash TEXT; run_cutoff TIMESTAMPTZ; run_code TEXT;
            universe_digest TEXT; actual TIMESTAMPTZ := clock_timestamp();
            expected_hash TEXT;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'research evaluator outputs are immutable';
            END IF;
            SELECT result.research_trial_id, result.input_hash, trial.input_cutoff
              INTO result_trial, result_hash, trial_cutoff
            FROM analysis.trial_result result
            JOIN analysis.research_trial trial ON trial.id = result.research_trial_id
            WHERE result.id = NEW.trial_result_id;
            SELECT input_hash, input_cutoff, code_version
              INTO run_input_hash, run_cutoff, run_code
            FROM analysis.run WHERE id = NEW.analysis_run_id;
            SELECT manifest.manifest_hash INTO universe_digest
            FROM analysis.trial_universe_manifest manifest
            WHERE manifest.research_trial_id = result_trial;
            IF result_trial IS NULL
               OR result_trial <> NEW.research_trial_id
               OR NEW.analysis_run_id IS NULL
               OR run_input_hash IS DISTINCT FROM result_hash
               OR run_cutoff IS DISTINCT FROM trial_cutoff
               OR NEW.evaluator_id IS NULL OR length(trim(NEW.evaluator_id)) < 3
               OR NEW.evaluator_code_version IS NULL OR length(trim(NEW.evaluator_code_version)) < 3
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
               OR NOT NEW.domain_valid
               OR NEW.sample_count <= 0
            THEN
                RAISE EXCEPTION 'evaluator output is not linked to the authoritative trial and run';
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
                   OR EXISTS (SELECT 1 FROM unnest(ARRAY['psr', 'dsr', 'pbo', 'data_snooping_probability', 'fdr_q_value']) AS metric(name)
                              WHERE jsonb_typeof(NEW.raw_output->'metrics'->metric.name) <> 'number'
                                OR (NEW.raw_output->'metrics'->>metric.name)::DOUBLE PRECISION NOT BETWEEN 0 AND 1)
                   OR NEW.sample_count <> jsonb_array_length(NEW.raw_output->'path_returns')
                THEN RAISE EXCEPTION 'multiple-testing evaluator output is incomplete'; END IF;
            END IF;
            -- The database is the clock authority. Caller timestamps are never
            -- used to make historical evidence appear available.
            NEW.created_at := actual;
            NEW.available_at := actual;
            expected_hash := analysis.research_evaluator_output_hash(
                NEW.research_trial_id, NEW.trial_result_id, NEW.analysis_run_id,
                NEW.evidence_kind, NEW.evaluator_id, NEW.evaluator_code_version,
                NEW.input_hash, NEW.universe_hash, NEW.feature_hash,
                NEW.sample_count, NEW.domain_valid, NEW.raw_output
            );
            NEW.output_hash := expected_hash;
            RETURN NEW;
        END;
        $output$;
        CREATE TRIGGER enforce_research_evaluator_output
            BEFORE INSERT OR UPDATE OR DELETE ON analysis.research_evaluator_output
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_evaluator_output();

        ALTER TABLE analysis.research_evidence_manifest
            ADD COLUMN evaluator_output_id UUID REFERENCES analysis.research_evaluator_output(id),
            ADD COLUMN evaluator_code_version TEXT,
            ADD COLUMN input_hash CHAR(64),
            ADD COLUMN universe_hash CHAR(64),
            ADD COLUMN feature_hash CHAR(64);

        CREATE OR REPLACE FUNCTION analysis.enforce_research_evidence_manifest()
        RETURNS trigger LANGUAGE plpgsql AS $evidence$
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
        $evidence$;

        DROP TRIGGER IF EXISTS enforce_research_evidence_manifest ON analysis.research_evidence_manifest;
        CREATE TRIGGER enforce_research_evidence_manifest
            BEFORE INSERT OR UPDATE OR DELETE ON analysis.research_evidence_manifest
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_evidence_manifest();

        CREATE OR REPLACE FUNCTION analysis.research_evidence_complete(result_uuid UUID)
        RETURNS BOOLEAN LANGUAGE sql STABLE AS $complete$
            SELECT count(*) = 6 AND bool_and(
                manifest.evaluator_output_id IS NOT NULL
                AND source.id IS NOT NULL
                AND source.output_hash = manifest.evidence_hash
                AND source.output_hash = analysis.research_evaluator_output_hash(
                    source.research_trial_id, source.trial_result_id,
                    source.analysis_run_id, source.evidence_kind,
                    source.evaluator_id, source.evaluator_code_version,
                    source.input_hash, source.universe_hash, source.feature_hash,
                    source.sample_count, source.domain_valid, source.raw_output
                )
                AND manifest.payload = source.raw_output
                AND source.trial_result_id = result_uuid
                AND source.domain_valid AND source.sample_count > 0
            )
            FROM analysis.research_evidence_manifest manifest
            LEFT JOIN analysis.research_evaluator_output source ON source.id = manifest.evaluator_output_id
            JOIN analysis.trial_result result ON result.id = result_uuid
            JOIN analysis.research_trial trial ON trial.id = result.research_trial_id
            WHERE manifest.trial_result_id = result_uuid;
        $complete$;

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
            JOIN analysis.research_trial trial ON trial.id = result.research_trial_id
            WHERE gate.dossier_id = dossier_uuid;
        $gates$;

        CREATE OR REPLACE FUNCTION analysis.research_validation_evidence_complete(result_uuid UUID, expected_attempt_count INTEGER)
        RETURNS BOOLEAN LANGUAGE sql STABLE AS $evidence$
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
        $evidence$;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        DROP TRIGGER IF EXISTS enforce_research_evidence_manifest ON analysis.research_evidence_manifest;
        DROP FUNCTION IF EXISTS analysis.enforce_research_evidence_manifest();
        DROP FUNCTION IF EXISTS analysis.research_evidence_complete(UUID);
        DROP FUNCTION IF EXISTS analysis.research_validation_evidence_complete(UUID, INTEGER);
        ALTER TABLE analysis.research_evidence_manifest
            DROP COLUMN IF EXISTS evaluator_output_id,
            DROP COLUMN IF EXISTS evaluator_code_version,
            DROP COLUMN IF EXISTS input_hash,
            DROP COLUMN IF EXISTS universe_hash,
            DROP COLUMN IF EXISTS feature_hash;
        DROP TRIGGER IF EXISTS enforce_research_evaluator_output ON analysis.research_evaluator_output;
        DROP FUNCTION IF EXISTS analysis.enforce_research_evaluator_output();
        DROP FUNCTION IF EXISTS analysis.research_evaluator_output_hash(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB);
        DROP TABLE IF EXISTS analysis.research_evaluator_output;
        """
    )
