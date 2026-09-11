"""Keep retryable continuous-advisor resolution attempts separate from outcomes."""

from alembic import op


revision = "20260911_0019"
down_revision = "20260911_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE analysis.continuous_advisor_resolution_attempt (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            claim_id uuid NOT NULL,
            response_id uuid NOT NULL,
            packet_id uuid NOT NULL,
            symbol text NOT NULL,
            status text NOT NULL,
            reason text NOT NULL,
            measured_through timestamp with time zone,
            metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT continuous_advisor_resolution_attempt_pkey PRIMARY KEY (id),
            CONSTRAINT continuous_advisor_resolution_attempt_claim_fkey
                FOREIGN KEY (claim_id) REFERENCES analysis.continuous_advisor_forecast_claim(id) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_resolution_attempt_response_fkey
                FOREIGN KEY (response_id) REFERENCES analysis.continuous_advisor_response(id) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_resolution_attempt_packet_fkey
                FOREIGN KEY (packet_id) REFERENCES analysis.continuous_advisor_packet(id) ON DELETE RESTRICT,
            CONSTRAINT continuous_advisor_resolution_attempt_status_check
                CHECK (status = ANY (ARRAY['waiting'::text, 'blocked'::text])),
            CONSTRAINT continuous_advisor_resolution_attempt_json_check
                CHECK (jsonb_typeof(metadata) = 'object')
        );

        CREATE INDEX ix_continuous_advisor_resolution_attempt_claim_created
            ON analysis.continuous_advisor_resolution_attempt (claim_id, created_at DESC, id DESC);

        CREATE FUNCTION analysis.write_continuous_advisor_resolution_attempt(p jsonb) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis'
        AS $$
        DECLARE record_id uuid;
        DECLARE claim_row record;
        BEGIN
            SELECT response_id, packet_id, symbol
              INTO claim_row
              FROM analysis.continuous_advisor_forecast_claim
             WHERE id = (p->>'claim_id')::uuid;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'continuous advisor claim not found: %', p->>'claim_id';
            END IF;
            INSERT INTO analysis.continuous_advisor_resolution_attempt (
                claim_id, response_id, packet_id, symbol, status, reason,
                measured_through, metadata
            )
            VALUES (
                (p->>'claim_id')::uuid, claim_row.response_id, claim_row.packet_id,
                claim_row.symbol, p->>'status', left(p->>'reason', 500),
                NULLIF(p->>'measured_through', '')::timestamptz,
                COALESCE(p->'metadata', '{}'::jsonb)
            )
            RETURNING id INTO record_id;
            RETURN record_id;
        END;
        $$;

        ALTER TABLE analysis.continuous_advisor_resolution_attempt OWNER TO market_research_signer;
        GRANT SELECT ON analysis.continuous_advisor_resolution_attempt TO market_app;
        GRANT EXECUTE ON FUNCTION analysis.write_continuous_advisor_resolution_attempt(jsonb) TO market_app;
        CREATE TRIGGER continuous_advisor_resolution_attempt_immutable
            BEFORE UPDATE OR DELETE ON analysis.continuous_advisor_resolution_attempt
            FOR EACH ROW EXECUTE FUNCTION analysis.reject_continuous_advisor_mutation();
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION IF EXISTS analysis.write_continuous_advisor_resolution_attempt(jsonb);
        DROP TABLE IF EXISTS analysis.continuous_advisor_resolution_attempt;
        """,
    )
