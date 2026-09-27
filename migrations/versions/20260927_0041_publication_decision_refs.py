"""Reference immutable decision payloads from decision-backed publication rows."""

from alembic import op

revision = "20260927_0041"
down_revision = "20260926_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE app.publication_bundle_item
          ADD COLUMN decision_payload_hash text
            REFERENCES analysis.decision_input_payload(content_hash) ON DELETE RESTRICT,
          ALTER COLUMN content_hash DROP NOT NULL,
          ADD CONSTRAINT publication_bundle_item_one_payload CHECK
            ((content_hash IS NULL) <> (decision_payload_hash IS NULL));
        ALTER TABLE app.current_publication_item
          ADD COLUMN decision_payload_hash text
            REFERENCES analysis.decision_input_payload(content_hash) ON DELETE RESTRICT,
          ALTER COLUMN content_hash DROP NOT NULL,
          ADD CONSTRAINT current_publication_item_one_payload CHECK
            ((content_hash IS NULL) <> (decision_payload_hash IS NULL));
        CREATE VIEW app.publication_bundle_item_read AS
          SELECT item.*, COALESCE(payload.payload, evidence.payload) AS payload
          FROM app.publication_bundle_item item
          LEFT JOIN app.publication_payload payload ON payload.content_hash = item.content_hash
          LEFT JOIN analysis.decision_input_payload evidence
            ON evidence.content_hash = item.decision_payload_hash;
        CREATE VIEW app.current_publication_item_read AS
          SELECT item.*, COALESCE(payload.payload, evidence.payload) AS payload
          FROM app.current_publication_item item
          LEFT JOIN app.publication_payload payload ON payload.content_hash = item.content_hash
          LEFT JOIN analysis.decision_input_payload evidence
            ON evidence.content_hash = item.decision_payload_hash;
        GRANT SELECT ON app.publication_bundle_item_read,
          app.current_publication_item_read TO market_app;
        CREATE OR REPLACE VIEW app.publication_content_item AS
          SELECT item.publication_id, item.model_name, item.stable_key,
                 item.rank, item.instrument_id, item.payload
          FROM app.publication_item item
          JOIN app.publication publication ON publication.id = item.publication_id
          WHERE publication.bundle_id IS NULL
          UNION ALL
          SELECT publication.id, item.model_name, item.stable_key,
                 item.rank, item.instrument_id, item.payload
          FROM app.publication publication
          JOIN app.publication_bundle_item_read item ON item.bundle_id = publication.bundle_id;
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION analysis.gc_archived_decision_payloads(p_hashes text[])
        RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE removed integer := 0;
        BEGIN
          IF cardinality(p_hashes) > 1000 OR EXISTS (
            SELECT 1 FROM unnest(p_hashes) digest WHERE digest !~ '^[0-9a-f]{64}$'
          ) THEN RAISE EXCEPTION 'invalid or oversized decision payload GC batch'; END IF;
          PERFORM pg_advisory_xact_lock(hashtextextended('market-decision-evidence-gc', 0));
          LOCK TABLE analysis.ticker_decision, app.publication_bundle_item,
            app.current_publication_item IN SHARE MODE;
          PERFORM set_config('market.verified_decision_payload_gc', 'on', true);
          WITH live AS MATERIALIZED (
            SELECT DISTINCT ref.digest
            FROM analysis.ticker_decision decision
            CROSS JOIN LATERAL (
              SELECT value AS digest FROM jsonb_each_text(decision.input_payload_refs)
              UNION ALL SELECT decision.evidence_refs->>'plan_impact'
              UNION ALL SELECT decision.evidence_refs->>'resolution_impact'
              UNION ALL SELECT value FROM jsonb_each_text(
                COALESCE(decision.evidence_refs->'manifest', '{}'::jsonb))
              UNION ALL SELECT value FROM jsonb_each_text(
                COALESCE(decision.evidence_refs->'portfolio_impacts', '{}'::jsonb))
            ) ref WHERE ref.digest = ANY(p_hashes)
          ), archived AS MATERIALIZED (
            SELECT ref.value AS digest,
                   bool_and(decision.evidence_state = 'archived'
                     AND manifest.verification_status = 'verified') AS covered
            FROM analysis.ticker_decision decision
            CROSS JOIN LATERAL jsonb_each_text(decision.archived_input_refs) ref
            LEFT JOIN ops.storage_archive_manifest manifest
              ON manifest.id = decision.evidence_archive_manifest_id
            WHERE ref.value = ANY(p_hashes) GROUP BY ref.value
          )
          DELETE FROM analysis.decision_input_payload payload
          WHERE payload.content_hash = ANY(p_hashes)
            AND (
              EXISTS (SELECT 1 FROM archived WHERE archived.digest = payload.content_hash
                      AND archived.covered)
              OR EXISTS (
                SELECT 1 FROM ops.storage_archive_manifest manifest
                CROSS JOIN LATERAL jsonb_array_elements_text(
                  COALESCE(manifest.metadata->'source_row_ids', '[]'::jsonb)) archived_id
                WHERE manifest.source_relation = 'analysis.decision_input_payload'
                  AND manifest.verification_status = 'verified'
                  AND archived_id.value = payload.content_hash
              )
            )
            AND NOT EXISTS (SELECT 1 FROM live WHERE live.digest = payload.content_hash)
            AND NOT EXISTS (SELECT 1 FROM app.publication_bundle_item item
                            WHERE item.decision_payload_hash = payload.content_hash)
            AND NOT EXISTS (SELECT 1 FROM app.current_publication_item item
                            WHERE item.decision_payload_hash = payload.content_hash);
          GET DIAGNOSTICS removed = ROW_COUNT;
          RETURN removed;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        INSERT INTO app.publication_payload(content_hash, payload)
        SELECT DISTINCT evidence.content_hash, evidence.payload
        FROM analysis.decision_input_payload evidence
        WHERE EXISTS (SELECT 1 FROM app.publication_bundle_item item
                      WHERE item.decision_payload_hash = evidence.content_hash)
        ON CONFLICT (content_hash) DO NOTHING;
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM app.publication_bundle_item item
            JOIN analysis.decision_input_payload evidence
              ON evidence.content_hash = item.decision_payload_hash
            JOIN app.publication_payload payload
              ON payload.content_hash = evidence.content_hash
            WHERE payload.payload IS DISTINCT FROM evidence.payload
          ) THEN RAISE EXCEPTION 'publication downgrade payload conflict'; END IF;
        END $$;
        UPDATE app.publication_bundle_item
          SET content_hash = decision_payload_hash, decision_payload_hash = NULL
          WHERE decision_payload_hash IS NOT NULL;
        UPDATE app.current_publication_item
          SET content_hash = decision_payload_hash, decision_payload_hash = NULL
          WHERE decision_payload_hash IS NOT NULL;
        CREATE OR REPLACE VIEW app.publication_content_item AS
          SELECT item.publication_id, item.model_name, item.stable_key,
                 item.rank, item.instrument_id, item.payload
          FROM app.publication_item item
          JOIN app.publication publication ON publication.id = item.publication_id
          WHERE publication.bundle_id IS NULL
          UNION ALL
          SELECT publication.id, item.model_name, item.stable_key,
                 item.rank, item.instrument_id, payload.payload
          FROM app.publication publication
          JOIN app.publication_bundle_item item ON item.bundle_id = publication.bundle_id
          JOIN app.publication_payload payload ON payload.content_hash = item.content_hash;
        DROP VIEW app.current_publication_item_read;
        DROP VIEW app.publication_bundle_item_read;
        ALTER TABLE app.current_publication_item
          DROP CONSTRAINT current_publication_item_one_payload,
          DROP COLUMN decision_payload_hash,
          ALTER COLUMN content_hash SET NOT NULL;
        ALTER TABLE app.publication_bundle_item
          DROP CONSTRAINT publication_bundle_item_one_payload,
          DROP COLUMN decision_payload_hash,
          ALTER COLUMN content_hash SET NOT NULL;
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION analysis.gc_archived_decision_payloads(p_hashes text[])
        RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE removed integer := 0;
        BEGIN
          IF cardinality(p_hashes) > 1000 OR EXISTS (
            SELECT 1 FROM unnest(p_hashes) digest WHERE digest !~ '^[0-9a-f]{64}$'
          ) THEN RAISE EXCEPTION 'invalid or oversized decision payload GC batch'; END IF;
          PERFORM pg_advisory_xact_lock(hashtextextended('market-decision-evidence-gc', 0));
          LOCK TABLE analysis.ticker_decision IN SHARE MODE;
          PERFORM set_config('market.verified_decision_payload_gc', 'on', true);
          WITH live AS MATERIALIZED (
            SELECT DISTINCT ref.digest
            FROM analysis.ticker_decision decision
            CROSS JOIN LATERAL (
              SELECT value AS digest FROM jsonb_each_text(decision.input_payload_refs)
              UNION ALL SELECT decision.evidence_refs->>'plan_impact'
              UNION ALL SELECT decision.evidence_refs->>'resolution_impact'
              UNION ALL SELECT value FROM jsonb_each_text(
                COALESCE(decision.evidence_refs->'manifest', '{}'::jsonb))
              UNION ALL SELECT value FROM jsonb_each_text(
                COALESCE(decision.evidence_refs->'portfolio_impacts', '{}'::jsonb))
            ) ref WHERE ref.digest = ANY(p_hashes)
          ), archived AS MATERIALIZED (
            SELECT ref.value AS digest,
                   bool_and(decision.evidence_state = 'archived'
                     AND manifest.verification_status = 'verified') AS covered
            FROM analysis.ticker_decision decision
            CROSS JOIN LATERAL jsonb_each_text(decision.archived_input_refs) ref
            LEFT JOIN ops.storage_archive_manifest manifest
              ON manifest.id = decision.evidence_archive_manifest_id
            WHERE ref.value = ANY(p_hashes) GROUP BY ref.value
          )
          DELETE FROM analysis.decision_input_payload payload
          USING archived
          WHERE payload.content_hash = archived.digest AND archived.covered
            AND NOT EXISTS (SELECT 1 FROM live WHERE live.digest = archived.digest);
          GET DIAGNOSTICS removed = ROW_COUNT;
          RETURN removed;
        END $$;
    """)
