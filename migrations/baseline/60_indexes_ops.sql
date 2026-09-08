-- Indexes: ops schema.

CREATE INDEX ix_ops_job_run_due ON ops.job_run USING btree (job_name, scheduled_due_at DESC);

CREATE INDEX ix_provider_lease_active ON ops.provider_lease USING btree (provider, expires_at DESC, workload, symbol);

CREATE INDEX ix_storage_archive_manifest_status ON ops.storage_archive_manifest USING btree (verification_status, archive_kind, created_at DESC);

CREATE UNIQUE INDEX uq_ops_job_run_running ON ops.job_run USING btree (job_name) WHERE (status = 'running'::text);
