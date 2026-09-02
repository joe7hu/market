"""Put evaluator key and write authority behind distinct database roles."""

from __future__ import annotations

from alembic import op


revision = "20260902_0066"
down_revision = "20260902_0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        -- This is the explicit privileged setup path. A migration caller
        -- without CREATEROLE fails here; no owner-role fallback is allowed.
        DO $roles$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'market_research_signer') THEN
                CREATE ROLE market_research_signer NOLOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'market_app') THEN
                CREATE ROLE market_app NOLOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'market_migrator') THEN
                CREATE ROLE market_migrator NOLOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'market_research_signer' AND (rolcanlogin OR rolsuper OR rolbypassrls OR rolinherit)) THEN
                RAISE EXCEPTION 'protected evaluator role has unsafe login, inheritance, superuser, or bypass-RLS privileges';
            END IF;
        END
        $roles$;

        ALTER TABLE analysis.research_evaluator_signing_secret OWNER TO market_research_signer;
        ALTER TABLE analysis.research_evaluator_output OWNER TO market_research_signer;
        -- SECURITY DEFINER executes with the protected owner as current_user.
        -- Grant the namespace privileges explicitly after role provisioning;
        -- schema ACLs must not depend on the migration or application role.
        GRANT USAGE ON SCHEMA analysis, public TO market_research_signer;
        GRANT SELECT ON analysis.research_trial,
                       analysis.trial_result,
                       analysis.run,
                       analysis.trial_universe_manifest,
                       analysis.universe_observation,
                       analysis.experiment_manifest,
                       analysis.experiment_family,
                       analysis.research_evaluator_output,
                       analysis.research_evidence_manifest
          TO market_research_signer;

        ALTER FUNCTION analysis.research_evaluator_signing_key() OWNER TO market_research_signer;
        ALTER FUNCTION analysis.research_evaluator_signing_key() SECURITY DEFINER;
        ALTER FUNCTION analysis.research_evaluator_output_hash_v2(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB, TIMESTAMPTZ) OWNER TO market_research_signer;
        ALTER FUNCTION analysis.research_evaluator_signature_payload(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, TEXT, TIMESTAMPTZ) OWNER TO market_research_signer;
        ALTER FUNCTION analysis.enforce_research_evaluator_output() OWNER TO market_research_signer;
        ALTER FUNCTION analysis.enforce_research_evaluator_output() SECURITY DEFINER;
        ALTER FUNCTION analysis.enforce_research_evaluator_output() SET search_path = pg_catalog, analysis;
        ALTER FUNCTION analysis.enforce_research_evidence_manifest() OWNER TO market_research_signer;
        ALTER FUNCTION analysis.enforce_research_evidence_manifest() SECURITY DEFINER;
        ALTER FUNCTION analysis.enforce_research_evidence_manifest() SET search_path = pg_catalog, analysis;

        -- The restricted security-definer search path must use the explicit
        -- pgcrypto bytea signature. The older helpers used an unqualified
        -- digest(text, text), which becomes an undefined function when a
        -- protected owner evaluates promotion evidence.
        CREATE OR REPLACE FUNCTION analysis.research_trial_universe_complete(trial_id UUID)
        RETURNS BOOLEAN LANGUAGE sql STABLE
        SET search_path = pg_catalog, analysis, public AS $universe$
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
        $universe$;

        CREATE OR REPLACE FUNCTION analysis.research_family_complete(family_id UUID)
        RETURNS BOOLEAN LANGUAGE sql STABLE
        SET search_path = pg_catalog, analysis, public AS $family$
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
        $family$;

        DROP FUNCTION analysis.write_research_evaluator_output(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB, TEXT);
        CREATE OR REPLACE FUNCTION analysis.write_research_evaluator_output(
            p_trial_id UUID, p_result_id UUID, p_run_id UUID, p_kind TEXT,
            p_evaluator TEXT, p_code_version TEXT, p_input_digest TEXT,
            p_universe_digest TEXT, p_feature_digest TEXT, p_samples INTEGER,
            p_valid BOOLEAN, p_output JSONB, p_authorization_signature TEXT
        ) RETURNS TABLE(id UUID, output_hash TEXT, available_at TIMESTAMPTZ)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, analysis
        AS $writer$
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
        $writer$;

        ALTER FUNCTION analysis.write_research_evaluator_output(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB, TEXT) OWNER TO market_research_signer;
        ALTER FUNCTION analysis.research_evaluator_authorization_payload(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB) OWNER TO market_research_signer;
        REVOKE ALL ON FUNCTION analysis.research_evaluator_authorization_payload(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB) FROM PUBLIC, market_migrator;
        REVOKE ALL ON FUNCTION analysis.write_research_evaluator_output(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB, TEXT) FROM PUBLIC, market_migrator;
        GRANT USAGE ON SCHEMA analysis TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.research_evaluator_authorization_payload(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB) TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.write_research_evaluator_output(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB, TEXT) TO market_app;

        REVOKE ALL ON analysis.research_evaluator_signing_secret FROM PUBLIC, market_app, market_migrator;
        REVOKE ALL ON analysis.research_evaluator_output FROM PUBLIC, market_app, market_migrator;
        REVOKE ALL ON FUNCTION analysis.research_evaluator_signing_key() FROM PUBLIC, market_app, market_migrator;
        REVOKE ALL ON FUNCTION analysis.research_evaluator_output_hash_v2(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB, TIMESTAMPTZ) FROM PUBLIC, market_app, market_migrator;
        REVOKE ALL ON FUNCTION analysis.research_evaluator_signature_payload(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, TEXT, TIMESTAMPTZ) FROM PUBLIC, market_app, market_migrator;

        CREATE OR REPLACE FUNCTION analysis.research_evidence_complete(result_uuid UUID)
        RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, analysis AS $complete$
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
        ALTER FUNCTION analysis.research_evidence_complete(UUID) OWNER TO market_research_signer;
        ALTER FUNCTION analysis.research_evidence_complete(UUID) SECURITY DEFINER;
        ALTER FUNCTION analysis.research_evidence_complete(UUID) SET search_path = pg_catalog, analysis;

        CREATE OR REPLACE FUNCTION analysis.research_validation_evidence_complete(result_uuid UUID, expected_attempt_count INTEGER)
        RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, analysis AS $evidence$
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
        ALTER FUNCTION analysis.research_validation_evidence_complete(UUID, INTEGER) OWNER TO market_research_signer;
        ALTER FUNCTION analysis.research_validation_evidence_complete(UUID, INTEGER) SECURITY DEFINER;
        ALTER FUNCTION analysis.research_validation_evidence_complete(UUID, INTEGER) SET search_path = pg_catalog, analysis;
        GRANT EXECUTE ON FUNCTION analysis.research_evidence_complete(UUID) TO market_app;
        REVOKE ALL ON FUNCTION analysis.research_validation_evidence_complete(UUID, INTEGER) FROM PUBLIC, market_app, market_migrator;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        -- Restore ownership to the migration invoker before removing the
        -- role boundary. This keeps downgrade tests and older migration
        -- round-trips reversible without dropping 0065-owned objects.
        DO $reassign$
        DECLARE
            invoker NAME := current_user;
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'market_research_signer') THEN
                EXECUTE format('REASSIGN OWNED BY market_research_signer TO %I', invoker);
                EXECUTE 'DROP OWNED BY market_research_signer';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'market_app') THEN
                EXECUTE 'DROP OWNED BY market_app';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'market_migrator') THEN
                EXECUTE 'DROP OWNED BY market_migrator';
            END IF;
        END
        $reassign$;
        DROP FUNCTION IF EXISTS analysis.write_research_evaluator_output(UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, JSONB, TEXT);
        DROP ROLE IF EXISTS market_app;
        DROP ROLE IF EXISTS market_migrator;
        DROP ROLE IF EXISTS market_research_signer;
        """
    )
