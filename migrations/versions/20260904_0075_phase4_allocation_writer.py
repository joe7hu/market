"""Make the Phase 4 allocation writer repository-signed and multi-source."""

from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa


revision = "20260904_0075"
down_revision = "20260904_0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    key = os.environ.get("MARKET_PHASE4_ALLOCATION_SIGNING_KEY", "").strip()
    if key and len(key) < 16:
        raise RuntimeError("MARKET_PHASE4_ALLOCATION_SIGNING_KEY must contain at least 16 characters")
    op.execute("""
        ALTER TABLE analysis.portfolio_allocation_item
            ADD COLUMN IF NOT EXISTS funding_sources JSONB NOT NULL DEFAULT '{}'::jsonb;
        DO $$
        DECLARE item_constraint RECORD;
        BEGIN
          FOR item_constraint IN
            SELECT conname FROM pg_constraint
             WHERE conrelid = 'analysis.portfolio_allocation_item'::regclass
               AND pg_get_constraintdef(oid) LIKE '%funding_source%'
          LOOP
            EXECUTE format('ALTER TABLE analysis.portfolio_allocation_item DROP CONSTRAINT %I', item_constraint.conname);
          END LOOP;
        END $$;
        ALTER TABLE analysis.portfolio_allocation_item
          ADD CONSTRAINT phase4_allocation_item_funding_sources_shape
          CHECK (jsonb_typeof(funding_sources) = 'object'),
          ADD CONSTRAINT phase4_allocation_item_funding_sources_required
          CHECK (ticker = 'CASH' OR disposition <> 'selected' OR target_weight <= current_weight
                 OR (funding_amount IS NOT NULL AND funding_amount > 0 AND funding_sources <> '{}'::jsonb));
        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_funding_content()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
            'allocation_item_id', NEW.allocation_item_id, 'allocation_id', NEW.allocation_id,
            'candidate_id', NEW.candidate_id, 'ticker', NEW.ticker,
            'strategy_forecast_id', NEW.strategy_forecast_id, 'action_id', NEW.action_id,
            'rank_id', NEW.rank_id, 'hypothesis_id', NEW.hypothesis_id,
            'disposition', NEW.disposition, 'target_weight', NEW.target_weight,
            'current_weight', NEW.current_weight, 'marginal_book_utility', NEW.marginal_book_utility,
            'trace', NEW.trace, 'blockers', NEW.blockers, 'funding_source', NEW.funding_source,
            'funding_amount', NEW.funding_amount, 'funding_sources', NEW.funding_sources));
          RETURN NEW;
        END;
        $$;
        DROP TRIGGER IF EXISTS zzz_phase4_funding_content ON analysis.portfolio_allocation_item;
        CREATE TRIGGER zzz_phase4_funding_content BEFORE INSERT ON analysis.portfolio_allocation_item
          FOR EACH ROW EXECUTE FUNCTION analysis.enforce_phase4_funding_content();
        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_execution_snapshot_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE allocation_cutoff TIMESTAMPTZ;
        BEGIN
          SELECT input_cutoff INTO allocation_cutoff
            FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id;
          IF allocation_cutoff IS NULL OR NEW.input_cutoff <= allocation_cutoff THEN
            RAISE EXCEPTION 'Phase 4 execution snapshot cutoff must follow its allocation';
          END IF;
          IF NEW.calibration_status = 'calibrated' AND (
            NEW.sample_count <= 0 OR jsonb_typeof(NEW.metadata->'paper_observation_ids') IS DISTINCT FROM 'array'
            OR jsonb_array_length(NEW.metadata->'paper_observation_ids') <> NEW.sample_count
            OR EXISTS (
              SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'paper_observation_ids') id
              LEFT JOIN app.paper_execution_observation observation ON observation.paper_execution_observation_id = id
              LEFT JOIN analysis.portfolio_allocation_item item ON item.allocation_item_id = observation.allocation_item_id
              LEFT JOIN app.paper_order paper ON paper.id = observation.paper_order_id
              WHERE observation.paper_execution_observation_id IS NULL OR item.allocation_id <> NEW.allocation_id
                OR observation.available_at <= allocation_cutoff OR observation.available_at > NEW.input_cutoff
                OR observation.filled_quantity <= 0 OR observation.fill_price IS NULL
                OR observation.contract_multiplier IS NULL OR observation.event_fee IS NULL
                OR paper.submitted_at IS NULL OR paper.filled_at IS NULL OR paper.fill_evidence_at IS NULL
                OR paper.execution_quote IS NULL OR paper.actual_fill_price IS NULL)) THEN
            RAISE EXCEPTION 'Phase 4 calibrated snapshot requires matching post-allocation paper fills';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_review_snapshot_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF coalesce(NEW.metadata->>'authority', '') <> 'postgresql'
             OR NEW.metadata->>'authority_snapshot_id' !~ '^broker-account:[0-9]+$'
             OR jsonb_typeof(NEW.metadata->'source_hashes') IS DISTINCT FROM 'array'
             OR (NEW.status <> 'cash_only' AND jsonb_array_length(NEW.metadata->'source_hashes') = 0)
             OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'source_hashes') hash
                        WHERE hash !~ '^[0-9a-f]{64}$' OR hash = repeat('0', 64))
             OR NOT EXISTS (SELECT 1 FROM raw.broker_account_snapshot account
                            WHERE ('broker-account:' || account.id::text) = NEW.metadata->>'authority_snapshot_id'
                              AND account.observed_at <= NEW.input_cutoff) THEN
            RAISE EXCEPTION 'Phase 4 allocation requires PostgreSQL authority evidence';
          END IF;
          NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
            'allocation_id', NEW.allocation_id, 'as_of', analysis.phase4_canonical_timestamp(NEW.as_of),
            'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff), 'status', NEW.status,
            'cash_hurdle', NEW.cash_hurdle, 'forecast_ids', NEW.forecast_ids, 'action_ids', NEW.action_ids,
            'strategy_registry_ids', NEW.strategy_registry_ids, 'metadata', NEW.metadata));
          RETURN NEW;
        END;
        $$;

        CREATE TABLE IF NOT EXISTS analysis.phase4_allocation_signing_secret (
            singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
            secret BYTEA NOT NULL CHECK (length(secret) >= 16),
            installed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        );
        REVOKE ALL ON analysis.phase4_allocation_signing_secret FROM PUBLIC, market_app, market_migrator;
        CREATE OR REPLACE FUNCTION analysis.phase4_allocation_signing_key()
        RETURNS TEXT LANGUAGE sql SECURITY DEFINER STABLE
        SET search_path = pg_catalog, analysis AS $$
          SELECT convert_from(secret, 'UTF8') FROM analysis.phase4_allocation_signing_secret WHERE singleton
        $$;
        REVOKE ALL ON FUNCTION analysis.phase4_allocation_signing_key() FROM PUBLIC, market_app, market_migrator;
        CREATE OR REPLACE FUNCTION analysis.phase4_allocation_authorization_payload(p_snapshot JSONB, p_items JSONB)
        RETURNS TEXT LANGUAGE sql IMMUTABLE
        SET search_path = pg_catalog, analysis AS $$
          SELECT analysis.phase4_canonical_json(jsonb_build_object(
            'contract', 'phase4-allocation-writer.v1', 'snapshot', p_snapshot, 'items', p_items))
        $$;

        CREATE OR REPLACE FUNCTION analysis.write_phase4_allocation(
            p_snapshot JSONB, p_items JSONB, p_authorization_signature TEXT
        ) RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, analysis, raw, public AS $$
        DECLARE signing_key TEXT; expected_signature TEXT; account_id BIGINT;
                cutoff TIMESTAMPTZ; expected_hashes JSONB; expected_forecasts JSONB; expected_actions JSONB;
        BEGIN
          signing_key := analysis.phase4_allocation_signing_key();
          IF signing_key IS NULL OR length(signing_key) < 16 THEN
            RAISE EXCEPTION 'Phase 4 allocation signing key is not configured';
          END IF;
          expected_signature := encode(public.hmac(
            convert_to(analysis.phase4_allocation_authorization_payload(p_snapshot, p_items), 'UTF8'),
            convert_to(signing_key, 'UTF8'), 'sha256'::TEXT), 'hex');
          IF p_authorization_signature IS NULL OR lower(p_authorization_signature) <> expected_signature THEN
            RAISE EXCEPTION 'Phase 4 allocation authorization signature is invalid';
          END IF;
          IF jsonb_typeof(p_snapshot) <> 'object' OR jsonb_typeof(p_items) <> 'array'
             OR jsonb_array_length(p_items) = 0 OR p_snapshot->>'authority' IS NOT NULL THEN
            RAISE EXCEPTION 'Phase 4 allocation writer payload is malformed';
          END IF;
          cutoff := (p_snapshot->>'input_cutoff')::timestamptz;
          IF p_snapshot->'metadata'->>'authority' <> 'postgresql'
             OR p_snapshot->'metadata'->>'authority_snapshot_id' !~ '^broker-account:[0-9]+$' THEN
            RAISE EXCEPTION 'Phase 4 allocation authority metadata is invalid';
          END IF;
          account_id := split_part(p_snapshot->'metadata'->>'authority_snapshot_id', ':', 2)::BIGINT;
          IF NOT EXISTS (SELECT 1 FROM raw.broker_account_snapshot account
                         WHERE account.id = account_id AND account.observed_at <= cutoff) THEN
            RAISE EXCEPTION 'Phase 4 allocation authority account is not a PIT PostgreSQL row';
          END IF;
          IF EXISTS (SELECT 1 FROM jsonb_array_elements(p_items) item
                     WHERE item->>'allocation_id' IS DISTINCT FROM p_snapshot->>'allocation_id') THEN
            RAISE EXCEPTION 'Phase 4 allocation items cannot be recombined across snapshots';
          END IF;
          IF EXISTS (
            SELECT 1 FROM jsonb_array_elements(p_items) item
             WHERE item->>'ticker' <> 'CASH' AND item->>'disposition' IN ('selected', 'rollback')
               AND NOT EXISTS (
                 SELECT 1 FROM analysis.ticker_decision decision
                 JOIN analysis.strategy_forecast forecast ON forecast.id = item->>'strategy_forecast_id'
                  WHERE decision.id::text = item->'trace'->>'source_decision_id'
                    AND decision.input_hash = item->'trace'->>'source_decision_input_hash'
                    AND decision.status = 'published' AND decision.published_at IS NOT NULL
                    AND decision.input_manifest->'trade_plan'->>'trade_plan_id' = item->>'action_id'
                    AND decision.input_manifest->'trade_plan'->>'rank_id' = item->>'rank_id'
                    AND decision.input_manifest->'trade_plan'->>'strategy_forecast_id' = item->>'strategy_forecast_id'
                    AND forecast.input_cutoff <= cutoff AND decision.published_at <= cutoff)) THEN
            RAISE EXCEPTION 'Phase 4 allocation source action lineage is invalid';
          END IF;
          SELECT coalesce(jsonb_agg(DISTINCT item->'trace'->>'source_decision_input_hash' ORDER BY item->'trace'->>'source_decision_input_hash'), '[]'::jsonb),
                 coalesce(jsonb_agg(DISTINCT item->>'strategy_forecast_id' ORDER BY item->>'strategy_forecast_id'), '[]'::jsonb),
                 coalesce(jsonb_agg(DISTINCT item->>'action_id' ORDER BY item->>'action_id'), '[]'::jsonb)
            INTO expected_hashes, expected_forecasts, expected_actions
            FROM jsonb_array_elements(p_items) item
           WHERE item->>'ticker' <> 'CASH' AND item->>'disposition' IN ('selected', 'rollback');
          IF (p_snapshot->>'status' <> 'cash_only' AND (
                coalesce(p_snapshot->'metadata'->'source_hashes', '[]'::jsonb) <> expected_hashes
             OR coalesce(p_snapshot->'forecast_ids', '[]'::jsonb) <> expected_forecasts
             OR coalesce(p_snapshot->'action_ids', '[]'::jsonb) <> expected_actions)) THEN
            RAISE EXCEPTION 'Phase 4 allocation source hashes or identities are not canonical';
          END IF;
          INSERT INTO analysis.portfolio_allocation_snapshot
            (allocation_id, as_of, input_cutoff, status, cash_hurdle, forecast_ids, action_ids,
             strategy_registry_ids, input_hash, content_hash, metadata)
          VALUES (p_snapshot->>'allocation_id', (p_snapshot->>'as_of')::timestamptz,
                  cutoff, p_snapshot->>'status', (p_snapshot->>'cash_hurdle')::double precision,
                  p_snapshot->'forecast_ids', p_snapshot->'action_ids', p_snapshot->'strategy_registry_ids',
                  p_snapshot->>'input_hash', p_snapshot->>'content_hash', p_snapshot->'metadata')
          ON CONFLICT (allocation_id) DO NOTHING;
          INSERT INTO analysis.portfolio_allocation_item
            (allocation_item_id, allocation_id, candidate_id, ticker, strategy_forecast_id, action_id, rank_id,
             hypothesis_id, disposition, target_weight, current_weight, marginal_book_utility, trace, blockers,
             funding_source, funding_amount, funding_sources, input_hash, content_hash)
          SELECT item->>'allocation_item_id', item->>'allocation_id', item->>'candidate_id', item->>'ticker',
                 nullif(item->>'strategy_forecast_id', ''), nullif(item->>'action_id', ''), nullif(item->>'rank_id', ''),
                 nullif(item->>'hypothesis_id', '')::uuid, item->>'disposition', (item->>'target_weight')::double precision,
                 (item->>'current_weight')::double precision, (item->>'marginal_book_utility')::double precision,
                 item->'trace', item->'blockers', nullif(item->>'funding_source', ''),
                 (item->>'funding_amount')::double precision, coalesce(item->'funding_sources', '{}'::jsonb),
                 item->>'input_hash', item->>'content_hash'
            FROM jsonb_array_elements(p_items) item
          ON CONFLICT (allocation_item_id) DO NOTHING;
        END;
        $$;
        REVOKE ALL ON FUNCTION analysis.insert_phase4_allocation_snapshot(JSONB),
          analysis.insert_phase4_allocation_item(JSONB), analysis.insert_phase4_paper_execution_observation(JSONB),
          analysis.insert_phase4_book_attribution(JSONB) FROM PUBLIC, market_app, market_migrator;
        REVOKE ALL ON FUNCTION analysis.write_phase4_allocation(JSONB, JSONB, TEXT) FROM PUBLIC, market_migrator;
        REVOKE ALL ON FUNCTION analysis.phase4_allocation_authorization_payload(JSONB, JSONB) FROM PUBLIC, market_migrator;
        GRANT EXECUTE ON FUNCTION analysis.write_phase4_allocation(JSONB, JSONB, TEXT),
          analysis.phase4_allocation_authorization_payload(JSONB, JSONB) TO market_app;
    """)
    if key:
        op.get_bind().execute(sa.text("""
            INSERT INTO analysis.phase4_allocation_signing_secret (singleton, secret)
            VALUES (true, convert_to(:secret, 'UTF8'))
            ON CONFLICT (singleton) DO UPDATE SET secret = EXCLUDED.secret, installed_at = clock_timestamp()
        """), {"secret": key})


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER IF EXISTS zzz_phase4_funding_content ON analysis.portfolio_allocation_item;
        DROP FUNCTION IF EXISTS analysis.enforce_phase4_funding_content();
        DROP FUNCTION IF EXISTS analysis.write_phase4_allocation(JSONB, JSONB, TEXT);
        DROP FUNCTION IF EXISTS analysis.phase4_allocation_authorization_payload(JSONB, JSONB);
        DROP FUNCTION IF EXISTS analysis.phase4_allocation_signing_key();
        DROP TABLE IF EXISTS analysis.phase4_allocation_signing_secret;
        ALTER TABLE analysis.portfolio_allocation_item DROP COLUMN IF EXISTS funding_sources;
        ALTER TABLE analysis.portfolio_allocation_item
          ADD CONSTRAINT phase4_allocation_item_funding_source_shape
          CHECK (ticker = 'CASH' OR disposition <> 'selected'
                 OR (funding_source IS NOT NULL AND (funding_source LIKE 'CASH:%' OR funding_source LIKE 'TRIM:%'))),
          ADD CONSTRAINT phase4_allocation_item_funding_amount_required
          CHECK (ticker = 'CASH' OR disposition <> 'selected' OR (funding_amount IS NOT NULL AND funding_amount > 0));

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_review_snapshot_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF coalesce(NEW.metadata->>'authority', '') <> 'postgresql'
               OR coalesce(NEW.metadata->>'authority_snapshot_id', '') = ''
               OR jsonb_typeof(NEW.metadata->'source_hashes') IS DISTINCT FROM 'array'
               OR jsonb_array_length(NEW.metadata->'source_hashes') = 0
               OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'source_hashes') hash
                          WHERE hash !~ '^[0-9a-f]{64}$' OR hash = repeat('0', 64)) THEN
                RAISE EXCEPTION 'Phase 4 allocation requires PostgreSQL authority evidence';
            END IF;
            IF NEW.status <> 'cash_only' AND NOT EXISTS (
                SELECT 1 FROM raw.broker_account_snapshot account
                WHERE ('broker-account:' || account.id::text) = NEW.metadata->>'authority_snapshot_id'
                  AND account.observed_at <= NEW.input_cutoff
            ) THEN
                RAISE EXCEPTION 'Phase 4 allocation authority is not persisted';
            END IF;
            NEW.content_hash := analysis.phase4_content_digest(jsonb_build_object(
                'allocation_id', NEW.allocation_id, 'as_of', analysis.phase4_canonical_timestamp(NEW.as_of),
                'input_cutoff', analysis.phase4_canonical_timestamp(NEW.input_cutoff), 'status', NEW.status,
                'cash_hurdle', NEW.cash_hurdle, 'forecast_ids', NEW.forecast_ids,
                'action_ids', NEW.action_ids, 'strategy_registry_ids', NEW.strategy_registry_ids,
                'metadata', NEW.metadata));
            RETURN NEW;
        END;
        $$;

        CREATE OR REPLACE FUNCTION analysis.enforce_phase4_execution_snapshot_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE expected_cutoff TIMESTAMPTZ;
        BEGIN
            SELECT input_cutoff INTO expected_cutoff FROM analysis.portfolio_allocation_snapshot WHERE allocation_id = NEW.allocation_id;
            IF expected_cutoff IS NULL OR NEW.input_cutoff < expected_cutoff THEN
                RAISE EXCEPTION 'Phase 4 execution snapshot cutoff is not bounded by allocation';
            END IF;
            IF NEW.calibration_status = 'calibrated' AND (
                NEW.sample_count <= 0 OR jsonb_typeof(NEW.metadata->'paper_observation_ids') IS DISTINCT FROM 'array'
                OR jsonb_array_length(NEW.metadata->'paper_observation_ids') <> NEW.sample_count
                OR EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(NEW.metadata->'paper_observation_ids') id
                    JOIN app.paper_execution_observation observation ON observation.paper_execution_observation_id = id
                    JOIN analysis.portfolio_allocation_item item ON item.allocation_item_id = observation.allocation_item_id
                    JOIN app.paper_order paper ON paper.id = observation.paper_order_id
                    WHERE (SELECT input_cutoff FROM analysis.portfolio_allocation_snapshot prior
                           WHERE prior.allocation_id = item.allocation_id) > observation.available_at
                       OR observation.available_at > NEW.input_cutoff
                       OR observation.observed_at IS DISTINCT FROM paper.filled_at OR observation.available_at IS DISTINCT FROM paper.fill_evidence_at
                       OR observation.filled_quantity <= 0 OR observation.fill_price IS NULL OR paper.submitted_at IS NULL
                       OR paper.filled_at IS NULL OR paper.fill_evidence_at IS NULL OR paper.execution_quote IS NULL
                       OR paper.entry_fees IS NULL OR paper.contract_multiplier IS NULL OR paper.actual_fill_price IS NULL
                       OR paper.status NOT IN ('open', 'entered', 'partial_exited', 'exited', 'closed', 'invalidated')
                )
            ) THEN RAISE EXCEPTION 'Phase 4 calibrated snapshot requires matching persisted fills'; END IF;
            RETURN NEW;
        END;
        $$;
        GRANT EXECUTE ON FUNCTION analysis.insert_phase4_allocation_snapshot(JSONB),
          analysis.insert_phase4_allocation_item(JSONB), analysis.insert_phase4_paper_execution_observation(JSONB),
          analysis.insert_phase4_book_attribution(JSONB),
          analysis.insert_phase4_execution(TEXT,TEXT,TEXT,TEXT,INTEGER,DOUBLE PRECISION,DOUBLE PRECISION,DOUBLE PRECISION,DOUBLE PRECISION,TIMESTAMPTZ,TEXT,TEXT,JSONB)
          TO market_app;
    """)
