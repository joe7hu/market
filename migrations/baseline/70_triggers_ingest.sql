-- Triggers: ingest schema.

CREATE TRIGGER payload_identity_immutable BEFORE UPDATE ON ingest.payload FOR EACH ROW EXECUTE FUNCTION ingest.reject_identity_update();

CREATE TRIGGER run_identity_immutable BEFORE UPDATE ON ingest.run FOR EACH ROW EXECUTE FUNCTION ingest.reject_identity_update();

CREATE TRIGGER source_lifecycle_change AFTER UPDATE OF enabled, operational_state ON ingest.source FOR EACH ROW WHEN (((old.enabled IS DISTINCT FROM new.enabled) OR (old.operational_state IS DISTINCT FROM new.operational_state))) EXECUTE FUNCTION ingest.record_source_lifecycle();

CREATE TRIGGER source_lifecycle_insert AFTER INSERT ON ingest.source FOR EACH ROW EXECUTE FUNCTION ingest.record_source_lifecycle();
