-- Application and ingest trigger functions.

CREATE FUNCTION app.capture_portfolio_transaction_sector() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.instrument_id IS NOT NULL AND NEW.instrument_sector IS NULL THEN
                SELECT instrument.sector
                  INTO NEW.instrument_sector
                  FROM catalog.instrument instrument
                 WHERE instrument.id = NEW.instrument_id;
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE FUNCTION app.prevent_manual_account_snapshot_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          RAISE EXCEPTION 'manual account snapshots are append-only';
        END;
        $$;

CREATE FUNCTION app.prevent_paper_order_leg_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
          target_order_id uuid;
          expected jsonb;
        BEGIN
          IF TG_OP = 'UPDATE' AND (
            NEW.paper_order_id IS DISTINCT FROM OLD.paper_order_id
            OR NEW.leg_index IS DISTINCT FROM OLD.leg_index
          ) THEN
            RAISE EXCEPTION 'paper order leg identity is immutable';
          END IF;
          target_order_id := CASE WHEN TG_OP = 'INSERT'
            THEN NEW.paper_order_id ELSE OLD.paper_order_id END;
          IF EXISTS (
            SELECT 1 FROM app.paper_order
            WHERE id = target_order_id AND ticket_version IS NOT NULL
          ) THEN
            IF TG_OP = 'INSERT' THEN
              SELECT ticket_snapshot->'legs'->NEW.leg_index
              INTO expected
              FROM app.paper_order
              WHERE id = NEW.paper_order_id;
              IF expected IS NULL OR ROW(
                expected->>'contract_id',
                expected->>'option_type',
                expected->>'side',
                (expected->>'strike')::numeric,
                (expected->>'bid')::numeric,
                (expected->>'ask')::numeric,
                (expected->>'bid_size')::integer,
                (expected->>'ask_size')::integer,
                (expected->>'quote_time')::timestamptz,
                (expected->>'open_interest')::integer,
                (expected->>'volume')::integer
              ) IS DISTINCT FROM ROW(
                NEW.contract_id::text,
                NEW.option_type,
                NEW.side,
                NEW.strike,
                NEW.bid,
                NEW.ask,
                NEW.bid_size,
                NEW.ask_size,
                NEW.quote_time,
                NEW.open_interest,
                NEW.volume
              ) THEN
                RAISE EXCEPTION 'paper order leg does not match immutable ticket';
              END IF;
              RETURN NEW;
            END IF;
            RAISE EXCEPTION 'ticketed paper order legs are append-only';
          END IF;
          RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;

CREATE FUNCTION app.prevent_paper_order_ticket_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
          IF OLD.ticket_version IS NOT NULL THEN
            IF TG_OP = 'DELETE' THEN
              RAISE EXCEPTION 'ticketed paper order audit is append-only';
            END IF;
            IF ROW(
              NEW.decision_id, NEW.instrument_id, NEW.side, NEW.quantity,
              NEW.limit_price, NEW.intended_limit_price, NEW.structure,
              NEW.reserved_collateral, NEW.idempotency_key,
              NEW.ticket_version, NEW.ticket_snapshot,
              NEW.policy_result, NEW.policy_snapshot, NEW.lane, NEW.created_at
            ) IS DISTINCT FROM ROW(
              OLD.decision_id, OLD.instrument_id, OLD.side, OLD.quantity,
              OLD.limit_price, OLD.intended_limit_price, OLD.structure,
              OLD.reserved_collateral, OLD.idempotency_key,
              OLD.ticket_version, OLD.ticket_snapshot,
              OLD.policy_result, OLD.policy_snapshot, OLD.lane, OLD.created_at
            ) THEN
              RAISE EXCEPTION 'ticketed paper order intent is immutable';
            END IF;
          END IF;
          RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;

CREATE FUNCTION app.require_complete_paper_order_leg_set() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
          expected_count integer;
          actual_count integer;
        BEGIN
          IF NEW.ticket_version IS NULL THEN
            RETURN NEW;
          END IF;
          expected_count := jsonb_array_length(coalesce(NEW.ticket_snapshot->'legs', '[]'::jsonb));
          SELECT count(*) INTO actual_count
          FROM app.paper_order_leg
          WHERE paper_order_id = NEW.id;
          IF actual_count IS DISTINCT FROM expected_count THEN
            RAISE EXCEPTION 'ticketed paper order requires the complete immutable leg set';
          END IF;
          RETURN NEW;
        END;
        $$;

CREATE FUNCTION ingest.record_source_lifecycle() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'ingest'
    AS $$
        BEGIN
            INSERT INTO ingest.source_lifecycle_history
                (source_id, effective_at, enabled, operational_state)
            VALUES (NEW.id, COALESCE(NULLIF(current_setting('market.phase2_effective_at', true), '')::timestamptz, now()),
                    NEW.enabled, NEW.operational_state)
            ON CONFLICT DO NOTHING;
            RETURN NEW;
        END; $$;

CREATE FUNCTION ingest.reject_identity_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_TABLE_NAME = 'payload' AND to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
                RAISE EXCEPTION 'ingest payload rows are immutable';
            END IF;
            IF TG_TABLE_NAME = 'payload' AND (
                to_jsonb(NEW)->'id' IS DISTINCT FROM to_jsonb(OLD)->'id' OR
                to_jsonb(NEW)->'run_id' IS DISTINCT FROM to_jsonb(OLD)->'run_id' OR
                to_jsonb(NEW)->'archive_uri' IS DISTINCT FROM to_jsonb(OLD)->'archive_uri' OR
                to_jsonb(NEW)->'sha256' IS DISTINCT FROM to_jsonb(OLD)->'sha256' OR
                to_jsonb(NEW)->'encoding' IS DISTINCT FROM to_jsonb(OLD)->'encoding' OR
                to_jsonb(NEW)->'byte_count' IS DISTINCT FROM to_jsonb(OLD)->'byte_count' OR
                to_jsonb(NEW)->'schema_version' IS DISTINCT FROM to_jsonb(OLD)->'schema_version') THEN
                RAISE EXCEPTION 'ingest payload identity is immutable';
            END IF;
            IF TG_TABLE_NAME = 'run' AND (
                to_jsonb(NEW)->'id' IS DISTINCT FROM to_jsonb(OLD)->'id' OR
                to_jsonb(NEW)->'source_id' IS DISTINCT FROM to_jsonb(OLD)->'source_id' OR
                to_jsonb(NEW)->'source_run_key' IS DISTINCT FROM to_jsonb(OLD)->'source_run_key' OR
                to_jsonb(NEW)->'capability' IS DISTINCT FROM to_jsonb(OLD)->'capability') THEN
                RAISE EXCEPTION 'ingest run identity is immutable';
            END IF;
            RETURN NEW;
        END; $$;
