-- Triggers: raw schema.

CREATE TRIGGER market_observation_immutable BEFORE DELETE OR UPDATE ON raw.market_observation FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase2_update();

CREATE TRIGGER price_bar_confirmation_projection AFTER INSERT OR UPDATE OF fact_id, fact_available_at, ingest_run_id ON raw.price_bar_confirmation FOR EACH ROW EXECUTE FUNCTION raw.project_confirmation_staging();

CREATE TRIGGER quote_confirmation_projection AFTER INSERT OR UPDATE OF fact_id, fact_available_at, ingest_run_id ON raw.quote_confirmation FOR EACH ROW EXECUTE FUNCTION raw.project_confirmation_staging();
