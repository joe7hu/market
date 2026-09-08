-- Privileges: relations.

GRANT SELECT,INSERT,UPDATE ON TABLE raw.price_bar TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.quote TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.agent_experiment TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.agent_run TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.agent_task TO market_app;

GRANT SELECT ON TABLE analysis.book_attribution TO market_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE analysis.decision TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.decision_evidence TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.event_decision_packet TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.event_scout_event TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.event_study_feature TO market_app;

GRANT SELECT ON TABLE analysis.execution_model_snapshot TO market_app;

GRANT SELECT ON TABLE analysis.experiment_family TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.experiment_family TO market_app;

GRANT SELECT ON TABLE analysis.experiment_manifest TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.experiment_manifest TO market_app;

GRANT SELECT ON TABLE analysis.hypothesis TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.hypothesis TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.market_coverage_vector TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.market_scenario_path TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.market_state_posterior TO market_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE analysis.option_decision TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_discovery_candidate TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_discovery_run TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_event TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_event_agent_batch TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_event_capture TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_event_contract TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_event_detector_run TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_event_signal TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_event_spot TO market_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE analysis.option_feature TO market_app;

GRANT SELECT,USAGE ON SEQUENCE analysis.option_feature_id_seq TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_gate_result TO market_app;

GRANT SELECT ON TABLE analysis.option_history_anomaly TO market_app;

GRANT SELECT ON TABLE analysis.option_history_canary TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.option_liquidity_sla TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_opportunity_observation TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_outcome TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_recovery_cohort TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_recovery_event_session_quality TO market_app;

GRANT SELECT ON TABLE analysis.option_recovery_program_session TO market_app;

GRANT SELECT ON TABLE analysis.option_relative_value TO market_app;

GRANT SELECT ON TABLE analysis.option_relative_value_verification TO market_app;

GRANT SELECT ON TABLE analysis.option_surface_shift TO market_app;

GRANT SELECT ON TABLE analysis.option_surface_summary TO market_app;

GRANT SELECT ON TABLE analysis.portfolio_allocation_item TO market_app;

GRANT SELECT ON TABLE analysis.portfolio_allocation_snapshot TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.portfolio_drift_evidence TO market_app;

GRANT SELECT ON TABLE analysis.probabilistic_portfolio_scenario_artifact TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.reject_summary TO market_app;

GRANT SELECT,USAGE ON SEQUENCE analysis.reject_summary_id_seq TO market_app;

GRANT SELECT ON TABLE analysis.research_evidence_manifest TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.research_evidence_manifest TO market_app;

GRANT SELECT ON TABLE analysis.research_trial TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.research_trial TO market_app;

GRANT SELECT ON TABLE analysis.run TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.run TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.shadow_trade TO market_app;

GRANT UPDATE(entry_at) ON TABLE analysis.shadow_trade TO market_app;

GRANT UPDATE(entry_price) ON TABLE analysis.shadow_trade TO market_app;

GRANT UPDATE(exit_at) ON TABLE analysis.shadow_trade TO market_app;

GRANT UPDATE(exit_price) ON TABLE analysis.shadow_trade TO market_app;

GRANT UPDATE(status) ON TABLE analysis.shadow_trade TO market_app;

GRANT UPDATE(metrics) ON TABLE analysis.shadow_trade TO market_app;

GRANT UPDATE(pending_entry_reason) ON TABLE analysis.shadow_trade TO market_app;

GRANT UPDATE(fill_basis) ON TABLE analysis.shadow_trade TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.source_signal TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.strategy_comparison TO market_app;

GRANT SELECT ON TABLE analysis.strategy_evaluation TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.strategy_evaluation TO market_app;

GRANT SELECT ON TABLE analysis.strategy_forecast TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.strategy_forecast TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.strategy_manifest TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.strategy_monitoring_evidence TO market_app;

GRANT SELECT,INSERT ON TABLE analysis.strategy_pnl_tape TO market_app;

GRANT SELECT ON TABLE analysis.strategy_revision TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.strategy_revision TO market_app;

GRANT SELECT ON TABLE analysis.strategy_registry TO market_app;

GRANT SELECT ON TABLE analysis.trial_result TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.trial_result TO market_app;

GRANT SELECT ON TABLE analysis.trial_universe_manifest TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.trial_universe_manifest TO market_app;

GRANT SELECT ON TABLE analysis.universe_observation TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.universe_observation TO market_app;

GRANT SELECT ON TABLE analysis.strategy_trial_accounting TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.symbol_decision_outcome TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.symbol_feature TO market_app;

GRANT SELECT,USAGE ON SEQUENCE analysis.symbol_feature_id_seq TO market_app;

GRANT SELECT ON TABLE analysis.ticker_benchmark_snapshot TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.ticker_benchmark_snapshot TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.ticker_data_request TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.ticker_decision TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.ticker_input_manifest TO market_app;

GRANT SELECT,USAGE ON SEQUENCE analysis.ticker_input_manifest_id_seq TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.ticker_outcome TO market_app;

GRANT SELECT ON TABLE analysis.validation_dossier TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.validation_dossier TO market_app;

GRANT SELECT ON TABLE analysis.validation_gate_result TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE analysis.validation_gate_result TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.alert TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.catalyst TO market_app;

GRANT SELECT,INSERT,DELETE ON TABLE app.current_publication_item TO market_app;

GRANT SELECT ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(dedupe_key) ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(event_type) ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(opportunity_id) ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(ticket_version) ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(paper_order_id) ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(lane) ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(severity) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(status) ON TABLE app.decision_inbox_item TO market_app;

GRANT INSERT(payload) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(resolved_at) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(user_state) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(snoozed_until) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(dismiss_reason) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(user_state_updated_at) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(reviewed_at) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(useful) ON TABLE app.decision_inbox_item TO market_app;

GRANT UPDATE(usefulness_updated_at) ON TABLE app.decision_inbox_item TO market_app;

GRANT SELECT,INSERT ON TABLE app.decision_inbox_sync_state TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.decision_truth TO market_app;

GRANT SELECT,INSERT ON TABLE app.manual_account_snapshot TO market_app;

GRANT SELECT,USAGE ON SEQUENCE app.manual_account_snapshot_id_seq TO market_app;

GRANT SELECT ON TABLE app.notification_outbox TO market_app;

GRANT INSERT(dedupe_key) ON TABLE app.notification_outbox TO market_app;

GRANT INSERT(inbox_item_id) ON TABLE app.notification_outbox TO market_app;

GRANT INSERT(event_type) ON TABLE app.notification_outbox TO market_app;

GRANT INSERT(payload) ON TABLE app.notification_outbox TO market_app;

GRANT UPDATE(status) ON TABLE app.notification_outbox TO market_app;

GRANT UPDATE(attempts) ON TABLE app.notification_outbox TO market_app;

GRANT UPDATE(next_attempt_at) ON TABLE app.notification_outbox TO market_app;

GRANT UPDATE(last_error) ON TABLE app.notification_outbox TO market_app;

GRANT UPDATE(sent_at) ON TABLE app.notification_outbox TO market_app;

GRANT UPDATE(updated_at) ON TABLE app.notification_outbox TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.option_history_policy TO market_app;

GRANT SELECT ON TABLE app.paper_execution_observation TO market_app;

GRANT SELECT ON TABLE app.paper_order TO market_app;

GRANT INSERT(decision_id) ON TABLE app.paper_order TO market_app;

GRANT INSERT(instrument_id) ON TABLE app.paper_order TO market_app;

GRANT INSERT(side) ON TABLE app.paper_order TO market_app;

GRANT INSERT(quantity) ON TABLE app.paper_order TO market_app;

GRANT INSERT(limit_price) ON TABLE app.paper_order TO market_app;

GRANT INSERT(status),UPDATE(status) ON TABLE app.paper_order TO market_app;

GRANT INSERT(policy_result) ON TABLE app.paper_order TO market_app;

GRANT INSERT(structure) ON TABLE app.paper_order TO market_app;

GRANT INSERT(reserved_collateral) ON TABLE app.paper_order TO market_app;

GRANT INSERT(idempotency_key) ON TABLE app.paper_order TO market_app;

GRANT INSERT(ticket_version) ON TABLE app.paper_order TO market_app;

GRANT INSERT(ticket_snapshot) ON TABLE app.paper_order TO market_app;

GRANT INSERT(intended_limit_price) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(actual_fill_price) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(filled_at) ON TABLE app.paper_order TO market_app;

GRANT INSERT(lane) ON TABLE app.paper_order TO market_app;

GRANT INSERT(policy_snapshot) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(exit_at) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(exit_price) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(fees) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(updated_at) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(submitted_at) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(filled_quantity) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(exited_quantity) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(entry_slippage) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(exit_slippage) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(unfilled_reason) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(execution_quote) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(fill_evidence_at) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(contract_multiplier) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(entry_fees) ON TABLE app.paper_order TO market_app;

GRANT UPDATE(exit_fees) ON TABLE app.paper_order TO market_app;

GRANT SELECT ON TABLE app.paper_order_leg TO market_app;

GRANT INSERT(paper_order_id) ON TABLE app.paper_order_leg TO market_app;

GRANT INSERT(leg_index) ON TABLE app.paper_order_leg TO market_app;

GRANT INSERT(contract_id) ON TABLE app.paper_order_leg TO market_app;

GRANT INSERT(option_type) ON TABLE app.paper_order_leg TO market_app;

GRANT INSERT(side) ON TABLE app.paper_order_leg TO market_app;

GRANT INSERT(strike) ON TABLE app.paper_order_leg TO market_app;

GRANT INSERT(bid) ON TABLE app.paper_order_leg TO market_app;

GRANT INSERT(ask) ON TABLE app.paper_order_leg TO market_app;

GRANT INSERT(bid_size) ON TABLE app.paper_order_leg TO market_app;

GRANT INSERT(ask_size) ON TABLE app.paper_order_leg TO market_app;

GRANT INSERT(quote_time) ON TABLE app.paper_order_leg TO market_app;

GRANT INSERT(open_interest) ON TABLE app.paper_order_leg TO market_app;

GRANT INSERT(volume) ON TABLE app.paper_order_leg TO market_app;

GRANT SELECT ON TABLE app.portfolio_position TO market_app;

GRANT SELECT ON TABLE app.portfolio_transaction TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.publication TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.publication_bundle TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.publication_bundle_item TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.publication_item TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.publication_payload TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.publication_content_item TO market_app;

GRANT SELECT ON TABLE app.setting TO market_app;

GRANT SELECT ON TABLE app.thesis TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE app.thesis_automation_run TO market_app;

GRANT SELECT ON TABLE app.thesis_evidence_assessment TO market_app;

GRANT SELECT ON TABLE app.thesis_expression TO market_app;

GRANT SELECT,INSERT ON TABLE app.thesis_review_event TO market_app;

GRANT SELECT,USAGE ON SEQUENCE app.thesis_review_event_id_seq TO market_app;

GRANT SELECT,INSERT ON TABLE app.trade_journal TO market_app;

GRANT SELECT ON TABLE app.watchlist_item TO market_app;

GRANT SELECT ON TABLE catalog.instrument TO market_research_signer;

GRANT SELECT,INSERT,UPDATE ON TABLE catalog.instrument TO market_app;

GRANT SELECT ON TABLE catalog.instrument_alias TO market_app;

GRANT SELECT,USAGE ON SEQUENCE catalog.instrument_alias_id_seq TO market_app;

GRANT SELECT,USAGE ON SEQUENCE catalog.instrument_id_seq TO market_app;

GRANT SELECT ON TABLE catalog.option_contract TO market_app;

GRANT SELECT,USAGE ON SEQUENCE catalog.option_contract_id_seq TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE ingest.payload TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE ingest.run TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE ingest.source TO market_app;

GRANT INSERT ON TABLE ingest.source_lifecycle_history TO market_migrator;

GRANT SELECT ON TABLE ingest.source_lifecycle_history TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE ops.job_run TO market_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE ops.provider_lease TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE ops.storage_archive_checkpoint TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE ops.storage_archive_manifest TO market_app;

GRANT SELECT,USAGE ON SEQUENCE ops.storage_archive_manifest_id_seq TO market_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE ops.storage_archive_manifest_reference TO market_app;

GRANT SELECT ON TABLE raw.broker_account_snapshot TO market_app;

GRANT SELECT ON TABLE raw.broker_position_snapshot TO market_app;

GRANT SELECT ON TABLE raw.confirmed_price_bar TO market_app;

GRANT SELECT ON TABLE raw.confirmed_quote TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.content_item TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.content_item_instrument TO market_app;

GRANT SELECT ON TABLE raw.disclosure TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.fundamental_observation TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.market_event TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.market_event_version TO market_app;

GRANT SELECT,INSERT ON TABLE raw.market_observation TO market_app;

GRANT SELECT ON TABLE raw.option_capture_generation TO market_app;

GRANT SELECT ON TABLE raw.option_quote TO market_app;

GRANT SELECT ON TABLE raw.option_snapshot TO market_app;

GRANT SELECT,INSERT,DELETE ON TABLE raw.price_bar_confirmation TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.price_bar_fact_availability TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.price_bar_history TO market_app;

GRANT SELECT,INSERT,DELETE ON TABLE raw.quote_confirmation TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.quote_fact_availability TO market_app;

GRANT SELECT,INSERT,UPDATE ON TABLE raw.quote_history TO market_app;
