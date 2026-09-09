"""Add immutable continuous-advisor packets, claims, replay, and promotion records."""

from alembic import op


revision = "20260908_0007"
down_revision = "20260907_0006"
branch_labels = None
depends_on = None


_IMMUTABLE_TABLES = (
    "analysis.continuous_advisor_packet",
    "analysis.continuous_advisor_response",
    "analysis.continuous_advisor_forecast_claim",
    "analysis.continuous_advisor_forecast_outcome",
    "analysis.continuous_advisor_prompt_version",
    "analysis.continuous_advisor_evaluation_cohort",
    "analysis.continuous_advisor_promotion_decision",
)

_WRITER_SIGNATURES = (
    "analysis.write_continuous_advisor_packet(jsonb)",
    "analysis.write_continuous_advisor_response(jsonb)",
    "analysis.write_continuous_advisor_claim(jsonb)",
    "analysis.write_continuous_advisor_outcome(jsonb)",
    "analysis.write_continuous_advisor_prompt(jsonb)",
    "analysis.write_continuous_advisor_cohort(jsonb)",
    "analysis.write_continuous_advisor_promotion(jsonb)",
)


def upgrade() -> None:
    op.execute("ALTER TABLE app.publication ADD COLUMN superseded_at timestamp with time zone")
    op.execute(
        """
        CREATE TABLE analysis.continuous_advisor_packet (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            instrument_id bigint NOT NULL,
            symbol text NOT NULL,
            cutoff timestamp with time zone NOT NULL,
            slot_start timestamp with time zone NOT NULL,
            fingerprint text NOT NULL,
            prompt_version text NOT NULL,
            source_refs jsonb DEFAULT '[]'::jsonb NOT NULL,
            blockers jsonb DEFAULT '[]'::jsonb NOT NULL,
            packet jsonb NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT continuous_advisor_packet_pkey PRIMARY KEY (id),
            CONSTRAINT continuous_advisor_packet_instrument_id_fkey FOREIGN KEY (instrument_id) REFERENCES catalog.instrument(id) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_packet_symbol_check CHECK (symbol = upper(symbol)),
            CONSTRAINT continuous_advisor_packet_fingerprint_check CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
            CONSTRAINT continuous_advisor_packet_json_check CHECK (jsonb_typeof(source_refs) = 'array' AND jsonb_typeof(blockers) = 'array' AND jsonb_typeof(packet) = 'object'),
            CONSTRAINT continuous_advisor_packet_unique_slot UNIQUE (instrument_id, slot_start, fingerprint)
        );

        CREATE TABLE analysis.continuous_advisor_response (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            packet_id uuid NOT NULL,
            task_id uuid NOT NULL,
            agent_run_id uuid,
            symbol text NOT NULL,
            prompt_version text NOT NULL,
            provider text NOT NULL,
            model text NOT NULL,
            reasoning_effort text,
            status text NOT NULL,
            response jsonb DEFAULT '{}'::jsonb NOT NULL,
            validation jsonb DEFAULT '{}'::jsonb NOT NULL,
            started_at timestamp with time zone NOT NULL,
            finished_at timestamp with time zone,
            latency_ms integer,
            input_tokens bigint,
            output_tokens bigint,
            cost_usd numeric(14,6),
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT continuous_advisor_response_pkey PRIMARY KEY (id),
            CONSTRAINT continuous_advisor_response_packet_id_fkey FOREIGN KEY (packet_id) REFERENCES analysis.continuous_advisor_packet(id) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_response_task_id_fkey FOREIGN KEY (task_id) REFERENCES analysis.agent_task(id) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_response_agent_run_id_fkey FOREIGN KEY (agent_run_id) REFERENCES analysis.agent_run(id) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_response_status_check CHECK (status = ANY (ARRAY['succeeded'::text, 'failed'::text, 'skipped'::text, 'invalid'::text])),
            CONSTRAINT continuous_advisor_response_json_check CHECK (jsonb_typeof(response) = 'object' AND jsonb_typeof(validation) = 'object'),
            CONSTRAINT continuous_advisor_response_latency_check CHECK (latency_ms IS NULL OR latency_ms >= 0),
            CONSTRAINT continuous_advisor_response_unique_task UNIQUE (task_id)
        );

        CREATE TABLE analysis.continuous_advisor_forecast_claim (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            response_id uuid NOT NULL,
            packet_id uuid NOT NULL,
            symbol text NOT NULL,
            claim_key text NOT NULL,
            claim_kind text NOT NULL,
            horizon text NOT NULL,
            statement text NOT NULL,
            direction text,
            probability double precision NOT NULL,
            target double precision,
            evidence_refs jsonb DEFAULT '[]'::jsonb NOT NULL,
            claim jsonb DEFAULT '{}'::jsonb NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT continuous_advisor_forecast_claim_pkey PRIMARY KEY (id),
            CONSTRAINT continuous_advisor_forecast_claim_response_id_fkey FOREIGN KEY (response_id) REFERENCES analysis.continuous_advisor_response(id) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_forecast_claim_packet_id_fkey FOREIGN KEY (packet_id) REFERENCES analysis.continuous_advisor_packet(id) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_forecast_claim_kind_check CHECK (claim_kind = ANY (ARRAY['forecast'::text, 'invalidation'::text])),
            CONSTRAINT continuous_advisor_forecast_claim_probability_check CHECK (probability >= 0 AND probability <= 1),
            CONSTRAINT continuous_advisor_forecast_claim_json_check CHECK (jsonb_typeof(evidence_refs) = 'array' AND jsonb_typeof(claim) = 'object'),
            CONSTRAINT continuous_advisor_forecast_claim_unique_key UNIQUE (response_id, claim_key)
        );

        CREATE TABLE analysis.continuous_advisor_forecast_outcome (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            claim_id uuid NOT NULL,
            response_id uuid NOT NULL,
            packet_id uuid NOT NULL,
            symbol text NOT NULL,
            status text NOT NULL,
            resolved_at timestamp with time zone DEFAULT now() NOT NULL,
            measured_through timestamp with time zone,
            actual_return double precision,
            excess_return double precision,
            actual_direction text,
            correct boolean,
            calibration_error double precision,
            invalidation_actual boolean,
            invalidation_correct boolean,
            evidence_valid boolean NOT NULL,
            metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT continuous_advisor_forecast_outcome_pkey PRIMARY KEY (id),
            CONSTRAINT continuous_advisor_forecast_outcome_claim_id_fkey FOREIGN KEY (claim_id) REFERENCES analysis.continuous_advisor_forecast_claim(id) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_forecast_outcome_response_id_fkey FOREIGN KEY (response_id) REFERENCES analysis.continuous_advisor_response(id) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_forecast_outcome_packet_id_fkey FOREIGN KEY (packet_id) REFERENCES analysis.continuous_advisor_packet(id) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_forecast_outcome_status_check CHECK (status = ANY (ARRAY['resolved'::text, 'unresolvable'::text, 'quarantined'::text])),
            CONSTRAINT continuous_advisor_forecast_outcome_json_check CHECK (jsonb_typeof(metadata) = 'object'),
            CONSTRAINT continuous_advisor_forecast_outcome_unique_claim UNIQUE (claim_id)
        );

        CREATE TABLE analysis.continuous_advisor_prompt_version (
            version text NOT NULL,
            parent_version text,
            template jsonb NOT NULL,
            approved_change_set jsonb DEFAULT '[]'::jsonb NOT NULL,
            mutation_rationale text,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT continuous_advisor_prompt_version_pkey PRIMARY KEY (version),
            CONSTRAINT continuous_advisor_prompt_version_parent_fkey FOREIGN KEY (parent_version) REFERENCES analysis.continuous_advisor_prompt_version(version) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_prompt_version_json_check CHECK (jsonb_typeof(template) = 'object' AND jsonb_typeof(approved_change_set) = 'array'),
            CONSTRAINT continuous_advisor_prompt_version_name_check CHECK (version <> '')
        );

        CREATE TABLE analysis.continuous_advisor_evaluation_cohort (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            cohort_key text NOT NULL,
            active_prompt_version text NOT NULL,
            candidate_prompt_version text NOT NULL,
            cutoff_start timestamp with time zone,
            cutoff_end timestamp with time zone,
            holdout_cutoff timestamp with time zone,
            matched_outcomes integer DEFAULT 0 NOT NULL,
            scorecard jsonb DEFAULT '{}'::jsonb NOT NULL,
            walk_forward_scorecard jsonb DEFAULT '{}'::jsonb NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT continuous_advisor_evaluation_cohort_pkey PRIMARY KEY (id),
            CONSTRAINT continuous_advisor_evaluation_cohort_active_fkey FOREIGN KEY (active_prompt_version) REFERENCES analysis.continuous_advisor_prompt_version(version) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_evaluation_cohort_candidate_fkey FOREIGN KEY (candidate_prompt_version) REFERENCES analysis.continuous_advisor_prompt_version(version) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_evaluation_cohort_matches_check CHECK (matched_outcomes >= 0),
            CONSTRAINT continuous_advisor_evaluation_cohort_json_check CHECK (jsonb_typeof(scorecard) = 'object' AND jsonb_typeof(walk_forward_scorecard) = 'object'),
            CONSTRAINT continuous_advisor_evaluation_cohort_unique_run UNIQUE (active_prompt_version, candidate_prompt_version, cohort_key)
        );

        CREATE TABLE analysis.continuous_advisor_promotion_decision (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            candidate_prompt_version text NOT NULL,
            previous_active_prompt_version text,
            decision text NOT NULL,
            reason text NOT NULL,
            scorecard jsonb DEFAULT '{}'::jsonb NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT continuous_advisor_promotion_decision_pkey PRIMARY KEY (id),
            CONSTRAINT continuous_advisor_promotion_decision_candidate_fkey FOREIGN KEY (candidate_prompt_version) REFERENCES analysis.continuous_advisor_prompt_version(version) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_promotion_decision_previous_fkey FOREIGN KEY (previous_active_prompt_version) REFERENCES analysis.continuous_advisor_prompt_version(version) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_promotion_decision_kind_check CHECK (decision = ANY (ARRAY['activate'::text, 'reject'::text, 'rollback'::text])),
            CONSTRAINT continuous_advisor_promotion_decision_json_check CHECK (jsonb_typeof(scorecard) = 'object')
        );

        CREATE INDEX ix_continuous_advisor_packet_symbol_cutoff
            ON analysis.continuous_advisor_packet (symbol, cutoff DESC);
        CREATE INDEX ix_continuous_advisor_response_symbol_finished
            ON analysis.continuous_advisor_response (symbol, finished_at DESC);
        CREATE INDEX ix_continuous_advisor_claim_prompt_created
            ON analysis.continuous_advisor_forecast_claim (symbol, created_at DESC);
        CREATE INDEX ix_continuous_advisor_outcome_status_resolved
            ON analysis.continuous_advisor_forecast_outcome (status, resolved_at DESC);
        CREATE INDEX ix_continuous_advisor_cohort_candidate_created
            ON analysis.continuous_advisor_evaluation_cohort (candidate_prompt_version, created_at DESC);
        CREATE INDEX ix_continuous_advisor_promotion_candidate_created
            ON analysis.continuous_advisor_promotion_decision (candidate_prompt_version, created_at DESC);

        CREATE FUNCTION analysis.reject_continuous_advisor_mutation() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'continuous advisor records are immutable';
        END;
        $$;

        CREATE FUNCTION analysis.continuous_advisor_stable_packet(p jsonb) RETURNS jsonb
        LANGUAGE plpgsql IMMUTABLE STRICT
        SET search_path TO 'pg_catalog', 'analysis'
        AS $$
        DECLARE kind text; result jsonb;
        BEGIN
            kind := jsonb_typeof(p);
            IF kind = 'object' THEN
                SELECT COALESCE(jsonb_object_agg(key, analysis.continuous_advisor_stable_packet(value)), '{}'::jsonb)
                INTO result
                FROM jsonb_each(p)
                WHERE key NOT IN ('fingerprint', 'slot_start', 'created_at', 'ingest_run_id',
                                  'ingested_at', 'last_seen_at', 'payload_id', 'run_id', 'updated_at');
            ELSIF kind = 'array' THEN
                SELECT COALESCE(jsonb_agg(analysis.continuous_advisor_stable_packet(value) ORDER BY ordinality), '[]'::jsonb)
                INTO result
                FROM jsonb_array_elements(p) WITH ORDINALITY;
            ELSE
                result := p;
            END IF;
            RETURN result;
        END;
        $$;

        CREATE FUNCTION analysis.continuous_advisor_packet_fingerprint(p jsonb) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT
        SET search_path TO 'pg_catalog', 'analysis'
        AS $$
            SELECT analysis.phase4_content_digest(analysis.continuous_advisor_stable_packet(p))
        $$;

        CREATE FUNCTION analysis.write_continuous_advisor_packet(p jsonb) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis'
        AS $$
        DECLARE record_id uuid; instrument_symbol text;
        BEGIN
            SELECT symbol INTO instrument_symbol
            FROM catalog.instrument
            WHERE id = (p->>'instrument_id')::bigint;
            IF NOT FOUND
               OR p->>'symbol' IS DISTINCT FROM instrument_symbol
               OR p->>'symbol' IS DISTINCT FROM upper(p->>'symbol')
               OR jsonb_typeof(p->'packet') IS DISTINCT FROM 'object'
               OR (p->'packet')->>'symbol' IS DISTINCT FROM p->>'symbol'
               OR (p->'packet')->>'fingerprint' IS DISTINCT FROM p->>'fingerprint'
               OR analysis.continuous_advisor_packet_fingerprint(p->'packet') IS DISTINCT FROM p->>'fingerprint'
               OR (p->'packet')->>'prompt_version' IS DISTINCT FROM p->>'prompt_version'
               OR NULLIF((p->'packet')->>'cutoff', '')::timestamptz IS DISTINCT FROM NULLIF(p->>'cutoff', '')::timestamptz
               OR NULLIF((p->'packet')->>'slot_start', '')::timestamptz IS DISTINCT FROM NULLIF(p->>'slot_start', '')::timestamptz
               OR COALESCE((p->'packet')->'source_references', '[]'::jsonb) IS DISTINCT FROM COALESCE(p->'source_refs', '[]'::jsonb)
               OR COALESCE((p->'packet')->'blockers', '[]'::jsonb) IS DISTINCT FROM COALESCE(p->'blockers', '[]'::jsonb) THEN
                RAISE EXCEPTION 'continuous advisor packet identity or fingerprint is invalid';
            END IF;
            INSERT INTO analysis.continuous_advisor_packet (
                instrument_id, symbol, cutoff, slot_start, fingerprint,
                prompt_version, source_refs, blockers, packet
            )
            VALUES (
                (p->>'instrument_id')::bigint, p->>'symbol', (p->>'cutoff')::timestamptz,
                (p->>'slot_start')::timestamptz, p->>'fingerprint', p->>'prompt_version',
                COALESCE(p->'source_refs', '[]'::jsonb), COALESCE(p->'blockers', '[]'::jsonb), p->'packet'
            )
            ON CONFLICT (instrument_id, slot_start, fingerprint) DO NOTHING
            RETURNING id INTO record_id;
            IF record_id IS NULL THEN
                SELECT id INTO record_id
                FROM analysis.continuous_advisor_packet
                WHERE instrument_id = (p->>'instrument_id')::bigint
                  AND slot_start = (p->>'slot_start')::timestamptz
                  AND fingerprint = p->>'fingerprint';
            END IF;
            RETURN record_id;
        END;
        $$;

        CREATE FUNCTION analysis.write_continuous_advisor_response(p jsonb) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis'
        AS $$
        DECLARE record_id uuid; task_row record; packet_row record;
        BEGIN
            SELECT id, agent_run_id, task_kind, status, provider, model, prompt_version,
                   evidence_fingerprint, request
            INTO task_row
            FROM analysis.agent_task
            WHERE id = (p->>'task_id')::uuid;
            IF NOT FOUND OR task_row.task_kind <> 'continuous_advisor' OR task_row.status <> 'running' THEN
                RAISE EXCEPTION 'continuous advisor task is not an active task: %', p->>'task_id';
            END IF;
            SELECT symbol, prompt_version, fingerprint, cutoff, slot_start
            INTO packet_row
            FROM analysis.continuous_advisor_packet
            WHERE id = (p->>'packet_id')::uuid;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'continuous advisor packet not found: %', p->>'packet_id';
            END IF;
            IF task_row.agent_run_id IS DISTINCT FROM NULLIF(p->>'agent_run_id', '')::uuid
               OR task_row.request->>'packet_id' IS DISTINCT FROM p->>'packet_id'
               OR task_row.request->>'packet_fingerprint' IS DISTINCT FROM task_row.evidence_fingerprint
               OR NULLIF(task_row.request->>'cutoff', '')::timestamptz IS DISTINCT FROM packet_row.cutoff
               OR NULLIF(task_row.request->>'slot_start', '')::timestamptz IS DISTINCT FROM packet_row.slot_start
               OR task_row.provider IS DISTINCT FROM p->>'provider'
               OR task_row.model IS DISTINCT FROM p->>'model'
               OR task_row.prompt_version IS DISTINCT FROM p->>'prompt_version'
               OR upper(task_row.request->>'symbol') IS DISTINCT FROM upper(p->>'symbol')
               OR upper(task_row.request->>'symbol') IS DISTINCT FROM upper(packet_row.symbol)
               OR task_row.prompt_version IS DISTINCT FROM packet_row.prompt_version
               OR task_row.evidence_fingerprint IS DISTINCT FROM packet_row.fingerprint THEN
                RAISE EXCEPTION 'continuous advisor response lineage does not match its task and packet';
            END IF;
            INSERT INTO analysis.continuous_advisor_response (
                packet_id, task_id, agent_run_id, symbol, prompt_version,
                provider, model, reasoning_effort, status, response, validation,
                started_at, finished_at, latency_ms, input_tokens, output_tokens, cost_usd
            )
            VALUES (
                (p->>'packet_id')::uuid, task_row.id, task_row.agent_run_id,
                task_row.request->>'symbol', task_row.prompt_version, task_row.provider, task_row.model,
                NULLIF(p->>'reasoning_effort', ''), p->>'status', COALESCE(p->'response', '{}'::jsonb),
                COALESCE(p->'validation', '{}'::jsonb), (p->>'started_at')::timestamptz,
                NULLIF(p->>'finished_at', '')::timestamptz, NULLIF(p->>'latency_ms', '')::integer,
                NULLIF(p->>'input_tokens', '')::bigint, NULLIF(p->>'output_tokens', '')::bigint,
                NULLIF(p->>'cost_usd', '')::numeric
            )
            ON CONFLICT (task_id) DO NOTHING
            RETURNING id INTO record_id;
            IF record_id IS NULL THEN
                SELECT id INTO record_id
                FROM analysis.continuous_advisor_response
                WHERE task_id = (p->>'task_id')::uuid;
            END IF;
            RETURN record_id;
        END;
        $$;

        CREATE FUNCTION analysis.write_continuous_advisor_claim(p jsonb) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis'
        AS $$
        DECLARE record_id uuid; response_row record; packet_row record;
        BEGIN
            SELECT id, packet_id, task_id, symbol, prompt_version, status
            INTO response_row
            FROM analysis.continuous_advisor_response
            WHERE id = (p->>'response_id')::uuid;
            IF NOT FOUND OR response_row.status <> 'succeeded' THEN
                RAISE EXCEPTION 'continuous advisor response is not a successful response: %', p->>'response_id';
            END IF;
            SELECT symbol, prompt_version, fingerprint
            INTO packet_row
            FROM analysis.continuous_advisor_packet
            WHERE id = response_row.packet_id;
            IF NOT FOUND
               OR response_row.packet_id IS DISTINCT FROM (p->>'packet_id')::uuid
               OR upper(response_row.symbol) IS DISTINCT FROM upper(p->>'symbol')
               OR response_row.prompt_version IS DISTINCT FROM packet_row.prompt_version THEN
                RAISE EXCEPTION 'continuous advisor claim lineage does not match its response and packet';
            END IF;
            INSERT INTO analysis.continuous_advisor_forecast_claim (
                response_id, packet_id, symbol, claim_key, claim_kind,
                horizon, statement, direction, probability, target, evidence_refs, claim
            )
            VALUES (
                response_row.id, response_row.packet_id, response_row.symbol, p->>'claim_key',
                p->>'claim_kind', p->>'horizon', p->>'statement', NULLIF(p->>'direction', ''),
                (p->>'probability')::double precision, NULLIF(p->>'target', '')::double precision,
                COALESCE(p->'evidence_refs', '[]'::jsonb), COALESCE(p->'claim', '{}'::jsonb)
            )
            ON CONFLICT (response_id, claim_key) DO NOTHING
            RETURNING id INTO record_id;
            IF record_id IS NULL THEN
                SELECT id INTO record_id
                FROM analysis.continuous_advisor_forecast_claim
                WHERE response_id = (p->>'response_id')::uuid
                  AND claim_key = p->>'claim_key';
            END IF;
            RETURN record_id;
        END;
        $$;

        CREATE FUNCTION analysis.write_continuous_advisor_outcome(p jsonb) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis'
        AS $$
        DECLARE record_id uuid; claim_row record;
        BEGIN
            SELECT response_id, packet_id, symbol INTO claim_row
            FROM analysis.continuous_advisor_forecast_claim
            WHERE id = (p->>'claim_id')::uuid;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'continuous advisor claim not found: %', p->>'claim_id';
            END IF;
            INSERT INTO analysis.continuous_advisor_forecast_outcome (
                claim_id, response_id, packet_id, symbol, status, resolved_at,
                measured_through, actual_return, excess_return, actual_direction,
                correct, calibration_error, invalidation_actual, invalidation_correct,
                evidence_valid, metadata
            )
            VALUES (
                (p->>'claim_id')::uuid, claim_row.response_id, claim_row.packet_id, claim_row.symbol,
                p->>'status', now(), NULLIF(p->>'measured_through', '')::timestamptz,
                NULLIF(p->>'actual_return', '')::double precision, NULLIF(p->>'excess_return', '')::double precision,
                NULLIF(p->>'actual_direction', ''), NULLIF(p->>'correct', '')::boolean,
                NULLIF(p->>'calibration_error', '')::double precision, NULLIF(p->>'invalidation_actual', '')::boolean,
                NULLIF(p->>'invalidation_correct', '')::boolean, COALESCE((p->>'evidence_valid')::boolean, false),
                COALESCE(p->'metadata', '{}'::jsonb)
            )
            ON CONFLICT (claim_id) DO NOTHING
            RETURNING id INTO record_id;
            IF record_id IS NULL THEN
                SELECT id INTO record_id
                FROM analysis.continuous_advisor_forecast_outcome
                WHERE claim_id = (p->>'claim_id')::uuid;
            END IF;
            RETURN record_id;
        END;
        $$;

        CREATE FUNCTION analysis.write_continuous_advisor_prompt(p jsonb) RETURNS text
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis'
        AS $$
        DECLARE record_version text;
        BEGIN
            INSERT INTO analysis.continuous_advisor_prompt_version (
                version, parent_version, template, approved_change_set, mutation_rationale
            )
            VALUES (
                p->>'version', NULLIF(p->>'parent_version', ''), COALESCE(p->'template', '{}'::jsonb),
                COALESCE(p->'approved_change_set', '[]'::jsonb), NULLIF(p->>'mutation_rationale', '')
            )
            ON CONFLICT (version) DO NOTHING
            RETURNING version INTO record_version;
            RETURN COALESCE(record_version, p->>'version');
        END;
        $$;

        CREATE FUNCTION analysis.write_continuous_advisor_cohort(p jsonb) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis'
        AS $$
        DECLARE record_id uuid;
        BEGIN
            INSERT INTO analysis.continuous_advisor_evaluation_cohort (
                cohort_key, active_prompt_version, candidate_prompt_version,
                cutoff_start, cutoff_end, holdout_cutoff, matched_outcomes,
                scorecard, walk_forward_scorecard
            )
            VALUES (
                p->>'cohort_key', p->>'active_prompt_version', p->>'candidate_prompt_version',
                NULLIF(p->>'cutoff_start', '')::timestamptz, NULLIF(p->>'cutoff_end', '')::timestamptz,
                NULLIF(p->>'holdout_cutoff', '')::timestamptz, COALESCE((p->>'matched_outcomes')::integer, 0),
                COALESCE(p->'scorecard', '{}'::jsonb), COALESCE(p->'walk_forward_scorecard', '{}'::jsonb)
            )
            ON CONFLICT (active_prompt_version, candidate_prompt_version, cohort_key) DO NOTHING
            RETURNING id INTO record_id;
            IF record_id IS NULL THEN
                SELECT id INTO record_id
                FROM analysis.continuous_advisor_evaluation_cohort
                WHERE active_prompt_version = p->>'active_prompt_version'
                  AND candidate_prompt_version = p->>'candidate_prompt_version'
                  AND cohort_key = p->>'cohort_key';
            END IF;
            RETURN record_id;
        END;
        $$;

        CREATE FUNCTION analysis.write_continuous_advisor_promotion(p jsonb) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis'
        AS $$
        DECLARE record_id uuid;
        BEGIN
            INSERT INTO analysis.continuous_advisor_promotion_decision (
                candidate_prompt_version, previous_active_prompt_version,
                decision, reason, scorecard
            )
            VALUES (
                p->>'candidate_prompt_version', NULLIF(p->>'previous_active_prompt_version', ''),
                p->>'decision', left(COALESCE(p->>'reason', ''), 2_000), COALESCE(p->'scorecard', '{}'::jsonb)
            )
            RETURNING id INTO record_id;
            RETURN record_id;
        END;
        $$;
        """
    )
    for signature in _WRITER_SIGNATURES:
        op.execute(f"ALTER FUNCTION {signature} OWNER TO market_research_signer")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO market_app")
    for table in _IMMUTABLE_TABLES:
        short_name = table.split(".", 1)[1]
        op.execute(f"ALTER TABLE {table} OWNER TO market_research_signer")
        op.execute(
            f"CREATE TRIGGER {short_name}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION analysis.reject_continuous_advisor_mutation()"
        )
        op.execute(f"REVOKE INSERT, UPDATE, DELETE ON TABLE {table} FROM market_app")
        lock_privilege = ", UPDATE" if table == "analysis.continuous_advisor_packet" else ""
        op.execute(f"GRANT SELECT{lock_privilege} ON TABLE {table} TO market_app")
        op.execute("GRANT SELECT ON TABLE analysis.agent_task TO market_research_signer")
    # The existing thesis-monitor writer is part of this workflow. Keep its
    # intended least privilege narrow: revision insert/update, never delete.
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE app.thesis TO market_app")
    op.execute("GRANT SELECT, USAGE ON SEQUENCE app.thesis_id_seq TO market_app")


def downgrade() -> None:
    for signature in _WRITER_SIGNATURES:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("DROP FUNCTION IF EXISTS analysis.continuous_advisor_packet_fingerprint(jsonb)")
    op.execute("DROP FUNCTION IF EXISTS analysis.continuous_advisor_stable_packet(jsonb)")
    for table in reversed(_IMMUTABLE_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("ALTER TABLE app.publication DROP COLUMN IF EXISTS superseded_at")
    op.execute("DROP FUNCTION IF EXISTS analysis.reject_continuous_advisor_mutation()")
    op.execute("REVOKE INSERT, UPDATE ON TABLE app.thesis FROM market_app")
    op.execute("REVOKE USAGE ON SEQUENCE app.thesis_id_seq FROM market_app")
