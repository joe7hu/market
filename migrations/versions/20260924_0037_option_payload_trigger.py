"""Skip mark rematerialization for provider-payload-only quote updates."""
from alembic import op

revision = "20260924_0037"
down_revision = "20260924_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION analysis.refresh_paper_option_quote_update_projection_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis', 'raw', 'ingest'
        AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM old_rows old_row
                FULL JOIN new_rows new_row USING (id)
                WHERE old_row.id IS NULL OR new_row.id IS NULL
                   OR ROW(old_row.contract_id, old_row.snapshot_id, old_row.capture_generation_id,
                          old_row.bid, old_row.ask, old_row.mid, old_row.last,
                          old_row.observed_at, old_row.available_at)
                      IS DISTINCT FROM
                      ROW(new_row.contract_id, new_row.snapshot_id, new_row.capture_generation_id,
                          new_row.bid, new_row.ask, new_row.mid, new_row.last,
                          new_row.observed_at, new_row.available_at)
            ) THEN
                PERFORM analysis.refresh_paper_option_marks(
                    ARRAY(
                        SELECT DISTINCT contract_id
                        FROM (
                            SELECT contract_id FROM new_rows
                            UNION ALL
                            SELECT contract_id FROM old_rows
                        ) changed
                    )
                );
            END IF;
            RETURN NULL;
        END;
        $$;
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION analysis.refresh_paper_option_quote_update_projection_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path TO 'pg_catalog', 'analysis', 'raw', 'ingest'
        AS $$
        BEGIN
            PERFORM analysis.refresh_paper_option_marks(
                ARRAY(
                    SELECT DISTINCT contract_id
                    FROM (
                        SELECT contract_id FROM new_rows
                        UNION ALL
                        SELECT contract_id FROM old_rows
                    ) changed
                )
            );
            RETURN NULL;
        END;
        $$;
    """)
