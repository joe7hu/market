-- Triggers: analysis schema.

CREATE TRIGGER agent_task_payload_availability BEFORE INSERT OR UPDATE ON analysis.agent_task FOR EACH ROW EXECUTE FUNCTION analysis.stamp_agent_task_payload_availability();

CREATE TRIGGER assign_research_gate_actual_clock BEFORE INSERT ON analysis.validation_gate_result FOR EACH ROW EXECUTE FUNCTION analysis.assign_research_gate_actual_clock();

CREATE TRIGGER book_attribution_immutable BEFORE DELETE OR UPDATE ON analysis.book_attribution FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

CREATE TRIGGER enforce_experiment_manifest_immutable BEFORE DELETE OR UPDATE ON analysis.experiment_manifest FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_manifest_immutable();

CREATE TRIGGER enforce_phase3_strategy_status BEFORE INSERT OR UPDATE OF status, promotability, actionability, strategy_family, strategy_key, mechanism_class, p3_enabled ON analysis.strategy_revision FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_strategy_status();

CREATE TRIGGER enforce_research_authority_availability BEFORE INSERT ON analysis.experiment_family FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_authority_availability();

CREATE TRIGGER enforce_research_authority_availability BEFORE INSERT ON analysis.experiment_manifest FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_authority_availability();

CREATE TRIGGER enforce_research_authority_availability BEFORE INSERT ON analysis.hypothesis FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_authority_availability();

CREATE TRIGGER enforce_research_authority_availability BEFORE INSERT ON analysis.trial_universe_manifest FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_authority_availability();

CREATE TRIGGER enforce_research_evaluator_output BEFORE INSERT OR DELETE OR UPDATE ON analysis.research_evaluator_output FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_evaluator_output();

CREATE TRIGGER enforce_research_evidence_manifest BEFORE INSERT OR DELETE OR UPDATE ON analysis.research_evidence_manifest FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_evidence_manifest();

CREATE TRIGGER enforce_research_gate_actual_availability BEFORE INSERT ON analysis.validation_gate_result FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_gate_actual_availability();

CREATE TRIGGER enforce_research_gate_pit BEFORE INSERT ON analysis.validation_gate_result FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_gate_pit();

CREATE TRIGGER enforce_research_gate_promotion_clock BEFORE UPDATE ON analysis.strategy_revision FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_gate_promotion_clock();

CREATE TRIGGER enforce_research_result_actual_availability BEFORE INSERT ON analysis.trial_result FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_result_actual_availability();

CREATE TRIGGER enforce_research_result_pit BEFORE INSERT ON analysis.trial_result FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_result_pit();

CREATE TRIGGER enforce_research_revision_promotion BEFORE INSERT OR UPDATE OF status, research_required, hypothesis_id, experiment_family_id, artifact_id, artifact_hash ON analysis.strategy_revision FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_revision_promotion();

CREATE CONSTRAINT TRIGGER enforce_research_revision_promotion_hardened AFTER INSERT OR UPDATE ON analysis.strategy_revision DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_revision_promotion_hardened();

CREATE TRIGGER enforce_research_trial_terminal_immutability BEFORE INSERT OR DELETE OR UPDATE ON analysis.research_trial FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_trial_terminal_immutability();

CREATE TRIGGER enforce_research_universe_actual_availability BEFORE INSERT ON analysis.universe_observation FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_universe_actual_availability();

CREATE TRIGGER enforce_research_universe_pit BEFORE INSERT ON analysis.universe_observation FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_universe_pit();

CREATE TRIGGER enforce_strategy_comparison_immutable BEFORE DELETE OR UPDATE ON analysis.strategy_comparison FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_immutable();

CREATE TRIGGER enforce_strategy_comparison_lineage BEFORE INSERT ON analysis.strategy_comparison FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_comparison();

CREATE TRIGGER enforce_strategy_evaluation_availability BEFORE INSERT OR DELETE OR UPDATE ON analysis.strategy_evaluation FOR EACH ROW EXECUTE FUNCTION analysis.enforce_strategy_evaluation_availability();

CREATE TRIGGER enforce_strategy_forecast_authority BEFORE INSERT OR UPDATE ON analysis.strategy_forecast FOR EACH ROW EXECUTE FUNCTION analysis.enforce_strategy_forecast_authority();

CREATE TRIGGER enforce_strategy_forecast_immutable BEFORE DELETE OR UPDATE ON analysis.strategy_forecast FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_rows_immutable();

CREATE TRIGGER enforce_strategy_forecast_phase3_link BEFORE INSERT OR UPDATE ON analysis.strategy_forecast FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_forecast_link();

CREATE TRIGGER enforce_strategy_manifest_hash BEFORE INSERT ON analysis.strategy_manifest FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_manifest_hash();

CREATE TRIGGER enforce_strategy_manifest_immutable BEFORE DELETE OR UPDATE ON analysis.strategy_manifest FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_immutable();

CREATE TRIGGER enforce_strategy_monitoring_immutable BEFORE DELETE OR UPDATE ON analysis.strategy_monitoring_evidence FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_immutable();

CREATE TRIGGER enforce_strategy_monitoring_lineage BEFORE INSERT ON analysis.strategy_monitoring_evidence FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_monitoring();

CREATE TRIGGER enforce_strategy_pnl_tape_immutable BEFORE DELETE OR UPDATE ON analysis.strategy_pnl_tape FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_immutable();

CREATE TRIGGER enforce_strategy_pnl_tape_lineage BEFORE INSERT ON analysis.strategy_pnl_tape FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase3_pnl_tape();

CREATE TRIGGER enforce_strategy_revision_parameters_immutable BEFORE UPDATE OF parameters ON analysis.strategy_revision FOR EACH ROW EXECUTE FUNCTION analysis.enforce_strategy_revision_parameters_immutable();

CREATE TRIGGER enforce_trial_result_immutable BEFORE DELETE OR UPDATE ON analysis.trial_result FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_rows_immutable();

CREATE TRIGGER enforce_trial_universe_manifest_immutable BEFORE DELETE OR UPDATE ON analysis.trial_universe_manifest FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_manifest_immutable();

CREATE TRIGGER enforce_universe_observation_immutable BEFORE DELETE OR UPDATE ON analysis.universe_observation FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_rows_immutable();

CREATE TRIGGER enforce_validation_dossier_seal BEFORE INSERT OR DELETE OR UPDATE ON analysis.validation_dossier FOR EACH ROW EXECUTE FUNCTION analysis.enforce_validation_dossier_seal();

CREATE TRIGGER enforce_validation_gate_result_immutable BEFORE DELETE OR UPDATE ON analysis.validation_gate_result FOR EACH ROW EXECUTE FUNCTION analysis.enforce_research_rows_immutable();

CREATE TRIGGER execution_model_snapshot_immutable BEFORE DELETE OR UPDATE ON analysis.execution_model_snapshot FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

CREATE TRIGGER market_coverage_vector_immutable BEFORE DELETE OR UPDATE ON analysis.market_coverage_vector FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase2_update();

CREATE TRIGGER market_scenario_path_immutable BEFORE DELETE OR UPDATE ON analysis.market_scenario_path FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase2_update();

CREATE TRIGGER market_state_posterior_immutable BEFORE DELETE OR UPDATE ON analysis.market_state_posterior FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase2_update();

CREATE TRIGGER option_liquidity_sla_immutable BEFORE DELETE OR UPDATE ON analysis.option_liquidity_sla FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase2_update();

CREATE TRIGGER phase4_attribution_multiplier_guard BEFORE INSERT ON analysis.book_attribution FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_attribution_multiplier_guard();

CREATE CONSTRAINT TRIGGER phase4_authority_lineage AFTER INSERT ON analysis.portfolio_allocation_item DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_authority_lineage();

CREATE CONSTRAINT TRIGGER phase4_authority_snapshot_complete AFTER INSERT ON analysis.portfolio_allocation_snapshot DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_authority_snapshot_complete();

CREATE TRIGGER phase4_execution_snapshot_guard BEFORE INSERT ON analysis.execution_model_snapshot FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_execution_snapshot_guard();

CREATE CONSTRAINT TRIGGER phase4_funding_conservation AFTER INSERT ON analysis.portfolio_allocation_item DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_funding_conservation();

CREATE TRIGGER phase4_review_item_guard BEFORE INSERT ON analysis.portfolio_allocation_item FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_review_item_guard();

CREATE TRIGGER phase4_review_snapshot_guard BEFORE INSERT ON analysis.portfolio_allocation_snapshot FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_review_snapshot_guard();

CREATE CONSTRAINT TRIGGER phase4_source_lineage AFTER INSERT ON analysis.portfolio_allocation_item DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_source_lineage();

CREATE TRIGGER portfolio_allocation_item_immutable BEFORE DELETE OR UPDATE ON analysis.portfolio_allocation_item FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

CREATE TRIGGER portfolio_allocation_item_lineage BEFORE INSERT ON analysis.portfolio_allocation_item FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_allocation_item_funding_lineage();

CREATE TRIGGER portfolio_allocation_snapshot_immutable BEFORE DELETE OR UPDATE ON analysis.portfolio_allocation_snapshot FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

CREATE TRIGGER portfolio_allocation_snapshot_lineage BEFORE INSERT ON analysis.portfolio_allocation_snapshot FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();

CREATE TRIGGER portfolio_drift_evidence_immutable BEFORE DELETE OR UPDATE ON analysis.portfolio_drift_evidence FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

CREATE TRIGGER portfolio_drift_lineage BEFORE INSERT ON analysis.portfolio_drift_evidence FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();

CREATE TRIGGER portfolio_scenario_artifact_immutable BEFORE DELETE OR UPDATE ON analysis.probabilistic_portfolio_scenario_artifact FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase4_update();

CREATE TRIGGER portfolio_scenario_lineage BEFORE INSERT ON analysis.probabilistic_portfolio_scenario_artifact FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_lineage();

CREATE TRIGGER tr_option_event_assign_current_cohort BEFORE INSERT ON analysis.option_event FOR EACH ROW EXECUTE FUNCTION analysis.assign_current_option_recovery_cohort();

CREATE TRIGGER zzz_phase4_funding_content BEFORE INSERT ON analysis.portfolio_allocation_item FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_funding_content();
