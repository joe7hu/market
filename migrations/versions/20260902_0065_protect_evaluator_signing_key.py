"""Move evaluator signature verification behind a protected database key."""

from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa


revision = "20260902_0065"
down_revision = "20260902_0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE analysis.research_evaluator_signing_secret (
            singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
            secret BYTEA NOT NULL CHECK (length(secret) >= 16),
            installed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        );
        REVOKE ALL ON analysis.research_evaluator_signing_secret FROM PUBLIC;

        CREATE OR REPLACE FUNCTION analysis.research_evaluator_signing_key()
        RETURNS TEXT
        LANGUAGE sql
        SECURITY DEFINER
        STABLE
        SET search_path = pg_catalog, analysis
        AS $key$
            SELECT convert_from(secret, 'UTF8')
            FROM analysis.research_evaluator_signing_secret
            WHERE singleton
        $key$;
        REVOKE ALL ON FUNCTION analysis.research_evaluator_signing_key() FROM PUBLIC;

        CREATE OR REPLACE FUNCTION analysis.research_evaluator_output_hash_v2(
            trial_id UUID, result_id UUID, run_id UUID, kind TEXT,
            evaluator TEXT, code_version TEXT, input_digest TEXT,
            universe_digest TEXT, feature_digest TEXT, samples INTEGER,
            valid BOOLEAN, output JSONB, available TIMESTAMPTZ
        ) RETURNS TEXT LANGUAGE sql IMMUTABLE AS $hash$
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
        $output$;

        CREATE OR REPLACE FUNCTION analysis.research_evaluator_authorization_payload(
            trial_id UUID, result_id UUID, run_id UUID, kind TEXT,
            evaluator TEXT, code_version TEXT, input_digest TEXT,
            universe_digest TEXT, feature_digest TEXT, samples INTEGER,
            valid BOOLEAN, output JSONB
        ) RETURNS TEXT LANGUAGE sql IMMUTABLE AS $authorization$
            SELECT concat_ws(chr(31),
                'research-evaluator-authorization.v1', trial_id::TEXT,
                result_id::TEXT, run_id::TEXT, kind, evaluator, code_version,
                input_digest, universe_digest, feature_digest, samples::TEXT,
                CASE WHEN valid THEN 'true' ELSE 'false' END, output::TEXT
            );
        $authorization$;

        CREATE OR REPLACE FUNCTION analysis.write_research_evaluator_output(
            trial_id UUID, result_id UUID, run_id UUID, kind TEXT,
            evaluator TEXT, code_version TEXT, input_digest TEXT,
            universe_digest TEXT, feature_digest TEXT, samples INTEGER,
            valid BOOLEAN, output JSONB, authorization_signature TEXT
        ) RETURNS TABLE(id UUID, output_hash TEXT, available_at TIMESTAMPTZ)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, analysis
        AS $writer$
        DECLARE
            signing_key TEXT; expected_authorization TEXT;
            actual TIMESTAMPTZ; content_digest TEXT; final_signature TEXT;
        BEGIN
            signing_key := analysis.research_evaluator_signing_key();
            IF signing_key IS NULL OR length(signing_key) < 16 THEN
                RAISE EXCEPTION 'protected research evaluator signing key is not configured';
            END IF;
            expected_authorization := encode(public.hmac(
                convert_to(analysis.research_evaluator_authorization_payload(
                    trial_id, result_id, run_id, kind, evaluator, code_version,
                    input_digest, universe_digest, feature_digest, samples,
                    valid, output
                ), 'UTF8'), convert_to(signing_key, 'UTF8'), 'sha256'::TEXT
            ), 'hex');
            IF authorization_signature IS NULL
               OR lower(authorization_signature) <> expected_authorization THEN
                RAISE EXCEPTION 'research evaluator authorization signature is invalid';
            END IF;
            actual := clock_timestamp();
            content_digest := analysis.research_evaluator_output_hash_v2(
                trial_id, result_id, run_id, kind, evaluator, code_version,
                input_digest, universe_digest, feature_digest, samples, valid,
                output, actual
            );
            final_signature := encode(public.hmac(
                convert_to(analysis.research_evaluator_signature_payload(
                    trial_id, result_id, run_id, kind, evaluator, code_version,
                    input_digest, universe_digest, feature_digest, samples, valid,
                    content_digest, actual
                ), 'UTF8'), convert_to(signing_key, 'UTF8'), 'sha256'::TEXT
            ), 'hex');
            RETURN QUERY
            INSERT INTO analysis.research_evaluator_output(
                research_trial_id, trial_result_id, analysis_run_id, evidence_kind,
                evaluator_id, evaluator_code_version, input_hash, universe_hash,
                feature_hash, sample_count, domain_valid, raw_output,
                output_hash, signature, available_at
            ) VALUES (
                trial_id, result_id, run_id, kind, evaluator, code_version,
                input_digest, universe_digest, feature_digest, samples, valid,
                output, content_digest, final_signature, actual
            )
            RETURNING analysis.research_evaluator_output.id,
                      analysis.research_evaluator_output.output_hash::TEXT,
                      analysis.research_evaluator_output.available_at;
        END;
        $writer$;
        REVOKE ALL ON FUNCTION analysis.research_evaluator_authorization_payload(
            UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB
        ) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION analysis.write_research_evaluator_output(
            UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB, TEXT
        ) TO PUBLIC;
        REVOKE INSERT, UPDATE, DELETE ON analysis.research_evaluator_output FROM PUBLIC;

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
        $complete$;
        """
    )
    key = os.environ.get("MARKET_RESEARCH_EVALUATOR_SIGNING_KEY", "").strip()
    if key:
        if len(key) < 16:
            raise RuntimeError("MARKET_RESEARCH_EVALUATOR_SIGNING_KEY must contain at least 16 characters")
        op.get_bind().execute(
            sa.text(
                """INSERT INTO analysis.research_evaluator_signing_secret (singleton, secret)
                   VALUES (true, convert_to(:secret, 'UTF8'))
                   ON CONFLICT (singleton) DO UPDATE SET secret = EXCLUDED.secret, installed_at = clock_timestamp()"""
            ),
            {"secret": key},
        )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION IF EXISTS analysis.research_evaluator_signing_key();
        DROP FUNCTION IF EXISTS analysis.write_research_evaluator_output(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB, TEXT);
        DROP FUNCTION IF EXISTS analysis.research_evaluator_authorization_payload(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB);
        DROP TABLE IF EXISTS analysis.research_evaluator_signing_secret;
        """
    )
