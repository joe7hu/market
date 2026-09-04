"""Close Phase 4 telemetry writers behind the repository signature."""

from __future__ import annotations

from alembic import op


revision = "20260904_0076"
down_revision = "20260904_0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE OR REPLACE FUNCTION analysis.phase4_telemetry_authorization_payload(p_contract TEXT, p_payload JSONB)
      RETURNS TEXT LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, analysis AS $$
        SELECT analysis.phase4_canonical_json(jsonb_build_object('contract', p_contract, 'payload', p_payload))
      $$;
      CREATE OR REPLACE FUNCTION analysis.phase4_telemetry_authorized(p_contract TEXT, p_payload JSONB, p_signature TEXT)
      RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER STABLE SET search_path = pg_catalog, analysis, public AS $$
      DECLARE key TEXT;
      BEGIN
        key := analysis.phase4_allocation_signing_key();
        RETURN key IS NOT NULL AND length(key) >= 16 AND p_signature = encode(public.hmac(
          convert_to(analysis.phase4_telemetry_authorization_payload(p_contract, p_payload), 'UTF8'),
          convert_to(key, 'UTF8'), 'sha256'), 'hex');
      END $$;
      CREATE OR REPLACE FUNCTION analysis.write_phase4_execution(p JSONB, sig TEXT)
      RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, analysis, app AS $$
      BEGIN
        IF NOT analysis.phase4_telemetry_authorized('phase4-execution.v1', p, sig) THEN RAISE EXCEPTION 'Phase 4 execution authorization signature is invalid'; END IF;
        INSERT INTO analysis.execution_model_snapshot
          (execution_model_snapshot_id, allocation_id, model_version, calibration_status, sample_count,
           fill_probability, spread_bps, latency_ms, impact_bps, input_cutoff, input_hash, content_hash, metadata)
        VALUES (p->>'execution_model_snapshot_id', p->>'allocation_id', p->>'model_version', p->>'calibration_status',
          (p->>'sample_count')::integer, (p->>'fill_probability')::double precision, (p->>'spread_bps')::double precision,
          (p->>'latency_ms')::double precision, (p->>'impact_bps')::double precision, (p->>'input_cutoff')::timestamptz,
          p->>'input_hash', p->>'content_hash', jsonb_extract_path(p, 'metadata')) ON CONFLICT (execution_model_snapshot_id) DO NOTHING;
      END $$;
      CREATE OR REPLACE FUNCTION analysis.write_phase4_paper_execution(p JSONB, sig TEXT)
      RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, analysis, app AS $$
      BEGIN
        IF NOT analysis.phase4_telemetry_authorized('phase4-paper-execution.v1', p, sig) THEN RAISE EXCEPTION 'Phase 4 paper execution authorization signature is invalid'; END IF;
        INSERT INTO app.paper_execution_observation
          (paper_execution_observation_id, allocation_item_id, action_id, paper_order_id, execution_mode, paper_only, status,
           requested_quantity, filled_quantity, requested_price, fill_price, spread_bps, latency_ms, impact_bps, side,
           exit_price, event_fee, contract_multiplier, observed_at, available_at, metadata)
        VALUES (p->>'paper_execution_observation_id', p->>'allocation_item_id', p->>'action_id', (p->>'paper_order_id')::uuid,
          p->>'execution_mode', (p->>'paper_only')::boolean, p->>'status', (p->>'requested_quantity')::double precision,
          (p->>'filled_quantity')::double precision, (p->>'requested_price')::double precision, (p->>'fill_price')::double precision,
          (p->>'spread_bps')::double precision, (p->>'latency_ms')::double precision, (p->>'impact_bps')::double precision,
          p->>'side', (p->>'exit_price')::double precision, (p->>'event_fee')::double precision,
          (p->>'contract_multiplier')::double precision, (p->>'observed_at')::timestamptz, (p->>'available_at')::timestamptz, jsonb_extract_path(p, 'metadata'))
        ON CONFLICT (paper_execution_observation_id) DO NOTHING;
      END $$;
      CREATE OR REPLACE FUNCTION analysis.write_phase4_book_attribution(p JSONB, sig TEXT)
      RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, analysis, app AS $$
      BEGIN
        IF NOT analysis.phase4_telemetry_authorized('phase4-book-attribution.v1', p, sig) THEN RAISE EXCEPTION 'Phase 4 attribution authorization signature is invalid'; END IF;
        INSERT INTO analysis.book_attribution
          (book_attribution_id, allocation_id, allocation_item_id, strategy_forecast_id, hypothesis_id, action_id, rank_id,
           expression, experiment_id, trial_id, result_id, paper_execution_observation_id, pnl_status, realized_pnl,
           attribution, input_cutoff, input_hash, content_hash)
        VALUES (p->>'book_attribution_id', p->>'allocation_id', p->>'allocation_item_id', p->>'strategy_forecast_id',
          (p->>'hypothesis_id')::uuid, p->>'action_id', p->>'rank_id', jsonb_extract_path(p, 'expression'), p->>'experiment_id',
          (p->>'trial_id')::uuid, (p->>'result_id')::uuid, p->>'paper_execution_observation_id', p->>'pnl_status',
          (p->>'realized_pnl')::double precision, jsonb_extract_path(p, 'attribution'), (p->>'input_cutoff')::timestamptz, p->>'input_hash', p->>'content_hash')
        ON CONFLICT (book_attribution_id) DO NOTHING;
      END $$;
      REVOKE ALL ON FUNCTION analysis.insert_phase4_execution(TEXT,TEXT,TEXT,TEXT,INTEGER,DOUBLE PRECISION,DOUBLE PRECISION,DOUBLE PRECISION,DOUBLE PRECISION,TIMESTAMPTZ,TEXT,TEXT,JSONB),
        analysis.insert_phase4_paper_execution_observation(JSONB), analysis.insert_phase4_book_attribution(JSONB) FROM PUBLIC, market_app, market_migrator;
      REVOKE ALL ON FUNCTION analysis.phase4_telemetry_authorized(TEXT,JSONB,TEXT) FROM PUBLIC, market_app, market_migrator;
      REVOKE ALL ON FUNCTION analysis.phase4_telemetry_authorization_payload(TEXT,JSONB) FROM PUBLIC, market_migrator;
      REVOKE ALL ON FUNCTION analysis.write_phase4_execution(JSONB,TEXT), analysis.write_phase4_paper_execution(JSONB,TEXT), analysis.write_phase4_book_attribution(JSONB,TEXT) FROM PUBLIC, market_migrator;
      GRANT EXECUTE ON FUNCTION analysis.phase4_telemetry_authorization_payload(TEXT,JSONB),
        analysis.write_phase4_execution(JSONB,TEXT), analysis.write_phase4_paper_execution(JSONB,TEXT), analysis.write_phase4_book_attribution(JSONB,TEXT) TO market_app;
    """)


def downgrade() -> None:
    op.execute("""
      REVOKE ALL ON FUNCTION analysis.phase4_telemetry_authorization_payload(TEXT,JSONB), analysis.write_phase4_execution(JSONB,TEXT), analysis.write_phase4_paper_execution(JSONB,TEXT), analysis.write_phase4_book_attribution(JSONB,TEXT) FROM market_app, PUBLIC;
      DROP FUNCTION IF EXISTS analysis.write_phase4_book_attribution(JSONB,TEXT);
      DROP FUNCTION IF EXISTS analysis.write_phase4_paper_execution(JSONB,TEXT);
      DROP FUNCTION IF EXISTS analysis.write_phase4_execution(JSONB,TEXT);
      DROP FUNCTION IF EXISTS analysis.phase4_telemetry_authorized(TEXT,JSONB,TEXT);
      DROP FUNCTION IF EXISTS analysis.phase4_telemetry_authorization_payload(TEXT,JSONB);
      GRANT EXECUTE ON FUNCTION analysis.insert_phase4_execution(TEXT,TEXT,TEXT,TEXT,INTEGER,DOUBLE PRECISION,DOUBLE PRECISION,DOUBLE PRECISION,DOUBLE PRECISION,TIMESTAMPTZ,TEXT,TEXT,JSONB) TO market_app;
      GRANT EXECUTE ON FUNCTION analysis.insert_phase4_paper_execution_observation(JSONB), analysis.insert_phase4_book_attribution(JSONB) TO market_app;
    """)
