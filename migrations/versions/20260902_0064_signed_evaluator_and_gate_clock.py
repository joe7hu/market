"""Require signed evaluator output and database-owned gate timestamps."""

from __future__ import annotations

from alembic import op


revision = "20260902_0064"
down_revision = "20260902_0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        ALTER TABLE analysis.research_evaluator_output
            ADD COLUMN signature TEXT NOT NULL DEFAULT '';

        CREATE OR REPLACE FUNCTION analysis.research_evaluator_signature_payload(
            trial_id UUID, result_id UUID, run_id UUID, kind TEXT,
            evaluator TEXT, code_version TEXT, input_digest TEXT,
            universe_digest TEXT, feature_digest TEXT, samples INTEGER,
            valid BOOLEAN, output_digest TEXT, available TIMESTAMPTZ
        ) RETURNS TEXT LANGUAGE sql IMMUTABLE AS $payload$
            SELECT concat_ws(chr(31),
                'research-evaluator-signature.v1', trial_id::TEXT, result_id::TEXT,
                run_id::TEXT, kind, evaluator, code_version, input_digest,
                universe_digest, feature_digest, samples::TEXT,
                CASE WHEN valid THEN 'true' ELSE 'false' END, output_digest,
                to_char(available AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || 'Z'
            );
        $payload$;

        CREATE OR REPLACE FUNCTION analysis.research_evaluator_output_hash_v2(
            trial_id UUID, result_id UUID, run_id UUID, kind TEXT,
            evaluator TEXT, code_version TEXT, input_digest TEXT,
            universe_digest TEXT, feature_digest TEXT, samples INTEGER,
            valid BOOLEAN, output JSONB, available TIMESTAMPTZ
        ) RETURNS TEXT LANGUAGE sql IMMUTABLE AS $hash$
            SELECT encode(digest(jsonb_build_object(
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
        $hash$;

        CREATE OR REPLACE FUNCTION analysis.enforce_research_evaluator_output()
        RETURNS trigger LANGUAGE plpgsql AS $output$
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
            signing_key := NULLIF(current_setting('app.research_evaluator_signing_key', true), '');
            IF signing_key IS NULL THEN
                RAISE EXCEPTION 'research evaluator signing key is not configured';
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
                RAISE EXCEPTION 'signed evaluator output is not linked to the authoritative trial and run';
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
            expected_signature := encode(hmac(
                analysis.research_evaluator_signature_payload(
                    NEW.research_trial_id, NEW.trial_result_id, NEW.analysis_run_id,
                    NEW.evidence_kind, NEW.evaluator_id, NEW.evaluator_code_version,
                    NEW.input_hash, NEW.universe_hash, NEW.feature_hash,
                    NEW.sample_count, NEW.domain_valid, expected_hash, NEW.available_at
                ), signing_key, 'sha256'
            ), 'hex');
            IF lower(NEW.output_hash) <> expected_hash OR lower(NEW.signature) <> expected_signature THEN
                RAISE EXCEPTION 'evaluator output signature or content hash is invalid';
            END IF;
            -- Preserve the exact signed availability sample. The trigger
            -- rejects future values before accepting this immutable clock.
            NEW.created_at := NEW.available_at;
            NEW.output_hash := expected_hash;
            NEW.signature := expected_signature;
            RETURN NEW;
        END;
        $output$;

        CREATE OR REPLACE FUNCTION analysis.research_evidence_complete(result_uuid UUID)
        RETURNS BOOLEAN LANGUAGE sql STABLE AS $complete$
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
                AND source.signature = encode(hmac(
                    analysis.research_evaluator_signature_payload(
                        source.research_trial_id, source.trial_result_id,
                        source.analysis_run_id, source.evidence_kind,
                        source.evaluator_id, source.evaluator_code_version,
                        source.input_hash, source.universe_hash, source.feature_hash,
                        source.sample_count, source.domain_valid, source.output_hash,
                        source.available_at
                    ), NULLIF(current_setting('app.research_evaluator_signing_key', true), ''), 'sha256'
                ), 'hex')
                AND manifest.payload = source.raw_output
                AND source.trial_result_id = result_uuid
                AND source.domain_valid AND source.sample_count > 0
            )
            FROM analysis.research_evidence_manifest manifest
            LEFT JOIN analysis.research_evaluator_output source ON source.id = manifest.evaluator_output_id
            WHERE manifest.trial_result_id = result_uuid;
        $complete$;

        ALTER TABLE analysis.validation_gate_result
            ALTER COLUMN evaluated_at DROP DEFAULT,
            ALTER COLUMN available_at DROP DEFAULT;
        CREATE OR REPLACE FUNCTION analysis.assign_research_gate_actual_clock()
        RETURNS trigger LANGUAGE plpgsql AS $gate_clock$
        DECLARE actual TIMESTAMPTZ := clock_timestamp();
        BEGIN
            IF NEW.evaluated_at IS NOT NULL OR NEW.available_at IS NOT NULL THEN
                RAISE EXCEPTION 'validation gate timestamps are database-owned';
            END IF;
            NEW.evaluated_at := actual;
            NEW.available_at := actual;
            RETURN NEW;
        END;
        $gate_clock$;
        DROP TRIGGER IF EXISTS assign_research_gate_actual_clock ON analysis.validation_gate_result;
        CREATE TRIGGER assign_research_gate_actual_clock
            BEFORE INSERT ON analysis.validation_gate_result
            FOR EACH ROW EXECUTE FUNCTION analysis.assign_research_gate_actual_clock();

        CREATE OR REPLACE FUNCTION analysis.enforce_research_gate_promotion_clock()
        RETURNS trigger LANGUAGE plpgsql AS $promotion_clock$
        BEGIN
            IF NEW.status = 'active' AND (NEW.research_required OR NEW.hypothesis_id IS NOT NULL OR NEW.experiment_family_id IS NOT NULL) AND EXISTS (
                SELECT 1
                FROM analysis.validation_dossier dossier
                JOIN analysis.research_trial trial ON trial.id = dossier.research_trial_id
                JOIN analysis.validation_gate_result gate ON gate.dossier_id = dossier.id
                WHERE dossier.strategy_revision_id = NEW.id
                  AND (gate.evaluated_at > trial.input_cutoff OR gate.available_at > trial.input_cutoff)
            ) THEN
                RAISE EXCEPTION 'promotion requires database-authoritative gate timestamps at or before trial cutoff';
            END IF;
            RETURN NEW;
        END;
        $promotion_clock$;
        DROP TRIGGER IF EXISTS enforce_research_gate_promotion_clock ON analysis.strategy_revision;
        CREATE TRIGGER enforce_research_gate_promotion_clock
            BEFORE UPDATE ON analysis.strategy_revision
            FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_gate_promotion_clock();

        CREATE OR REPLACE FUNCTION analysis.canonical_forecast_number(value DOUBLE PRECISION)
        RETURNS TEXT LANGUAGE plpgsql IMMUTABLE AS $number$
        DECLARE text_value TEXT;
        BEGIN
            IF value IS NULL THEN RETURN NULL; END IF;
            IF value = 0 THEN RETURN '0'; END IF;
            IF value <> value OR value = 'Infinity'::DOUBLE PRECISION OR value = '-Infinity'::DOUBLE PRECISION THEN
                RAISE EXCEPTION 'forecast numeric payload must be finite';
            END IF;
            text_value := to_char(value::NUMERIC, 'FM999999999999999999999999999999999999990D999999999999999999999999999999999999999');
            text_value := rtrim(rtrim(text_value, '0'), '.');
            RETURN CASE WHEN text_value IN ('', '-0') THEN '0' ELSE text_value END;
        END;
        $number$;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        DROP TRIGGER IF EXISTS enforce_research_gate_promotion_clock ON analysis.strategy_revision;
        DROP FUNCTION IF EXISTS analysis.enforce_research_gate_promotion_clock();
        DROP TRIGGER IF EXISTS assign_research_gate_actual_clock ON analysis.validation_gate_result;
        DROP FUNCTION IF EXISTS analysis.assign_research_gate_actual_clock();
        ALTER TABLE analysis.validation_gate_result
            ALTER COLUMN evaluated_at SET DEFAULT now(),
            ALTER COLUMN available_at SET DEFAULT now();
        DROP TRIGGER IF EXISTS enforce_research_evaluator_output ON analysis.research_evaluator_output;
        DROP FUNCTION IF EXISTS analysis.enforce_research_evaluator_output();
        DROP FUNCTION IF EXISTS analysis.research_evaluator_output_hash_v2(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB, TIMESTAMPTZ);
        DROP FUNCTION IF EXISTS analysis.research_evaluator_signature_payload(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, TEXT, TIMESTAMPTZ);
        ALTER TABLE analysis.research_evaluator_output DROP COLUMN IF EXISTS signature;
        """
    )
