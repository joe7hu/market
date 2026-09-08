-- Constraints that require post-creation statements: NOT VALID and cyclic foreign keys.

ALTER TABLE ONLY analysis.option_relative_value
    ADD CONSTRAINT option_relative_value_analysis_run_id_fkey FOREIGN KEY (analysis_run_id) REFERENCES analysis.run(id) ON DELETE CASCADE NOT VALID;

ALTER TABLE ONLY analysis.option_surface_shift
    ADD CONSTRAINT option_surface_shift_current_analysis_run_id_fkey FOREIGN KEY (current_analysis_run_id) REFERENCES analysis.run(id) ON DELETE CASCADE NOT VALID;

ALTER TABLE ONLY analysis.option_surface_shift
    ADD CONSTRAINT option_surface_shift_previous_analysis_run_id_fkey FOREIGN KEY (previous_analysis_run_id) REFERENCES analysis.run(id) ON DELETE CASCADE NOT VALID;

ALTER TABLE ONLY analysis.option_surface_summary
    ADD CONSTRAINT option_surface_summary_analysis_run_id_fkey FOREIGN KEY (analysis_run_id) REFERENCES analysis.run(id) ON DELETE CASCADE NOT VALID;

ALTER TABLE ONLY raw.option_snapshot
    ADD CONSTRAINT fk_option_snapshot_latest_generation FOREIGN KEY (latest_complete_generation_id) REFERENCES raw.option_capture_generation(id) ON DELETE RESTRICT;

ALTER TABLE ONLY raw.option_capture_generation
    ADD CONSTRAINT option_capture_generation_snapshot_id_fkey FOREIGN KEY (snapshot_id) REFERENCES raw.option_snapshot(id) ON DELETE RESTRICT;

-- Keep protected research objects owned by non-application roles.
