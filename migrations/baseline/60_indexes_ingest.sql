-- Indexes: ingest schema.

CREATE INDEX ix_ingest_run_latest ON ingest.run USING btree (source_id, finished_at DESC);

CREATE INDEX ix_ingest_source_operational_health ON ingest.source USING btree (operational_state, enabled, health_owner);

CREATE INDEX ix_source_lifecycle_history_pit ON ingest.source_lifecycle_history USING btree (source_id, effective_at DESC, id DESC);
