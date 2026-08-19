"""Add point-in-time trend, strategy-route, and research evidence contracts.

Revision ID: 20260819_0041
Revises: 20260815_0040
"""

from __future__ import annotations

from alembic import op


revision = "20260819_0041"
down_revision = "20260815_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO analysis.option_history_canary (model_revision)
        SELECT 'history-v3-price-shape-r5-contract-terms'
        WHERE NOT EXISTS (
          SELECT 1 FROM analysis.option_history_canary
          WHERE model_revision = 'history-v3-price-shape-r5-contract-terms'
        );

        ALTER TABLE catalog.option_contract ADD COLUMN deliverable_key TEXT;
        ALTER TABLE catalog.option_contract
          ADD COLUMN standard_contract_verified BOOLEAN NOT NULL DEFAULT false;
        ALTER TABLE catalog.option_contract
          ADD CONSTRAINT ck_option_contract_standard_terms
          CHECK (
            NOT standard_contract_verified OR (
              style = 'american' AND settlement = 'physical' AND deliverable_key IS NOT NULL
            )
          );
        ALTER TABLE raw.option_quote
          ADD COLUMN contract_style TEXT,
          ADD COLUMN contract_settlement TEXT,
          ADD COLUMN contract_deliverable_key TEXT,
          ADD COLUMN standard_contract_verified BOOLEAN NOT NULL DEFAULT false;
        ALTER TABLE raw.option_quote
          ADD CONSTRAINT ck_option_quote_standard_terms
          CHECK (
            NOT standard_contract_verified OR (
              contract_style = 'american'
              AND contract_settlement = 'physical'
              AND contract_deliverable_key IS NOT NULL
            )
          );
        UPDATE catalog.option_contract
        SET deliverable_key = concat('legacy-unverified:', id::text)
        WHERE deliverable_key IS NULL;
        ALTER TABLE catalog.option_contract ALTER COLUMN deliverable_key SET NOT NULL;
        ALTER TABLE catalog.option_contract
          DROP CONSTRAINT option_contract_underlying_instrument_id_expiration_strike__key;
        ALTER TABLE catalog.option_contract
          ADD CONSTRAINT uq_option_contract_deliverable
          UNIQUE (underlying_instrument_id, expiration, strike, option_type, multiplier, deliverable_key);

        ALTER TABLE analysis.run ADD COLUMN inputs JSONB NOT NULL DEFAULT '{}'::jsonb;

        ALTER TABLE analysis.symbol_feature
          ADD COLUMN momentum_5d DOUBLE PRECISION,
          ADD COLUMN momentum_20d DOUBLE PRECISION,
          ADD COLUMN relative_strength_60d DOUBLE PRECISION,
          ADD COLUMN kaufman_er_20d DOUBLE PRECISION,
          ADD COLUMN kaufman_er_60d DOUBLE PRECISION,
          ADD COLUMN kama_fast DOUBLE PRECISION,
          ADD COLUMN kama_slow DOUBLE PRECISION,
          ADD COLUMN kama_fast_slope DOUBLE PRECISION,
          ADD COLUMN kama_slow_slope DOUBLE PRECISION,
          ADD COLUMN trend_state TEXT NOT NULL DEFAULT 'unavailable',
          ADD COLUMN trend_confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
          ADD COLUMN volatility_state TEXT NOT NULL DEFAULT 'unstable',
          ADD COLUMN data_quality_status TEXT NOT NULL DEFAULT 'unavailable',
          ADD COLUMN reason_codes TEXT[] NOT NULL DEFAULT '{}';

        ALTER TABLE analysis.symbol_feature
          ADD CONSTRAINT ck_symbol_feature_trend_state
          CHECK (trend_state IN ('trend_up', 'trend_down', 'range', 'transition', 'unavailable')),
          ADD CONSTRAINT ck_symbol_feature_trend_confidence
          CHECK (trend_confidence >= 0 AND trend_confidence <= 1),
          ADD CONSTRAINT ck_symbol_feature_volatility_state
          CHECK (volatility_state IN ('low', 'normal', 'high', 'unstable'));

        ALTER TABLE analysis.option_decision
          ADD COLUMN route_version TEXT,
          ADD COLUMN strategy_route JSONB NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN market_regime_detail JSONB NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN event_state TEXT;

        ALTER TABLE analysis.option_decision
          ADD CONSTRAINT ck_option_decision_strategy_route_object
          CHECK (jsonb_typeof(strategy_route) = 'object'),
          ADD CONSTRAINT ck_option_decision_market_regime_detail_object
          CHECK (jsonb_typeof(market_regime_detail) = 'object');

        ALTER TABLE raw.market_event ADD COLUMN available_at TIMESTAMPTZ;
        UPDATE raw.market_event event
        SET available_at = COALESCE(run.finished_at, run.started_at, clock_timestamp())
        FROM ingest.run run
        WHERE run.id = event.ingest_run_id AND event.available_at IS NULL;
        UPDATE raw.market_event SET available_at = clock_timestamp() WHERE available_at IS NULL;
        ALTER TABLE raw.market_event
          ALTER COLUMN available_at SET DEFAULT clock_timestamp(),
          ALTER COLUMN available_at SET NOT NULL;
        CREATE INDEX ix_raw_market_event_point_in_time
          ON raw.market_event (event_kind, starts_at, available_at);

        CREATE TABLE raw.market_event_version (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            market_event_id BIGINT NOT NULL REFERENCES raw.market_event(id) ON DELETE CASCADE,
            instrument_id BIGINT REFERENCES catalog.instrument(id),
            source_id TEXT NOT NULL REFERENCES ingest.source(id),
            ingest_run_id UUID NOT NULL REFERENCES ingest.run(id),
            payload_id BIGINT REFERENCES ingest.payload(id),
            source_key TEXT NOT NULL,
            event_scope TEXT NOT NULL,
            event_kind TEXT NOT NULL,
            title TEXT NOT NULL,
            starts_at TIMESTAMPTZ NOT NULL,
            ends_at TIMESTAMPTZ,
            importance TEXT,
            verification_status TEXT,
            source_url TEXT,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (market_event_id, ingest_run_id)
        );
        INSERT INTO raw.market_event_version (
            market_event_id, instrument_id, source_id, ingest_run_id, payload_id,
            source_key, event_scope, event_kind, title, starts_at, ends_at, importance,
            verification_status, source_url, details, available_at
        )
        SELECT id, instrument_id, source_id, ingest_run_id, payload_id, source_key,
               event_scope, event_kind, title, starts_at, ends_at, importance,
               verification_status, source_url, details, available_at
        FROM raw.market_event;
        CREATE INDEX ix_raw_market_event_version_point_in_time
          ON raw.market_event_version (event_kind, starts_at, available_at);

        CREATE TABLE analysis.event_study_feature (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id UUID NOT NULL REFERENCES analysis.run(id) ON DELETE RESTRICT,
            instrument_id BIGINT REFERENCES catalog.instrument(id) ON DELETE RESTRICT,
            market_event_id BIGINT NOT NULL REFERENCES raw.market_event(id) ON DELETE CASCADE,
            market_event_version_id BIGINT NOT NULL REFERENCES raw.market_event_version(id) ON DELETE RESTRICT,
            as_of TIMESTAMPTZ NOT NULL,
            event_kind TEXT NOT NULL,
            event_session TEXT NOT NULL,
            pre_event_regime TEXT NOT NULL,
            horizon INTEGER NOT NULL CHECK (horizon > 0),
            sample_size INTEGER NOT NULL CHECK (sample_size >= 0),
            actual_move_median DOUBLE PRECISION,
            actual_move_p75 DOUBLE PRECISION,
            actual_move_p90 DOUBLE PRECISION,
            bootstrap_low DOUBLE PRECISION,
            bootstrap_high DOUBLE PRECISION,
            win_rate DOUBLE PRECISION,
            iv_crush_frequency DOUBLE PRECISION,
            atm_iv DOUBLE PRECISION,
            skew_25d DOUBLE PRECISION,
            term_slope DOUBLE PRECISION,
            implied_move DOUBLE PRECISION,
            evidence_state TEXT NOT NULL,
            feature_version TEXT NOT NULL,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (run_id, instrument_id, market_event_version_id, horizon, feature_version),
            CONSTRAINT ck_event_study_evidence_state CHECK (
              evidence_state IN ('ready', 'insufficient_event_evidence', 'unavailable')
            )
        );
        CREATE INDEX ix_event_study_feature_lookup
          ON analysis.event_study_feature (instrument_id, event_kind, as_of DESC);

        CREATE TABLE analysis.option_surface_shift (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(id) ON DELETE RESTRICT,
            current_capture_generation_id BIGINT NOT NULL REFERENCES raw.option_capture_generation(id),
            previous_capture_generation_id BIGINT NOT NULL REFERENCES raw.option_capture_generation(id),
            current_analysis_run_id UUID NOT NULL REFERENCES analysis.run(id) ON DELETE RESTRICT,
            previous_analysis_run_id UUID NOT NULL REFERENCES analysis.run(id) ON DELETE RESTRICT,
            as_of TIMESTAMPTZ NOT NULL,
            previous_as_of TIMESTAMPTZ,
            feature_version TEXT NOT NULL,
            tenors INTEGER[] NOT NULL DEFAULT ARRAY[7,14,30,60,90],
            w1_shift DOUBLE PRECISION,
            tail_mass_change DOUBLE PRECISION,
            skew_shift DOUBLE PRECISION,
            term_shift DOUBLE PRECISION,
            evidence_state TEXT NOT NULL,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (current_analysis_run_id, previous_analysis_run_id, feature_version),
            CONSTRAINT ck_surface_shift_evidence_state CHECK (
              evidence_state IN ('ready', 'insufficient_surface_evidence', 'unavailable')
            )
        );
        CREATE INDEX ix_option_surface_shift_lookup
          ON analysis.option_surface_shift (instrument_id, as_of DESC);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM analysis.option_history_canary
        WHERE model_revision = 'history-v3-price-shape-r5-contract-terms';
        DROP TABLE analysis.option_surface_shift;
        DROP TABLE analysis.event_study_feature;
        DROP TABLE raw.market_event_version;
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM catalog.option_contract
            GROUP BY underlying_instrument_id, expiration, strike, option_type, multiplier
            HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'cannot downgrade: multiple option deliverables share the legacy key';
          END IF;
        END $$;
        ALTER TABLE catalog.option_contract DROP CONSTRAINT uq_option_contract_deliverable;
        ALTER TABLE raw.option_quote
          DROP CONSTRAINT ck_option_quote_standard_terms,
          DROP COLUMN standard_contract_verified,
          DROP COLUMN contract_deliverable_key,
          DROP COLUMN contract_settlement,
          DROP COLUMN contract_style;
        ALTER TABLE catalog.option_contract DROP CONSTRAINT ck_option_contract_standard_terms;
        ALTER TABLE catalog.option_contract DROP COLUMN standard_contract_verified;
        ALTER TABLE catalog.option_contract DROP COLUMN deliverable_key;
        ALTER TABLE catalog.option_contract
          ADD CONSTRAINT option_contract_underlying_instrument_id_expiration_strike__key
          UNIQUE (underlying_instrument_id, expiration, strike, option_type, multiplier);
        DROP INDEX raw.ix_raw_market_event_point_in_time;
        ALTER TABLE raw.market_event DROP COLUMN available_at;
        ALTER TABLE analysis.option_decision
          DROP CONSTRAINT ck_option_decision_market_regime_detail_object,
          DROP CONSTRAINT ck_option_decision_strategy_route_object,
          DROP COLUMN event_state,
          DROP COLUMN market_regime_detail,
          DROP COLUMN strategy_route,
          DROP COLUMN route_version;
        ALTER TABLE analysis.symbol_feature
          DROP CONSTRAINT ck_symbol_feature_volatility_state,
          DROP CONSTRAINT ck_symbol_feature_trend_confidence,
          DROP CONSTRAINT ck_symbol_feature_trend_state,
          DROP COLUMN reason_codes,
          DROP COLUMN data_quality_status,
          DROP COLUMN volatility_state,
          DROP COLUMN trend_confidence,
          DROP COLUMN trend_state,
          DROP COLUMN kama_slow_slope,
          DROP COLUMN kama_fast_slope,
          DROP COLUMN kama_slow,
          DROP COLUMN kama_fast,
          DROP COLUMN kaufman_er_60d,
          DROP COLUMN kaufman_er_20d,
          DROP COLUMN relative_strength_60d,
          DROP COLUMN momentum_20d,
          DROP COLUMN momentum_5d;
        ALTER TABLE analysis.run DROP COLUMN inputs;
        """
    )
