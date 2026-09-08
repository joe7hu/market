-- Indexes: analysis schema.

CREATE INDEX ix_agent_run_experiment_arm_started ON analysis.agent_run USING btree (experiment_id, arm, started_at DESC) WHERE (experiment_id IS NOT NULL);

CREATE INDEX ix_agent_task_experiment_arm_created ON analysis.agent_task USING btree (experiment_id, arm, created_at DESC) WHERE (experiment_id IS NOT NULL);

CREATE INDEX ix_analysis_agent_task_decision_id ON analysis.agent_task USING btree (decision_id);

CREATE INDEX ix_analysis_decision_actionable ON analysis.decision USING btree (run_id, kind, score DESC) WHERE (state <> 'REJECT'::text);

CREATE INDEX ix_analysis_decision_instrument ON analysis.decision USING btree (instrument_id, as_of DESC);

CREATE INDEX ix_analysis_decision_recent_page ON analysis.decision USING btree (as_of DESC, id DESC);

CREATE INDEX ix_analysis_decision_run ON analysis.decision USING btree (run_id, kind, state, rank);

CREATE INDEX ix_analysis_decision_scorecard_episode ON analysis.decision USING btree (lane, episode_key, as_of DESC, id DESC) INCLUDE (run_id, state, sample_eligible, quarantine_reason, calibration_cohort) WHERE ((kind = 'option'::text) AND (calibration_cohort ~~ 'option-scorecard-truth-v1:%'::text));

CREATE INDEX ix_analysis_option_anomaly_snapshot ON analysis.option_history_anomaly USING btree (snapshot_id, state, anomaly_type);

CREATE INDEX ix_analysis_option_decision_contract_predecessor ON analysis.option_decision USING btree (contract_id, decision_id);

CREATE INDEX ix_analysis_option_decision_primary_decision_id ON analysis.option_decision USING btree (primary_decision_id);

CREATE INDEX ix_analysis_option_decision_relative_value_id ON analysis.option_decision USING btree (relative_value_id);

CREATE INDEX ix_analysis_option_decision_structure ON analysis.option_decision USING btree (structure);

CREATE INDEX ix_analysis_option_event_signal_decision_id ON analysis.option_event_signal USING btree (decision_id);

CREATE INDEX ix_analysis_option_feature_lookup ON analysis.option_feature USING btree (snapshot_id, contract_id, feature_version);

CREATE INDEX ix_analysis_option_surface_history ON analysis.option_surface_summary USING btree (snapshot_id, expiration, option_type);

CREATE INDEX ix_analysis_outcome_maturity ON analysis.option_outcome USING btree (maturity_state, observed_through);

CREATE INDEX ix_analysis_source_signal_instrument ON analysis.source_signal USING btree (instrument_id, observed_at DESC);

CREATE INDEX ix_analysis_source_signal_pit ON analysis.source_signal USING btree (instrument_id, available_at DESC, observed_at DESC);

CREATE INDEX ix_analysis_symbol_feature_lookup ON analysis.symbol_feature USING btree (instrument_id, as_of DESC, feature_set);

CREATE INDEX ix_decision_lane_episode_asof ON analysis.decision USING btree (lane, episode_key, as_of DESC) WHERE (kind = 'option'::text);

CREATE INDEX ix_event_decision_packet_symbol_as_of ON analysis.event_decision_packet USING btree (symbol, as_of DESC);

CREATE INDEX ix_event_scout_event_symbol_observed ON analysis.event_scout_event USING btree (symbol, observed_at DESC);

CREATE INDEX ix_event_study_feature_lookup ON analysis.event_study_feature USING btree (instrument_id, event_kind, as_of DESC);

CREATE INDEX ix_experiment_family_hypothesis ON analysis.experiment_family USING btree (hypothesis_id, id);

CREATE INDEX ix_hypothesis_available ON analysis.hypothesis USING btree (available_at, id);

CREATE INDEX ix_market_coverage_vector_as_of ON analysis.market_coverage_vector USING btree (as_of DESC);

CREATE INDEX ix_market_scenario_path_snapshot ON analysis.market_scenario_path USING btree (snapshot_id);

CREATE INDEX ix_market_state_posterior_as_of ON analysis.market_state_posterior USING btree (as_of DESC);

CREATE INDEX ix_option_event_agent_batch_cohort_queue ON analysis.option_event_agent_batch USING btree (cohort_id, status, created_at);

CREATE INDEX ix_option_event_agent_batch_event_day ON analysis.option_event_agent_batch USING btree (event_id, created_at DESC);

CREATE INDEX ix_option_event_agent_batch_experiment ON analysis.option_event_agent_batch USING btree (experiment_id, arm, created_at DESC) WHERE (experiment_id IS NOT NULL);

CREATE INDEX ix_option_event_agent_batch_queue ON analysis.option_event_agent_batch USING btree (status, created_at, event_id);

CREATE INDEX ix_option_event_capture_event_slot ON analysis.option_event_capture USING btree (event_id, scheduled_at DESC);

CREATE INDEX ix_option_event_cohort_status_detected ON analysis.option_event USING btree (cohort_id, status, detected_at DESC);

CREATE INDEX ix_option_event_contract_event_initial ON analysis.option_event_contract USING btree (event_id, is_initial, retired_at);

CREATE INDEX ix_option_event_detector_run_cohort_time ON analysis.option_event_detector_run USING btree (cohort_id, scheduled_at DESC);

CREATE INDEX ix_option_event_signal_cohort_status ON analysis.option_event_signal USING btree (cohort_id, status, available_at DESC);

CREATE INDEX ix_option_event_signal_contract_family ON analysis.option_event_signal USING btree (contract_id, strategy_key, available_at DESC);

CREATE INDEX ix_option_event_signal_event_status ON analysis.option_event_signal USING btree (event_id, status, available_at DESC);

CREATE INDEX ix_option_event_spot_event_available ON analysis.option_event_spot USING btree (event_id, available_at DESC);

CREATE INDEX ix_option_event_status_detected ON analysis.option_event USING btree (status, detected_at DESC);

CREATE INDEX ix_option_liquidity_sla_as_of ON analysis.option_liquidity_sla USING btree (as_of DESC);

CREATE INDEX ix_option_opportunity_event_selection ON analysis.option_opportunity_observation USING btree (event_id, strategy_key, selection_stage, available_at DESC);

CREATE INDEX ix_option_opportunity_lane_episode ON analysis.option_opportunity_observation USING btree (lane, episode_key, available_at DESC);

CREATE INDEX ix_option_opportunity_measurement ON analysis.option_opportunity_observation USING btree (strategy_key, outcome_classification, measured_through DESC);

CREATE INDEX ix_option_opportunity_observation_cohort_measurement ON analysis.option_opportunity_observation USING btree (cohort_id, strategy_key, outcome_classification, measured_through DESC);

CREATE INDEX ix_option_opportunity_signal ON analysis.option_opportunity_observation USING btree (signal_id, paper_order_id) WHERE ((signal_id IS NOT NULL) OR (paper_order_id IS NOT NULL));

CREATE INDEX ix_option_outcome_lane_episode_eligible ON analysis.option_outcome USING btree (lane, episode_key, sample_eligible, observed_through DESC);

CREATE INDEX ix_option_outcome_objective_classification ON analysis.option_outcome USING btree (objective_version, outcome_classification, promotion_eligible);

CREATE INDEX ix_option_outcome_source_shadow ON analysis.option_outcome USING btree (outcome_source, shadow_trade_id);

CREATE INDEX ix_option_recovery_event_session_cohort_date ON analysis.option_recovery_event_session_quality USING btree (cohort_id, trading_date DESC);

CREATE INDEX ix_option_recovery_program_session_cohort_date ON analysis.option_recovery_program_session USING btree (cohort_id, trading_date DESC);

CREATE INDEX ix_option_relative_value_generation_contract ON analysis.option_relative_value USING btree (capture_generation_id, contract_id, id DESC);

CREATE INDEX ix_option_relative_value_run_classification_edge ON analysis.option_relative_value USING btree (analysis_run_id, classification, modeled_net_edge DESC);

CREATE INDEX ix_option_relative_value_verification_candidate ON analysis.option_relative_value_verification USING btree (relative_value_id, verified_at DESC);

CREATE INDEX ix_option_surface_shift_lookup ON analysis.option_surface_shift USING btree (instrument_id, as_of DESC);

CREATE INDEX ix_portfolio_allocation_item_snapshot ON analysis.portfolio_allocation_item USING btree (allocation_id, disposition, target_weight DESC);

CREATE INDEX ix_research_evaluator_output_trial ON analysis.research_evaluator_output USING btree (research_trial_id, trial_result_id, evidence_kind);

CREATE INDEX ix_research_trial_family_cutoff ON analysis.research_trial USING btree (experiment_family_id, input_cutoff, id);

CREATE INDEX ix_strategy_forecast_revision_cutoff ON analysis.strategy_forecast USING btree (strategy_revision_id, input_cutoff, instrument_id);

CREATE INDEX ix_strategy_monitoring_revision_kind ON analysis.strategy_monitoring_evidence USING btree (strategy_revision_id, evidence_kind, input_cutoff DESC);

CREATE INDEX ix_strategy_pnl_tape_date ON analysis.strategy_pnl_tape USING btree (strategy_revision_id, pnl_date, instrument_id);

CREATE INDEX ix_symbol_decision_outcome_state_measured ON analysis.symbol_decision_outcome USING btree (state, sample_eligible, measured_through DESC);

CREATE INDEX ix_ticker_benchmark_latest ON analysis.ticker_benchmark_snapshot USING btree (benchmark_key, as_of DESC);

CREATE INDEX ix_ticker_data_request_status ON analysis.ticker_data_request USING btree (status, created_at DESC);

CREATE INDEX ix_ticker_decision_latest ON analysis.ticker_decision USING btree (instrument_id, as_of DESC, created_at DESC);

CREATE INDEX ix_ticker_decision_market_publication ON analysis.ticker_decision USING btree (market_state_publication_id) WHERE (market_state_publication_id IS NOT NULL);

CREATE INDEX ix_ticker_decision_opportunity_episode ON analysis.ticker_decision USING btree (opportunity_episode_id, opportunity_cutoff DESC);

CREATE INDEX ix_ticker_decision_revision ON analysis.ticker_decision USING btree (input_hash, code_version, experiment_id);

CREATE INDEX ix_ticker_input_manifest_pit ON analysis.ticker_input_manifest USING btree (field, available_at, ticker_decision_id);

CREATE INDEX ix_ticker_outcome_learning ON analysis.ticker_outcome USING btree (horizon, state, horizon_sessions, measured_through);

CREATE INDEX ix_universe_observation_trial_cutoff ON analysis.universe_observation USING btree (research_trial_id, cutoff, rank, instrument_id);

CREATE UNIQUE INDEX uq_analysis_strategy_active_authority ON analysis.strategy_revision USING btree (authority_group) WHERE (status = 'active'::text);

CREATE UNIQUE INDEX uq_option_event_active_ladder_slot ON analysis.option_event_contract USING btree (event_id, ladder_slot_key) WHERE (retired_at IS NULL);

CREATE UNIQUE INDEX uq_option_event_open_symbol ON analysis.option_event USING btree (instrument_id) WHERE (status = ANY (ARRAY['active'::text, 'deferred_capacity'::text]));

CREATE UNIQUE INDEX uq_option_recovery_current_cohort_objective ON analysis.option_recovery_cohort USING btree (objective_version) WHERE (status = ANY (ARRAY['collecting'::text, 'qualified'::text]));

CREATE UNIQUE INDEX uq_strategy_forecast_content ON analysis.strategy_forecast USING btree (strategy_revision_id, instrument_id, opportunity_episode_id, horizon, input_cutoff, artifact_hash);

CREATE UNIQUE INDEX uq_strategy_manifest_revision_hash ON analysis.strategy_manifest USING btree (strategy_revision_id, manifest_hash);

CREATE UNIQUE INDEX ux_option_surface_summary_legacy_group ON analysis.option_surface_summary USING btree (snapshot_id, expiration, option_type, feature_version) WHERE (analysis_run_id IS NULL);

CREATE UNIQUE INDEX ux_option_surface_summary_v3_run_group ON analysis.option_surface_summary USING btree (analysis_run_id, expiration, option_type) WHERE (analysis_run_id IS NOT NULL);
