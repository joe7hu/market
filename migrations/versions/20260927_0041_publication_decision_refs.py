"""Reference immutable decision payloads from decision-backed publication rows."""

from alembic import op

revision = "20260927_0041"
down_revision = "20260926_0040"
branch_labels = None
depends_on = None

_OPTION_SUBSETS = {
    "option_snapshot": ("snapshot_time", "ticker", "underlying_price", "expiration", "strike",
                        "option_type", "bid", "ask", "mid", "volume", "open_interest", "iv",
                        "delta", "dte", "spread_pct", "data_source", "contract_id", "raw"),
    "option_features": ("snapshot_time", "contract_id", "ticker", "required_2x_price",
                        "required_5x_price", "required_10x_price", "required_move_pct",
                        "liquidity_score", "convexity_score", "raw"),
}


def upgrade() -> None:
    op.execute("""
        ALTER TABLE app.publication_bundle ADD COLUMN projection_version text
          CHECK (projection_version IS NULL OR projection_version = 'option-subsets-v1');
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
    projected = " UNION ALL ".join(
        "SELECT candidate.bundle_id, '" + model + "'::text AS model_name, "
        "candidate.contract_id AS stable_key, candidate.first_rank AS rank, "
        "NULL::bigint AS instrument_id, NULL::character(64) AS content_hash, "
        "NULL::uuid AS canonical_publication_id, NULL::text AS decision_payload_hash, "
        "jsonb_build_object(" + ", ".join(
            "'" + key + "', candidate.payload->'" + key + "'" for key in keys
        ) + ") AS payload FROM candidate WHERE candidate.last_rank = 1"
        for model, keys in _OPTION_SUBSETS.items()
    )
    op.execute("""
        CREATE VIEW app.option_publication_projection AS
        WITH candidate AS (
          SELECT item.bundle_id, item.payload->>'contract_id' AS contract_id,
                 item.payload, min(item.rank) OVER (
                   PARTITION BY item.bundle_id, item.payload->>'contract_id') AS first_rank,
                 row_number() OVER (
                   PARTITION BY item.bundle_id, item.payload->>'contract_id'
                   ORDER BY item.rank DESC) AS last_rank
          FROM app.publication_bundle_item_read item
          JOIN app.publication_bundle bundle ON bundle.id = item.bundle_id
          WHERE bundle.scope = 'options-radar'
            AND bundle.projection_version = 'option-subsets-v1'
            AND item.model_name = 'candidate_event'
            AND item.payload->>'contract_id' IS NOT NULL
        )
        """ + projected + ";")
    op.execute("""
        CREATE OR REPLACE VIEW app.current_publication_item_read AS
          SELECT item.*, COALESCE(payload.payload, evidence.payload) AS payload
          FROM app.current_publication_item item
          LEFT JOIN app.publication_payload payload ON payload.content_hash = item.content_hash
          LEFT JOIN analysis.decision_input_payload evidence
            ON evidence.content_hash = item.decision_payload_hash
          UNION ALL
          SELECT current_item.scope, current_item.publication_id,
                 projected.model_name, projected.stable_key, projected.rank,
                 projected.instrument_id, projected.content_hash,
                 projected.decision_payload_hash, projected.payload
          FROM app.option_publication_projection projected
          JOIN app.publication publication ON publication.bundle_id = projected.bundle_id
          JOIN (SELECT DISTINCT scope, publication_id
                FROM app.current_publication_item WHERE model_name = 'candidate_event') current_item
            ON current_item.publication_id = publication.id
          ;
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
          JOIN app.publication_bundle_item_read item ON item.bundle_id = publication.bundle_id
          UNION ALL
          SELECT publication.id, projected.model_name, projected.stable_key,
                 projected.rank, projected.instrument_id, projected.payload
          FROM app.publication publication
          JOIN app.option_publication_projection projected
            ON projected.bundle_id = publication.bundle_id;
        GRANT SELECT ON app.option_publication_projection TO market_app;
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
    op.execute("""
        CREATE FUNCTION app.rehome_ranking_publication_payload(
          p_bundle uuid, p_model text, p_key text)
        RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE old_hash text; original jsonb; digest text; after_text text;
                current_text text; removed integer;
        BEGIN
          IF p_model NOT IN ('instrument_state_snapshot', 'alpha_signal',
                             'opportunity_rank', 'trade_plan') THEN
            RAISE EXCEPTION 'unsupported ranking model';
          END IF;
          PERFORM pg_advisory_xact_lock(hashtextextended('publication:ticker-opportunity-ranking', 0));
          PERFORM pg_advisory_xact_lock_shared(hashtextextended('market-decision-evidence-gc', 0));
          LOCK TABLE app.publication_bundle_item, app.current_publication_item
            IN SHARE ROW EXCLUSIVE MODE;
          SELECT item.content_hash, payload.payload INTO old_hash, original
          FROM app.publication_bundle_item item
          JOIN app.publication_bundle bundle ON bundle.id = item.bundle_id
          JOIN app.publication_payload payload ON payload.content_hash = item.content_hash
          WHERE item.bundle_id = p_bundle AND item.model_name = p_model
            AND item.stable_key = p_key AND bundle.scope = 'ticker-opportunity-ranking'
          FOR UPDATE OF item;
          IF NOT FOUND THEN RAISE EXCEPTION 'legacy ranking payload is missing'; END IF;
          digest := analysis.intern_decision_payload(original);
          UPDATE app.publication_bundle_item SET content_hash = NULL,
            decision_payload_hash = digest WHERE bundle_id = p_bundle
            AND model_name = p_model AND stable_key = p_key;
          UPDATE app.current_publication_item current_item
          SET content_hash = NULL, decision_payload_hash = digest
          WHERE current_item.model_name = p_model AND current_item.stable_key = p_key
            AND current_item.content_hash = old_hash
            AND EXISTS (SELECT 1 FROM app.publication publication
                        WHERE publication.id = current_item.publication_id
                          AND publication.bundle_id = p_bundle);
          SELECT payload::text INTO after_text FROM app.publication_bundle_item_read
          WHERE bundle_id = p_bundle AND model_name = p_model AND stable_key = p_key;
          IF after_text IS DISTINCT FROM original::text THEN
            RAISE EXCEPTION 'ranking publication read changed';
          END IF;
          SELECT current_item.payload::text INTO current_text
          FROM app.current_publication_item_read current_item
          JOIN app.publication publication ON publication.id = current_item.publication_id
          WHERE publication.bundle_id = p_bundle AND current_item.model_name = p_model
            AND current_item.stable_key = p_key;
          IF FOUND AND current_text IS DISTINCT FROM original::text THEN
            RAISE EXCEPTION 'current ranking read changed';
          END IF;
          DELETE FROM app.publication_payload payload WHERE payload.content_hash = old_hash
            AND NOT EXISTS (SELECT 1 FROM app.publication_bundle_item item
                            WHERE item.content_hash = payload.content_hash)
            AND NOT EXISTS (SELECT 1 FROM app.current_publication_item item
                            WHERE item.content_hash = payload.content_hash);
          GET DIAGNOSTICS removed = ROW_COUNT;
          RETURN removed;
        END $$;
        REVOKE ALL ON FUNCTION app.rehome_ranking_publication_payload(uuid, text, text)
          FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION app.rehome_ranking_publication_payload(uuid, text, text)
          TO market_app;
    """)


def downgrade() -> None:
    op.execute("""
        CREATE TEMP TABLE option_projection_restore ON COMMIT DROP AS
          SELECT projected.*, encode(digest(projected.payload::text, 'sha256'), 'hex') AS hash
          FROM app.option_publication_projection projected;
        DO $$ BEGIN
          IF EXISTS (SELECT hash FROM option_projection_restore
                     GROUP BY hash HAVING count(DISTINCT payload) > 1) THEN
            RAISE EXCEPTION 'option projection hash conflict';
          END IF;
        END $$;
        INSERT INTO app.publication_payload(content_hash, payload)
          SELECT DISTINCT ON (hash) hash, payload FROM option_projection_restore
          ORDER BY hash ON CONFLICT (content_hash) DO NOTHING;
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM option_projection_restore projected
                     JOIN app.publication_payload payload ON payload.content_hash = projected.hash
                     WHERE payload.payload IS DISTINCT FROM projected.payload) THEN
            RAISE EXCEPTION 'option projection payload conflict';
          END IF;
        END $$;
        INSERT INTO app.publication_bundle_item
          (bundle_id, model_name, stable_key, rank, instrument_id, content_hash)
          SELECT bundle_id, model_name, stable_key, rank, instrument_id, hash
          FROM option_projection_restore;
        INSERT INTO app.current_publication_item
          (scope, publication_id, model_name, stable_key, rank, instrument_id, content_hash)
          SELECT DISTINCT current_item.scope, current_item.publication_id,
                 projected.model_name, projected.stable_key, projected.rank,
                 projected.instrument_id, projected.hash
          FROM option_projection_restore projected
          JOIN app.publication publication ON publication.bundle_id = projected.bundle_id
          JOIN app.current_publication_item current_item
            ON current_item.publication_id = publication.id
           AND current_item.model_name = 'candidate_event';
        UPDATE app.publication_bundle bundle SET item_count = (
          SELECT count(*) FROM app.publication_bundle_item item WHERE item.bundle_id = bundle.id)
        WHERE bundle.projection_version = 'option-subsets-v1';
    """)
    op.execute("DROP FUNCTION app.rehome_ranking_publication_payload(uuid, text, text)")
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
        DROP VIEW app.option_publication_projection;
        DROP VIEW app.publication_bundle_item_read;
        ALTER TABLE app.current_publication_item
          DROP CONSTRAINT current_publication_item_one_payload,
          DROP COLUMN decision_payload_hash,
          ALTER COLUMN content_hash SET NOT NULL;
        ALTER TABLE app.publication_bundle_item
          DROP CONSTRAINT publication_bundle_item_one_payload,
          DROP COLUMN decision_payload_hash,
          ALTER COLUMN content_hash SET NOT NULL;
        ALTER TABLE app.publication_bundle DROP COLUMN projection_version;
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
