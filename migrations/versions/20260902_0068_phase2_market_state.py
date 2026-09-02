"""Add immutable Phase 2 observations, coverage, posterior, and scenarios."""

from __future__ import annotations

from alembic import op


revision = "20260902_0068"
down_revision = "20260902_0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE raw.market_observation (
            observation_id TEXT PRIMARY KEY,
            field_name TEXT NOT NULL,
            dimension TEXT NOT NULL,
            asset_class TEXT NOT NULL,
            source_id TEXT NOT NULL REFERENCES ingest.source(id),
            source_version TEXT NOT NULL,
            value JSONB,
            unit TEXT,
            ingest_run_id UUID NOT NULL REFERENCES ingest.run(id),
            payload_id BIGINT REFERENCES ingest.payload(id),
            content_hash TEXT NOT NULL,
            parent_snapshot_id TEXT,
            observed_at TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL,
            publication_at TIMESTAMPTZ,
            release_at TIMESTAMPTZ,
            vintage_at TIMESTAMPTZ,
            actual DOUBLE PRECISION,
            consensus DOUBLE PRECISION,
            surprise DOUBLE PRECISION,
            revision DOUBLE PRECISION,
            status TEXT NOT NULL,
            confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_market_observation_status CHECK (status IN ('AVAILABLE','MISSING_SOURCE','MISSING_HISTORY','CONFLICTED','FALLBACK','UNSUPPORTED','STALE')),
            CONSTRAINT ck_market_observation_clocks CHECK (available_at IS NOT NULL)
        );
        CREATE INDEX ix_market_observation_pit ON raw.market_observation (field_name, observed_at, available_at);
        CREATE INDEX ix_market_observation_source ON raw.market_observation (source_id, available_at DESC);

        CREATE TABLE analysis.market_coverage_vector (
            vector_id TEXT PRIMARY KEY,
            as_of TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL,
            payload JSONB NOT NULL,
            ingest_run_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            input_content_hash TEXT NOT NULL,
            parent_snapshot_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_market_coverage_vector_as_of ON analysis.market_coverage_vector (as_of DESC);

        CREATE TABLE analysis.market_state_posterior (
            posterior_id TEXT PRIMARY KEY,
            as_of TIMESTAMPTZ NOT NULL,
            input_cutoff TIMESTAMPTZ NOT NULL,
            model_version TEXT NOT NULL,
            status TEXT NOT NULL,
            payload JSONB NOT NULL,
            ingest_run_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            input_content_hash TEXT NOT NULL,
            parent_snapshot_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_market_state_posterior_as_of ON analysis.market_state_posterior (as_of DESC);

        CREATE TABLE analysis.market_scenario_path (
            scenario_hash TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            parent_snapshot_id TEXT NOT NULL,
            posterior_id TEXT NOT NULL,
            model_version TEXT NOT NULL,
            ingest_run_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            input_content_hash TEXT NOT NULL,
            path JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_market_scenario_path_snapshot ON analysis.market_scenario_path (snapshot_id);

        CREATE TABLE analysis.option_liquidity_sla (
            sla_id TEXT PRIMARY KEY,
            as_of TIMESTAMPTZ NOT NULL,
            source_id TEXT NOT NULL REFERENCES ingest.source(id),
            ingest_run_id UUID NOT NULL REFERENCES ingest.run(id),
            payload_hash TEXT NOT NULL,
            parent_snapshot_id TEXT,
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_option_liquidity_sla_as_of ON analysis.option_liquidity_sla (as_of DESC);

        CREATE FUNCTION analysis.reject_phase2_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Phase 2 artifacts are immutable'; END; $$;
        CREATE TRIGGER market_observation_immutable BEFORE UPDATE OR DELETE ON raw.market_observation FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase2_update();
        CREATE TRIGGER market_coverage_vector_immutable BEFORE UPDATE OR DELETE ON analysis.market_coverage_vector FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase2_update();
        CREATE TRIGGER market_state_posterior_immutable BEFORE UPDATE OR DELETE ON analysis.market_state_posterior FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase2_update();
        CREATE TRIGGER market_scenario_path_immutable BEFORE UPDATE OR DELETE ON analysis.market_scenario_path FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase2_update();
        CREATE TRIGGER option_liquidity_sla_immutable BEFORE UPDATE OR DELETE ON analysis.option_liquidity_sla FOR EACH ROW EXECUTE FUNCTION analysis.reject_phase2_update();
        """
    )
    op.execute(
        """
        GRANT SELECT, INSERT ON raw.market_observation TO market_app;
        GRANT SELECT ON analysis.market_coverage_vector, analysis.market_state_posterior,
                             analysis.market_scenario_path, analysis.option_liquidity_sla TO market_app;
        GRANT INSERT ON analysis.market_coverage_vector, analysis.market_state_posterior,
                             analysis.market_scenario_path, analysis.option_liquidity_sla TO market_app;
        GRANT INSERT, UPDATE ON ingest.source, ingest.run, ingest.payload TO market_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE SELECT, INSERT ON raw.market_observation FROM market_app;
        REVOKE SELECT ON analysis.market_coverage_vector, analysis.market_state_posterior,
                             analysis.market_scenario_path, analysis.option_liquidity_sla FROM market_app;
        REVOKE INSERT ON analysis.market_coverage_vector, analysis.market_state_posterior,
                             analysis.market_scenario_path, analysis.option_liquidity_sla FROM market_app;
        REVOKE INSERT, UPDATE ON ingest.source, ingest.run, ingest.payload FROM market_app;
        DROP TRIGGER IF EXISTS option_liquidity_sla_immutable ON analysis.option_liquidity_sla;
        DROP TRIGGER IF EXISTS market_scenario_path_immutable ON analysis.market_scenario_path;
        DROP TRIGGER IF EXISTS market_state_posterior_immutable ON analysis.market_state_posterior;
        DROP TRIGGER IF EXISTS market_coverage_vector_immutable ON analysis.market_coverage_vector;
        DROP TRIGGER IF EXISTS market_observation_immutable ON raw.market_observation;
        DROP FUNCTION IF EXISTS analysis.reject_phase2_update();
        DROP TABLE IF EXISTS analysis.option_liquidity_sla;
        DROP TABLE IF EXISTS analysis.market_scenario_path;
        DROP TABLE IF EXISTS analysis.market_state_posterior;
        DROP TABLE IF EXISTS analysis.market_coverage_vector;
        DROP TABLE IF EXISTS raw.market_observation;
        """
    )
