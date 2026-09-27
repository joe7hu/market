"""Intern decision evidence components and record unchanged checks."""

import re
from alembic import op

revision = "20260926_0040"
down_revision = "20260924_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE analysis.ticker_decision
          ADD COLUMN evidence_refs jsonb NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN evidence_normalized boolean NOT NULL DEFAULT false,
          ADD COLUMN semantic_fingerprint text,
          ADD COLUMN last_evaluated_at timestamptz,
          ADD COLUMN evidence_state text NOT NULL DEFAULT 'local'
            CHECK (evidence_state IN ('local', 'archived', 'unavailable')),
          ADD COLUMN evidence_archive_manifest_id bigint
            REFERENCES ops.storage_archive_manifest(id) ON DELETE RESTRICT;
        CREATE INDEX ticker_decision_evidence_pending_idx ON analysis.ticker_decision(id)
          WHERE NOT evidence_normalized;
        ALTER TABLE analysis.ticker_decision
          DROP CONSTRAINT ticker_decision_instrument_id_decision_revision_key;
        CREATE UNIQUE INDEX ticker_decision_semantic_revision_key
          ON analysis.ticker_decision
          (instrument_id, decision_revision, semantic_fingerprint, published_at)
          NULLS NOT DISTINCT;
        CREATE TABLE analysis.ticker_decision_checkpoint (
          ticker_decision_id uuid NOT NULL REFERENCES analysis.ticker_decision(id) ON DELETE RESTRICT,
          check_day date NOT NULL,
          first_checked_at timestamptz NOT NULL,
          last_checked_at timestamptz NOT NULL,
          evaluation_count bigint NOT NULL CHECK (evaluation_count > 0),
          health_state text NOT NULL,
          PRIMARY KEY (ticker_decision_id, check_day),
          CHECK (first_checked_at <= last_checked_at)
        );
        GRANT SELECT, INSERT, UPDATE ON analysis.ticker_decision_checkpoint TO market_app;

        CREATE TABLE ops.storage_daily_accounting (
          sample_day date PRIMARY KEY,
          sampled_at timestamptz NOT NULL,
          database_bytes bigint NOT NULL CHECK (database_bytes >= 0),
          volume_free_bytes bigint NOT NULL CHECK (volume_free_bytes >= 0),
          logical_evidence_bytes bigint NOT NULL CHECK (logical_evidence_bytes >= 0),
          archived_bytes bigint NOT NULL CHECK (archived_bytes >= 0),
          archive_rows bigint NOT NULL CHECK (archive_rows >= 0)
        );
        GRANT SELECT, INSERT, UPDATE ON ops.storage_daily_accounting TO market_app;
        CREATE FUNCTION ops.postgres_data_directory()
        RETURNS text LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog
        AS 'SELECT current_setting(''data_directory'')';
        REVOKE ALL ON FUNCTION ops.postgres_data_directory() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION ops.postgres_data_directory() TO market_app;

        ALTER TABLE analysis.option_decision
          ADD COLUMN evidence_state text NOT NULL DEFAULT 'local'
            CHECK (evidence_state IN ('local', 'archived', 'unavailable')),
          ADD COLUMN evidence_archive_manifest_id bigint
            REFERENCES ops.storage_archive_manifest(id) ON DELETE RESTRICT;
        ALTER TABLE analysis.decision_evidence
          ADD COLUMN evidence_state text NOT NULL DEFAULT 'local'
            CHECK (evidence_state IN ('local', 'archived', 'unavailable')),
          ADD COLUMN evidence_archive_manifest_id bigint
            REFERENCES ops.storage_archive_manifest(id) ON DELETE RESTRICT;

        CREATE FUNCTION analysis.reject_archived_evidence_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.evidence_state = 'archived' THEN
            RAISE EXCEPTION 'archived option evidence is immutable';
          END IF;
          IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER archived_option_decision_immutable
          BEFORE UPDATE OR DELETE ON analysis.option_decision
          FOR EACH ROW EXECUTE FUNCTION analysis.reject_archived_evidence_mutation();
        CREATE TRIGGER archived_decision_evidence_immutable
          BEFORE UPDATE OR DELETE ON analysis.decision_evidence
          FOR EACH ROW EXECUTE FUNCTION analysis.reject_archived_evidence_mutation();

        CREATE FUNCTION analysis.require_local_primary_option_scan()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE state text;
        BEGIN
          IF NEW.primary_decision_id IS NULL THEN RETURN NEW; END IF;
          SELECT evidence_state INTO state FROM analysis.option_decision
            WHERE decision_id = NEW.primary_decision_id FOR KEY SHARE;
          IF state IS NOT NULL AND state <> 'local' THEN
            RAISE EXCEPTION 'linked option decision requires local primary scan evidence';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER option_primary_scan_evidence_local
          BEFORE INSERT OR UPDATE OF primary_decision_id ON analysis.option_decision
          FOR EACH ROW EXECUTE FUNCTION analysis.require_local_primary_option_scan();

        ALTER TABLE app.publication_bundle_item
          ADD COLUMN canonical_publication_id uuid
            REFERENCES app.publication(id) ON DELETE RESTRICT;

        CREATE FUNCTION analysis.require_local_option_evidence()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE state text;
        BEGIN
          IF NEW.decision_id IS NULL THEN RETURN NEW; END IF;
          IF TG_TABLE_NAME = 'paper_order' AND NEW.status IN
             ('closed', 'cancelled', 'rejected', 'exited', 'invalidated') THEN RETURN NEW; END IF;
          IF TG_TABLE_NAME = 'agent_task' AND NEW.status IN
             ('succeeded', 'completed', 'failed', 'cancelled') THEN RETURN NEW; END IF;
          IF TG_TABLE_NAME = 'shadow_trade' AND NEW.status IN
             ('closed', 'unfilled', 'unmeasurable', 'rejected', 'expired') THEN RETURN NEW; END IF;
          SELECT evidence_state INTO state FROM analysis.option_decision
            WHERE decision_id = NEW.decision_id FOR KEY SHARE;
          IF state IS NOT NULL AND state <> 'local' THEN
            RAISE EXCEPTION 'active option consumer requires local evidence';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER paper_order_option_evidence_local
          BEFORE INSERT OR UPDATE OF decision_id, status ON app.paper_order
          FOR EACH ROW EXECUTE FUNCTION analysis.require_local_option_evidence();
        CREATE TRIGGER agent_task_option_evidence_local
          BEFORE INSERT OR UPDATE OF decision_id, status ON analysis.agent_task
          FOR EACH ROW EXECUTE FUNCTION analysis.require_local_option_evidence();
        CREATE TRIGGER shadow_trade_option_evidence_local
          BEFORE INSERT OR UPDATE OF decision_id, status ON analysis.shadow_trade
          FOR EACH ROW EXECUTE FUNCTION analysis.require_local_option_evidence();
        CREATE FUNCTION analysis.require_local_option_publication()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.scope NOT IN ('options-radar', 'options-decision-system') OR NEW.status <> 'published' THEN RETURN NEW; END IF;
          PERFORM 1 FROM analysis.run WHERE id = NEW.analysis_run_id FOR KEY SHARE;
          IF EXISTS (
            SELECT 1 FROM analysis.decision decision
            JOIN analysis.option_decision scan ON scan.decision_id = decision.id
            WHERE decision.run_id = NEW.analysis_run_id AND scan.evidence_state <> 'local'
          ) THEN
            RAISE EXCEPTION 'option publication requires local scan evidence';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER option_publication_evidence_local
          BEFORE INSERT OR UPDATE OF status, scope, analysis_run_id ON app.publication
          FOR EACH ROW EXECUTE FUNCTION analysis.require_local_option_publication();

        CREATE FUNCTION analysis.intern_decision_payload(p_payload jsonb)
        RETURNS text LANGUAGE plpgsql AS $$
        DECLARE digest text; stored jsonb;
        BEGIN
          IF p_payload IS NULL THEN RAISE EXCEPTION 'null decision payload cannot be interned'; END IF;
          digest := encode(sha256(convert_to(p_payload::text, 'UTF8')), 'hex');
          INSERT INTO analysis.decision_input_payload(content_hash, payload)
            VALUES (digest, p_payload) ON CONFLICT (content_hash) DO NOTHING;
          SELECT payload INTO stored FROM analysis.decision_input_payload WHERE content_hash = digest;
          IF stored IS DISTINCT FROM p_payload THEN
            RAISE EXCEPTION 'decision payload hash collision or corruption';
          END IF;
          RETURN digest;
        END $$;

        CREATE FUNCTION analysis.decision_payload(p_hash text)
        RETURNS jsonb LANGUAGE plpgsql STABLE AS $$
        DECLARE value jsonb;
        BEGIN
          SELECT payload INTO value FROM analysis.decision_input_payload WHERE content_hash = p_hash;
          IF NOT FOUND THEN RAISE EXCEPTION 'missing immutable decision payload %', p_hash; END IF;
          RETURN value;
        END $$;

        CREATE FUNCTION analysis.expand_decision_manifest(p_manifest jsonb, p_refs jsonb, p_episode jsonb)
        RETURNS jsonb LANGUAGE plpgsql STABLE AS $$
        DECLARE item record; expanded jsonb := p_manifest; value jsonb;
        BEGIN
          FOR item IN SELECT r.key, r.value FROM jsonb_each_text(COALESCE(p_refs->'manifest', '{}'::jsonb)) r LOOP
            IF expanded ? item.key THEN RAISE EXCEPTION 'decision component inline and referenced: %', item.key; END IF;
            value := analysis.decision_payload(item.value);
            IF item.key = 'trade_plan' THEN
              IF p_refs ? 'plan_impact' THEN
                IF value ? 'portfolio_impact' THEN RAISE EXCEPTION 'plan impact inline and referenced'; END IF;
                value := value || jsonb_build_object('portfolio_impact', analysis.decision_payload(p_refs->>'plan_impact'));
              END IF;
              IF p_refs->>'plan_selected_episode' = 'true' THEN
                IF value ? 'selected_expression' OR NOT p_episode ? 'selected_expression' THEN
                  RAISE EXCEPTION 'plan selected expression reference invalid';
                END IF;
                value := value || jsonb_build_object('selected_expression', p_episode->'selected_expression');
              END IF;
            END IF;
            expanded := expanded || jsonb_build_object(item.key, value);
          END LOOP;
          RETURN expanded;
        END $$;

        CREATE FUNCTION analysis.expand_decision_resolution(
          p_resolution jsonb, p_refs jsonb, p_manifest jsonb, p_episode jsonb)
        RETURNS jsonb LANGUAGE plpgsql STABLE AS $$
        DECLARE expanded jsonb := p_resolution; component text; plan jsonb;
        BEGIN
          IF p_refs ? 'resolution_plan_fields' THEN
            plan := analysis.expand_decision_manifest(p_manifest, p_refs, p_episode)->'trade_plan';
            FOR component IN SELECT jsonb_array_elements_text(p_refs->'resolution_plan_fields') LOOP
              IF expanded ? component OR NOT plan ? component THEN
                RAISE EXCEPTION 'resolution plan field inline or missing: %', component;
              END IF;
              expanded := expanded || jsonb_build_object(component, plan->component);
            END LOOP;
          END IF;
          IF p_refs ? 'resolution_impact' THEN
            IF expanded ? 'portfolio_context' THEN RAISE EXCEPTION 'resolution impact inline and referenced'; END IF;
            expanded := expanded || jsonb_build_object('portfolio_context',
              analysis.decision_payload(p_refs->>'resolution_impact'));
          END IF;
          RETURN expanded;
        END $$;

        CREATE FUNCTION analysis.expand_decision_capital(
          p_capital jsonb, p_resolution jsonb, p_refs jsonb)
        RETURNS jsonb LANGUAGE plpgsql STABLE AS $$
        DECLARE expanded jsonb := p_capital; component text;
        BEGIN
          FOR component IN SELECT jsonb_array_elements_text(
            COALESCE(p_refs->'capital_resolution_fields', '[]'::jsonb)) LOOP
            IF expanded ? component OR NOT p_resolution ? component THEN
              RAISE EXCEPTION 'capital action field inline or missing: %', component;
            END IF;
            expanded := expanded || jsonb_build_object(component, p_resolution->component);
          END LOOP;
          RETURN expanded;
        END $$;

        CREATE FUNCTION analysis.expand_decision_impacts(p_impacts jsonb, p_refs jsonb)
        RETURNS jsonb LANGUAGE plpgsql STABLE AS $$
        DECLARE item record; expanded jsonb := p_impacts;
        BEGIN
          FOR item IN SELECT r.key, r.value FROM jsonb_each_text(COALESCE(p_refs->'portfolio_impacts', '{}'::jsonb)) r LOOP
            IF expanded ? item.key THEN RAISE EXCEPTION 'portfolio impact inline and referenced: %', item.key; END IF;
            expanded := expanded || jsonb_build_object(item.key, analysis.decision_payload(item.value));
          END LOOP;
          RETURN expanded;
        END $$;

        CREATE FUNCTION analysis.intern_decision_evidence(
          p_manifest jsonb, p_resolution jsonb, p_capital jsonb, p_expressions jsonb,
          p_selected jsonb, p_episode jsonb, p_impacts jsonb)
        RETURNS TABLE(compact_manifest jsonb, compact_resolution jsonb, compact_capital jsonb,
          compact_expressions jsonb, compact_selected jsonb,
          compact_impacts jsonb, refs jsonb) LANGUAGE plpgsql AS $$
        DECLARE component text; value jsonb; item record; manifest_refs jsonb := '{}'::jsonb;
                impact_refs jsonb := '{}'::jsonb;
        BEGIN
          compact_manifest := p_manifest;
          compact_resolution := p_resolution;
          compact_capital := p_capital;
          compact_expressions := p_expressions;
          compact_selected := p_selected;
          compact_impacts := p_impacts;
          refs := '{}'::jsonb;
          IF jsonb_typeof(p_manifest) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'decision manifest must be an object';
          END IF;
          FOREACH component IN ARRAY ARRAY['instrument_state_snapshot', 'alpha_signals',
            'opportunity_rank', 'trade_plan', 'reference_signal'] LOOP
            IF NOT compact_manifest ? component OR compact_manifest->component = 'null'::jsonb THEN
              CONTINUE;
            END IF;
            value := compact_manifest->component;
            IF component = 'trade_plan' AND jsonb_typeof(value) = 'object' THEN
              IF value ? 'portfolio_impact' THEN
                refs := refs || jsonb_build_object('plan_impact',
                  analysis.intern_decision_payload(value->'portfolio_impact'));
                value := value - 'portfolio_impact';
              END IF;
              IF value ? 'selected_expression'
                 AND p_episode ? 'selected_expression'
                 AND value->'selected_expression' = p_episode->'selected_expression' THEN
                value := value - 'selected_expression';
                refs := refs || jsonb_build_object('plan_selected_episode', true);
              END IF;
            END IF;
            manifest_refs := manifest_refs || jsonb_build_object(component,
              analysis.intern_decision_payload(value));
            compact_manifest := compact_manifest - component;
          END LOOP;
          IF manifest_refs <> '{}'::jsonb THEN
            refs := refs || jsonb_build_object('manifest', manifest_refs);
          END IF;
          IF p_resolution ? 'portfolio_context' THEN
            refs := refs || jsonb_build_object('resolution_impact',
              analysis.intern_decision_payload(p_resolution->'portfolio_context'));
            compact_resolution := p_resolution - 'portfolio_context';
          END IF;
          IF jsonb_typeof(p_manifest->'trade_plan') = 'object' THEN
            FOR component IN SELECT key FROM jsonb_object_keys(p_resolution) AS key LOOP
              IF component <> 'portfolio_context'
                 AND (p_manifest->'trade_plan') ? component
                 AND p_resolution->component = p_manifest->'trade_plan'->component THEN
                compact_resolution := compact_resolution - component;
                refs := refs || jsonb_build_object('resolution_plan_fields',
                  COALESCE(refs->'resolution_plan_fields', '[]'::jsonb) || jsonb_build_array(component));
              END IF;
            END LOOP;
          END IF;
          IF jsonb_typeof(p_capital) = 'object' THEN
            FOR component IN SELECT key FROM jsonb_object_keys(p_capital) AS key LOOP
              IF p_resolution ? component AND p_capital->component = p_resolution->component THEN
                compact_capital := compact_capital - component;
                refs := refs || jsonb_build_object('capital_resolution_fields',
                  COALESCE(refs->'capital_resolution_fields', '[]'::jsonb) || jsonb_build_array(component));
              END IF;
            END LOOP;
          END IF;
          IF jsonb_typeof(p_impacts) = 'object' THEN
            FOR item IN SELECT r.key, r.value FROM jsonb_each(p_impacts) r LOOP
              impact_refs := impact_refs || jsonb_build_object(item.key,
                analysis.intern_decision_payload(item.value));
              compact_impacts := compact_impacts - item.key;
            END LOOP;
          END IF;
          IF impact_refs <> '{}'::jsonb THEN
            refs := refs || jsonb_build_object('portfolio_impacts', impact_refs);
          END IF;
          IF p_episode ? 'expressions' AND p_expressions = p_episode->'expressions' THEN
            compact_expressions := '{}'::jsonb;
            refs := refs || jsonb_build_object('expressions_episode', true);
          END IF;
          IF p_episode ? 'selected_expression' AND p_selected = p_episode->'selected_expression' THEN
            compact_selected := NULL;
            refs := refs || jsonb_build_object('selected_episode', true);
          END IF;
          IF analysis.expand_decision_manifest(compact_manifest, refs, p_episode) IS DISTINCT FROM p_manifest
             OR analysis.expand_decision_resolution(compact_resolution, refs, compact_manifest, p_episode) IS DISTINCT FROM p_resolution
             OR analysis.expand_decision_capital(compact_capital, p_resolution, refs) IS DISTINCT FROM p_capital
             OR analysis.expand_decision_impacts(compact_impacts, refs) IS DISTINCT FROM p_impacts THEN
            RAISE EXCEPTION 'decision evidence normalization changed original values';
          END IF;
          RETURN NEXT;
        END $$;

        CREATE FUNCTION analysis.check_decision_evidence_refs()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF jsonb_typeof(NEW.evidence_refs) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'decision evidence references must be an object';
          END IF;
          PERFORM analysis.expand_decision_manifest(NEW.input_manifest, NEW.evidence_refs, NEW.opportunity_episode);
          PERFORM analysis.expand_decision_capital(NEW.capital_action,
            analysis.expand_decision_resolution(NEW.resolution, NEW.evidence_refs,
              NEW.input_manifest, NEW.opportunity_episode), NEW.evidence_refs);
          PERFORM analysis.expand_decision_impacts(NEW.portfolio_impacts, NEW.evidence_refs);
          IF NEW.evidence_refs->>'expressions_episode' = 'true'
             AND (NEW.expressions <> '{}'::jsonb OR NOT NEW.opportunity_episode ? 'expressions') THEN
            RAISE EXCEPTION 'expression inline and episode reference conflict';
          END IF;
          IF NEW.evidence_refs->>'selected_episode' = 'true'
             AND (NEW.selected_expression IS NOT NULL OR NOT NEW.opportunity_episode ? 'selected_expression') THEN
            RAISE EXCEPTION 'selected expression inline and episode reference conflict';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER ticker_decision_evidence_refs_valid
          BEFORE INSERT OR UPDATE OF input_manifest, resolution, capital_action, expressions,
            selected_expression, opportunity_episode, portfolio_impacts, evidence_refs
          ON analysis.ticker_decision FOR EACH ROW
          EXECUTE FUNCTION analysis.check_decision_evidence_refs();

        REVOKE ALL ON FUNCTION analysis.intern_decision_payload(jsonb) FROM PUBLIC;
        REVOKE ALL ON FUNCTION analysis.decision_payload(text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION analysis.intern_decision_evidence(jsonb,jsonb,jsonb,jsonb,jsonb,jsonb,jsonb) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION analysis.intern_decision_evidence(jsonb,jsonb,jsonb,jsonb,jsonb,jsonb,jsonb) TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.intern_decision_payload(jsonb) TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.decision_payload(text) TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.expand_decision_manifest(jsonb,jsonb,jsonb) TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.expand_decision_resolution(jsonb,jsonb,jsonb,jsonb) TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.expand_decision_capital(jsonb,jsonb,jsonb) TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.expand_decision_impacts(jsonb,jsonb) TO market_app;
    """)
    op.execute("""
        CREATE OR REPLACE VIEW analysis.ticker_decision_read AS
        SELECT d.id, d.instrument_id, d.decision_revision, d.contract_version, d.as_of,
          d.published_at, d.input_hash, d.code_version, d.experiment_id,
          d.tactical, d.fundamental,
          analysis.expand_decision_capital(d.capital_action,
            analysis.expand_decision_resolution(d.resolution, d.evidence_refs,
              d.input_manifest, d.opportunity_episode), d.evidence_refs) AS capital_action,
          d.risk_policy,
          CASE WHEN d.evidence_refs->>'expressions_episode' = 'true'
               THEN d.opportunity_episode->'expressions' ELSE d.expressions END AS expressions,
          CASE WHEN d.evidence_refs->>'selected_episode' = 'true'
               THEN d.opportunity_episode->'selected_expression' ELSE d.selected_expression END AS selected_expression,
          d.data_requests, d.learning_history,
          analysis.expand_decision_manifest(
            analysis.expand_decision_inputs(d.input_manifest, d.input_payload_refs),
            d.evidence_refs, d.opportunity_episode) AS input_manifest,
          d.status, d.created_at,
          analysis.expand_decision_resolution(d.resolution, d.evidence_refs,
            d.input_manifest, d.opportunity_episode) AS resolution,
          d.policy_version, d.opportunity_episode_id, d.opportunity_cutoff,
          d.opportunity_episode, d.market_state_publication_id,
          CASE WHEN d.market_state_context_hash IS NULL THEN d.market_state_snapshot
               ELSE (SELECT context.payload FROM analysis.decision_context context
                     WHERE context.content_hash = d.market_state_context_hash)
          END AS market_state_snapshot,
          analysis.expand_decision_impacts(d.portfolio_impacts, d.evidence_refs) AS portfolio_impacts,
          CASE WHEN d.risk_policy_context_hash IS NULL THEN d.risk_policy_snapshot
               ELSE (SELECT context.payload FROM analysis.decision_context context
                     WHERE context.content_hash = d.risk_policy_context_hash)
          END AS risk_policy_snapshot,
          d.market_state_context_hash, d.risk_policy_context_hash,
          d.semantic_fingerprint, d.last_evaluated_at, d.evidence_state,
          d.evidence_archive_manifest_id
        FROM analysis.ticker_decision d;
        GRANT SELECT ON analysis.ticker_decision_read TO market_app;
    """)
    _switch_phase4_readers("analysis.ticker_decision", "analysis.ticker_decision_read")


_PHASE4_READERS = (
    "analysis.enforce_phase4_allocation_item_funding_lineage()",
    "analysis.enforce_phase4_attribution_multiplier_guard()",
    "analysis.enforce_phase4_authority_lineage()",
    "analysis.enforce_phase4_lineage()",
    "analysis.write_phase4_allocation(jsonb,jsonb,text)",
)


def _switch_phase4_readers(source: str, destination: str) -> None:
    connection = op.get_bind()
    for signature in _PHASE4_READERS:
        definition = connection.exec_driver_sql(
            "SELECT pg_get_functiondef(%s::regprocedure)", (signature,),
        ).scalar_one()
        replacement, count = re.subn(r"\b" + re.escape(source) + r"\b", destination, definition)
        if count < 1:
            raise RuntimeError(f"phase 4 reader has no expected decision relation: {signature}")
        connection.exec_driver_sql(replacement.replace("%", "%%"))


def downgrade() -> None:
    if op.get_bind().exec_driver_sql("""
        SELECT EXISTS (SELECT 1 FROM analysis.ticker_decision
          GROUP BY instrument_id, decision_revision HAVING count(*) > 1)
    """).scalar():
        raise RuntimeError("same-revision decision changes must be reconciled before downgrading")
    if op.get_bind().exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM analysis.ticker_decision WHERE evidence_normalized)"
    ).scalar():
        raise RuntimeError("restore inline decision evidence before downgrading")
    _switch_phase4_readers("analysis.ticker_decision_read", "analysis.ticker_decision")
    op.execute("DROP TRIGGER paper_order_option_evidence_local ON app.paper_order")
    op.execute("DROP TRIGGER agent_task_option_evidence_local ON analysis.agent_task")
    op.execute("DROP TRIGGER shadow_trade_option_evidence_local ON analysis.shadow_trade")
    op.execute("DROP TRIGGER option_publication_evidence_local ON app.publication")
    op.execute("DROP TRIGGER option_primary_scan_evidence_local ON analysis.option_decision")
    op.execute("DROP TRIGGER archived_option_decision_immutable ON analysis.option_decision")
    op.execute("DROP TRIGGER archived_decision_evidence_immutable ON analysis.decision_evidence")
    op.execute("DROP FUNCTION analysis.reject_archived_evidence_mutation()")
    op.execute("DROP FUNCTION analysis.require_local_option_publication()")
    op.execute("DROP FUNCTION analysis.require_local_primary_option_scan()")
    op.execute("DROP FUNCTION analysis.require_local_option_evidence()")
    op.execute("DROP VIEW analysis.ticker_decision_read")
    op.execute("DROP TRIGGER ticker_decision_evidence_refs_valid ON analysis.ticker_decision")
    op.execute("DROP FUNCTION analysis.check_decision_evidence_refs()")
    op.execute("DROP FUNCTION analysis.intern_decision_evidence(jsonb,jsonb,jsonb,jsonb,jsonb,jsonb,jsonb)")
    op.execute("DROP FUNCTION analysis.expand_decision_manifest(jsonb,jsonb,jsonb)")
    op.execute("DROP FUNCTION analysis.expand_decision_capital(jsonb,jsonb,jsonb)")
    op.execute("DROP FUNCTION analysis.expand_decision_resolution(jsonb,jsonb,jsonb,jsonb)")
    op.execute("DROP FUNCTION analysis.expand_decision_impacts(jsonb,jsonb)")
    op.execute("DROP FUNCTION analysis.decision_payload(text)")
    op.execute("DROP FUNCTION analysis.intern_decision_payload(jsonb)")
    op.execute("DROP TABLE analysis.ticker_decision_checkpoint")
    op.execute("DROP TABLE ops.storage_daily_accounting")
    op.execute("DROP FUNCTION ops.postgres_data_directory()")
    op.execute("ALTER TABLE app.publication_bundle_item DROP COLUMN canonical_publication_id")
    op.execute("ALTER TABLE analysis.decision_evidence DROP COLUMN evidence_state, DROP COLUMN evidence_archive_manifest_id")
    op.execute("ALTER TABLE analysis.option_decision DROP COLUMN evidence_state, DROP COLUMN evidence_archive_manifest_id")
    op.execute("ALTER TABLE analysis.ticker_decision DROP COLUMN evidence_refs, DROP COLUMN evidence_normalized, DROP COLUMN semantic_fingerprint, DROP COLUMN last_evaluated_at, DROP COLUMN evidence_state, DROP COLUMN evidence_archive_manifest_id")
    op.execute("ALTER TABLE analysis.ticker_decision ADD CONSTRAINT ticker_decision_instrument_id_decision_revision_key UNIQUE (instrument_id, decision_revision)")
    from importlib import import_module
    old = import_module("migrations.versions.20260924_0036_hot_storage")
    op.execute(f"CREATE VIEW analysis.ticker_decision_read AS SELECT {old._COLUMNS} FROM analysis.ticker_decision d")
    op.execute("GRANT SELECT ON analysis.ticker_decision_read TO market_app")
