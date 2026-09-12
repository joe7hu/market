"""Index canonical paper reads and serialize advisory promotion receipts."""
from alembic import op

revision = "20260912_0020"
down_revision = "20260911_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX ix_paper_workbench_cursor ON app.paper_order (created_at DESC, id DESC)")
    op.execute("CREATE INDEX ix_paper_workbench_fill_order ON app.trade_journal ((details->>'paper_order_id'), instrument_id) WHERE rationale = 'deterministic_options_paper_execution'")
    op.execute("""
        CREATE OR REPLACE FUNCTION analysis.write_continuous_advisor_promotion(p jsonb) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis'
        AS $$
        DECLARE record_id uuid;
        DECLARE latest record;
        DECLARE active_version text;
        DECLARE expected_version text;
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtext('continuous-advisor-promotion'));
            SELECT * INTO latest FROM analysis.continuous_advisor_promotion_decision
              ORDER BY created_at DESC, id DESC LIMIT 1;
            IF FOUND THEN
                IF latest.decision = p->>'decision'
                   AND latest.candidate_prompt_version = p->>'candidate_prompt_version'
                   AND latest.previous_active_prompt_version IS NOT DISTINCT FROM NULLIF(p->>'previous_active_prompt_version', '') THEN
                    RETURN latest.id;
                END IF;
                active_version := CASE WHEN latest.decision = 'activate' THEN latest.candidate_prompt_version ELSE latest.previous_active_prompt_version END;
                expected_version := CASE WHEN p->>'decision' = 'rollback' THEN p->>'candidate_prompt_version' ELSE p->>'previous_active_prompt_version' END;
                IF active_version IS DISTINCT FROM expected_version THEN
                    RAISE EXCEPTION 'Active prompt revision changed; comparison must be rerun' USING ERRCODE = '40001';
                END IF;
            END IF;
            INSERT INTO analysis.continuous_advisor_promotion_decision (
                candidate_prompt_version, previous_active_prompt_version, decision, reason, scorecard
            ) VALUES (
                p->>'candidate_prompt_version', NULLIF(p->>'previous_active_prompt_version', ''),
                p->>'decision', left(COALESCE(p->>'reason', ''), 2000),
                COALESCE(p->'scorecard', '{}'::jsonb) || jsonb_build_object('actor', session_user, 'execution_controls_unchanged', true)
            ) RETURNING id INTO record_id;
            RETURN record_id;
        END;
        $$;
    """)


def downgrade() -> None:
    # Keep the safer write contract when removing read indexes.
    op.execute("DROP INDEX app.ix_paper_workbench_fill_order")
    op.execute("DROP INDEX app.ix_paper_workbench_cursor")
