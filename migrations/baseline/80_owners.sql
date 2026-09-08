-- Protected object ownership.

ALTER FUNCTION analysis.enforce_research_evaluator_output() OWNER TO market_research_signer;

ALTER FUNCTION analysis.enforce_research_evidence_manifest() OWNER TO market_research_signer;

ALTER FUNCTION analysis.enforce_research_gate_actual_availability() OWNER TO market_research_signer;

ALTER FUNCTION analysis.enforce_research_result_actual_availability() OWNER TO market_research_signer;

ALTER FUNCTION analysis.enforce_research_revision_promotion() OWNER TO market_research_signer;

ALTER FUNCTION analysis.enforce_research_revision_promotion_hardened() OWNER TO market_research_signer;

ALTER FUNCTION analysis.enforce_research_trial_terminal_immutability() OWNER TO market_research_signer;

ALTER FUNCTION analysis.enforce_research_universe_actual_availability() OWNER TO market_research_signer;

ALTER FUNCTION analysis.enforce_strategy_forecast_authority() OWNER TO market_research_signer;

ALTER FUNCTION analysis.enforce_validation_dossier_seal() OWNER TO market_research_signer;

ALTER FUNCTION analysis.research_evaluator_authorization_payload(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb) OWNER TO market_research_signer;

ALTER FUNCTION analysis.research_evaluator_output_hash_v2(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb, available timestamp with time zone) OWNER TO market_research_signer;

ALTER FUNCTION analysis.research_evaluator_signature_payload(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output_digest text, available timestamp with time zone) OWNER TO market_research_signer;

ALTER FUNCTION analysis.research_evaluator_signing_key() OWNER TO market_research_signer;

ALTER FUNCTION analysis.research_evidence_complete(result_uuid uuid) OWNER TO market_research_signer;

ALTER FUNCTION analysis.research_validation_evidence_complete(result_uuid uuid, expected_attempt_count integer) OWNER TO market_research_signer;

ALTER FUNCTION analysis.write_research_evaluator_output(p_trial_id uuid, p_result_id uuid, p_run_id uuid, p_kind text, p_evaluator text, p_code_version text, p_input_digest text, p_universe_digest text, p_feature_digest text, p_samples integer, p_valid boolean, p_output jsonb, p_authorization_signature text) OWNER TO market_research_signer;

ALTER FUNCTION ingest.record_source_lifecycle() OWNER TO market_migrator;

ALTER TABLE analysis.research_evaluator_output OWNER TO market_research_signer;

ALTER TABLE analysis.research_evaluator_signing_secret OWNER TO market_research_signer;
