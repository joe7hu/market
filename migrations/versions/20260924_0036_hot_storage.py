"""Lossless input-group sharing without rewriting existing decision history."""
from alembic import op

revision = "20260924_0036"
down_revision = "20260923_0035"
branch_labels = None
depends_on = None

_OLD_COLUMNS = """
    d.id, d.instrument_id, d.decision_revision, d.contract_version, d.as_of,
    d.published_at, d.input_hash, d.code_version, d.experiment_id,
    d.tactical, d.fundamental, d.capital_action, d.risk_policy, d.expressions,
    d.selected_expression, d.data_requests, d.learning_history, d.input_manifest,
    d.status, d.created_at, d.resolution, d.policy_version,
    d.opportunity_episode_id, d.opportunity_cutoff, d.opportunity_episode,
    d.market_state_publication_id,
    CASE WHEN d.market_state_context_hash IS NULL THEN d.market_state_snapshot
         ELSE (SELECT context.payload FROM analysis.decision_context context
               WHERE context.content_hash = d.market_state_context_hash)
    END AS market_state_snapshot,
    d.portfolio_impacts,
    CASE WHEN d.risk_policy_context_hash IS NULL THEN d.risk_policy_snapshot
         ELSE (SELECT context.payload FROM analysis.decision_context context
               WHERE context.content_hash = d.risk_policy_context_hash)
    END AS risk_policy_snapshot,
    d.market_state_context_hash, d.risk_policy_context_hash
"""
_COLUMNS = """
    d.id, d.instrument_id, d.decision_revision, d.contract_version, d.as_of,
    d.published_at, d.input_hash, d.code_version, d.experiment_id,
    d.tactical, d.fundamental, d.capital_action, d.risk_policy, d.expressions,
    d.selected_expression, d.data_requests, d.learning_history, analysis.expand_decision_inputs(d.input_manifest, d.input_payload_refs) AS input_manifest,
    d.status, d.created_at, d.resolution, d.policy_version,
    d.opportunity_episode_id, d.opportunity_cutoff, d.opportunity_episode,
    d.market_state_publication_id,
    CASE WHEN d.market_state_context_hash IS NULL THEN d.market_state_snapshot
         ELSE (SELECT context.payload FROM analysis.decision_context context
               WHERE context.content_hash = d.market_state_context_hash)
    END AS market_state_snapshot,
    d.portfolio_impacts,
    CASE WHEN d.risk_policy_context_hash IS NULL THEN d.risk_policy_snapshot
         ELSE (SELECT context.payload FROM analysis.decision_context context
               WHERE context.content_hash = d.risk_policy_context_hash)
    END AS risk_policy_snapshot,
    d.market_state_context_hash, d.risk_policy_context_hash
"""


def upgrade() -> None:
    op.execute("""
        CREATE TABLE analysis.decision_input_payload (
            content_hash text PRIMARY KEY,
            payload jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (content_hash = encode(sha256(convert_to(payload::text, 'UTF8')), 'hex'))
        );
        GRANT SELECT, INSERT ON analysis.decision_input_payload TO market_app;
        CREATE TRIGGER decision_input_payload_immutable
          BEFORE UPDATE OR DELETE ON analysis.decision_input_payload
          FOR EACH ROW EXECUTE FUNCTION analysis.reject_decision_context_mutation();
        ALTER TABLE analysis.ticker_decision
          ADD COLUMN input_payload_refs jsonb NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN inputs_normalized boolean NOT NULL DEFAULT false;
        CREATE INDEX ticker_decision_inputs_pending_idx ON analysis.ticker_decision(id)
          WHERE NOT inputs_normalized;

        CREATE FUNCTION analysis.expand_decision_inputs(manifest jsonb, refs jsonb)
        RETURNS jsonb LANGUAGE plpgsql STABLE AS $$
        DECLARE expanded jsonb;
        BEGIN
            IF refs = '{}'::jsonb THEN RETURN manifest; END IF;
            IF EXISTS (
                SELECT 1 FROM jsonb_each_text(refs) r
                LEFT JOIN analysis.decision_input_payload p ON p.content_hash = r.value
                WHERE p.content_hash IS NULL
            ) THEN RAISE EXCEPTION 'missing immutable decision input payload'; END IF;
            SELECT jsonb_object_agg(r.key, p.payload) INTO expanded
              FROM jsonb_each_text(refs) r
              JOIN analysis.decision_input_payload p ON p.content_hash = r.value;
            RETURN jsonb_set(manifest, '{inputs}', (manifest->'inputs') || expanded);
        END $$;

        CREATE FUNCTION analysis.check_decision_input_refs()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF jsonb_typeof(NEW.input_payload_refs) <> 'object' THEN
                RAISE EXCEPTION 'input payload references must be an object';
            END IF;
            IF NEW.input_payload_refs <> '{}'::jsonb THEN
                IF jsonb_typeof(NEW.input_manifest->'inputs') IS DISTINCT FROM 'object' THEN
                    RAISE EXCEPTION 'referenced input manifest requires an inputs object';
                END IF;
                IF EXISTS (SELECT 1 FROM jsonb_each_text(NEW.input_payload_refs) r
                           WHERE NEW.input_manifest->'inputs' ? r.key) THEN
                    RAISE EXCEPTION 'input cannot be both inline and referenced';
                END IF;
                PERFORM analysis.expand_decision_inputs(NEW.input_manifest, NEW.input_payload_refs);
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER ticker_decision_input_refs_valid
          BEFORE INSERT OR UPDATE OF input_manifest, input_payload_refs ON analysis.ticker_decision
          FOR EACH ROW EXECUTE FUNCTION analysis.check_decision_input_refs();

        CREATE FUNCTION analysis.intern_decision_inputs(manifest jsonb)
        RETURNS TABLE(compact_manifest jsonb, input_refs jsonb)
        LANGUAGE plpgsql AS $$
        DECLARE item record; digest text; stored jsonb;
        BEGIN
            compact_manifest := manifest;
            input_refs := '{}'::jsonb;
            IF jsonb_typeof(manifest->'inputs') IS DISTINCT FROM 'object' THEN
                RETURN NEXT; RETURN;
            END IF;
            FOR item IN SELECT key, value FROM jsonb_each(manifest->'inputs') ORDER BY key LOOP
                IF octet_length(item.value::text) < 1024 THEN CONTINUE; END IF;
                digest := encode(sha256(convert_to(item.value::text, 'UTF8')), 'hex');
                INSERT INTO analysis.decision_input_payload(content_hash, payload)
                    VALUES (digest, item.value) ON CONFLICT (content_hash) DO NOTHING;
                SELECT p.payload INTO stored FROM analysis.decision_input_payload p
                    WHERE p.content_hash = digest;
                IF stored IS DISTINCT FROM item.value THEN
                    RAISE EXCEPTION 'decision input hash collision or corruption';
                END IF;
                compact_manifest := compact_manifest #- ARRAY['inputs', item.key];
                input_refs := input_refs || jsonb_build_object(item.key, digest);
            END LOOP;
            IF analysis.expand_decision_inputs(compact_manifest, input_refs) IS DISTINCT FROM manifest THEN
                RAISE EXCEPTION 'decision input normalization changed original JSON';
            END IF;
            RETURN NEXT;
        END $$;
        REVOKE ALL ON FUNCTION analysis.intern_decision_inputs(jsonb) FROM PUBLIC;
        REVOKE ALL ON FUNCTION analysis.expand_decision_inputs(jsonb, jsonb) FROM PUBLIC;
        REVOKE ALL ON FUNCTION analysis.check_decision_input_refs() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION analysis.intern_decision_inputs(jsonb) TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.expand_decision_inputs(jsonb, jsonb) TO market_app;
    """)
    op.execute(f"CREATE OR REPLACE VIEW analysis.ticker_decision_read AS SELECT {_COLUMNS} FROM analysis.ticker_decision d")


def downgrade() -> None:
    if op.get_bind().exec_driver_sql("""
        SELECT EXISTS (SELECT 1 FROM analysis.ticker_decision WHERE input_payload_refs <> '{}'::jsonb)
    """).scalar():
        raise RuntimeError("restore inline decision inputs before downgrading hot storage")
    op.execute(f"CREATE OR REPLACE VIEW analysis.ticker_decision_read AS SELECT {_OLD_COLUMNS} FROM analysis.ticker_decision d")
    op.execute("""
        DROP TRIGGER ticker_decision_input_refs_valid ON analysis.ticker_decision;
        DROP FUNCTION analysis.check_decision_input_refs();
        DROP FUNCTION analysis.intern_decision_inputs(jsonb);
        DROP FUNCTION analysis.expand_decision_inputs(jsonb, jsonb);
        ALTER TABLE analysis.ticker_decision DROP COLUMN input_payload_refs, DROP COLUMN inputs_normalized;
        DROP TABLE analysis.decision_input_payload;
    """)
