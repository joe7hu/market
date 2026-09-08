-- Privileges: functions.

REVOKE ALL ON FUNCTION analysis.insert_phase4_allocation_item(p jsonb) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.insert_phase4_allocation_snapshot(p jsonb) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.insert_phase4_book_attribution(p jsonb) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.insert_phase4_execution(p_id text, p_allocation_id text, p_model_version text, p_calibration_status text, p_sample_count integer, p_fill_probability double precision, p_spread_bps double precision, p_latency_ms double precision, p_impact_bps double precision, p_input_cutoff timestamp with time zone, p_input_hash text, p_content_hash text, p_metadata jsonb) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.insert_phase4_paper_execution_observation(p jsonb) FROM PUBLIC;

GRANT ALL ON FUNCTION analysis.insert_phase4_scenario(p_id text, p_allocation_id text, p_model_version text, p_probability_semantics text, p_scenarios jsonb, p_tail_dependence jsonb, p_simultaneous_unwind jsonb, p_input_cutoff timestamp with time zone, p_input_hash text, p_content_hash text) TO market_app;

REVOKE ALL ON FUNCTION analysis.phase4_allocation_authorization_payload(p_snapshot jsonb, p_items jsonb) FROM PUBLIC;

GRANT ALL ON FUNCTION analysis.phase4_allocation_authorization_payload(p_snapshot jsonb, p_items jsonb) TO market_app;

REVOKE ALL ON FUNCTION analysis.phase4_allocation_signing_key() FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.phase4_telemetry_authorization_payload(p_contract text, p_payload jsonb) FROM PUBLIC;

GRANT ALL ON FUNCTION analysis.phase4_telemetry_authorization_payload(p_contract text, p_payload jsonb) TO market_app;

REVOKE ALL ON FUNCTION analysis.phase4_telemetry_authorized(p_contract text, p_payload jsonb, p_signature text) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.promote_phase3_strategy(revision_id bigint) FROM PUBLIC;

GRANT ALL ON FUNCTION analysis.promote_phase3_strategy(revision_id bigint) TO market_app;

REVOKE ALL ON FUNCTION analysis.research_evaluator_authorization_payload(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb) FROM PUBLIC;

GRANT ALL ON FUNCTION analysis.research_evaluator_authorization_payload(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb) TO market_app;

REVOKE ALL ON FUNCTION analysis.research_evaluator_output_hash_v2(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output jsonb, available timestamp with time zone) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.research_evaluator_signature_payload(trial_id uuid, result_id uuid, run_id uuid, kind text, evaluator text, code_version text, input_digest text, universe_digest text, feature_digest text, samples integer, valid boolean, output_digest text, available timestamp with time zone) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.research_evaluator_signing_key() FROM PUBLIC;

GRANT ALL ON FUNCTION analysis.research_evidence_complete(result_uuid uuid) TO market_app;

REVOKE ALL ON FUNCTION analysis.research_validation_evidence_complete(result_uuid uuid, expected_attempt_count integer) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.write_phase4_allocation(p_snapshot jsonb, p_items jsonb, p_authorization_signature text) FROM PUBLIC;

GRANT ALL ON FUNCTION analysis.write_phase4_allocation(p_snapshot jsonb, p_items jsonb, p_authorization_signature text) TO market_app;

REVOKE ALL ON FUNCTION analysis.write_phase4_book_attribution(p jsonb, sig text) FROM PUBLIC;

GRANT ALL ON FUNCTION analysis.write_phase4_book_attribution(p jsonb, sig text) TO market_app;

REVOKE ALL ON FUNCTION analysis.write_phase4_execution(p jsonb, sig text) FROM PUBLIC;

GRANT ALL ON FUNCTION analysis.write_phase4_execution(p jsonb, sig text) TO market_app;

REVOKE ALL ON FUNCTION analysis.write_phase4_execution_0077(p jsonb, sig text) FROM PUBLIC;

REVOKE ALL ON FUNCTION analysis.write_phase4_paper_execution(p jsonb, sig text) FROM PUBLIC;

GRANT ALL ON FUNCTION analysis.write_phase4_paper_execution(p jsonb, sig text) TO market_app;

REVOKE ALL ON FUNCTION analysis.write_research_evaluator_output(p_trial_id uuid, p_result_id uuid, p_run_id uuid, p_kind text, p_evaluator text, p_code_version text, p_input_digest text, p_universe_digest text, p_feature_digest text, p_samples integer, p_valid boolean, p_output jsonb, p_authorization_signature text) FROM PUBLIC;

GRANT ALL ON FUNCTION analysis.write_research_evaluator_output(p_trial_id uuid, p_result_id uuid, p_run_id uuid, p_kind text, p_evaluator text, p_code_version text, p_input_digest text, p_universe_digest text, p_feature_digest text, p_samples integer, p_valid boolean, p_output jsonb, p_authorization_signature text) TO market_app;

REVOKE ALL ON FUNCTION ingest.record_source_lifecycle() FROM PUBLIC;

REVOKE ALL ON FUNCTION ingest.reject_identity_update() FROM PUBLIC;

REVOKE ALL ON FUNCTION raw.confirmed_price_bar_at(p_as_of timestamp with time zone, p_instrument_ids bigint[]) FROM PUBLIC;

GRANT ALL ON FUNCTION raw.confirmed_price_bar_at(p_as_of timestamp with time zone, p_instrument_ids bigint[]) TO market_app;

REVOKE ALL ON FUNCTION raw.confirmed_quote_at(p_as_of timestamp with time zone, p_instrument_ids bigint[]) FROM PUBLIC;

GRANT ALL ON FUNCTION raw.confirmed_quote_at(p_as_of timestamp with time zone, p_instrument_ids bigint[]) TO market_app;

GRANT ALL ON FUNCTION raw.current_price_at(p_as_of timestamp with time zone, p_instrument_ids bigint[]) TO market_app;
