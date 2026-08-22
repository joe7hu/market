"""Keep availability projection keys aligned with staging updates."""

from __future__ import annotations

from alembic import op


revision = "20260821_0046"
down_revision = "20260821_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION raw.project_confirmation_staging()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_TABLE_NAME = 'price_bar_confirmation' THEN
                IF TG_OP = 'UPDATE' THEN
                    DELETE FROM raw.price_bar_fact_availability
                    WHERE fact_id = OLD.fact_id
                      AND fact_available_at = OLD.fact_available_at
                      AND ingest_run_id = OLD.ingest_run_id;
                END IF;
                INSERT INTO raw.price_bar_fact_availability (fact_id, fact_available_at, ingest_run_id)
                VALUES (NEW.fact_id, NEW.fact_available_at, NEW.ingest_run_id)
                ON CONFLICT (fact_id, fact_available_at) DO NOTHING;
            ELSE
                IF TG_OP = 'UPDATE' THEN
                    DELETE FROM raw.quote_fact_availability
                    WHERE fact_id = OLD.fact_id
                      AND fact_available_at = OLD.fact_available_at
                      AND ingest_run_id = OLD.ingest_run_id;
                END IF;
                INSERT INTO raw.quote_fact_availability (fact_id, fact_available_at, ingest_run_id)
                VALUES (NEW.fact_id, NEW.fact_available_at, NEW.ingest_run_id)
                ON CONFLICT (fact_id, fact_available_at) DO NOTHING;
            END IF;
            RETURN NEW;
        END
        $$;
        DROP TRIGGER IF EXISTS price_bar_confirmation_projection ON raw.price_bar_confirmation;
        CREATE TRIGGER price_bar_confirmation_projection
        AFTER INSERT OR UPDATE OF fact_id, fact_available_at, ingest_run_id
        ON raw.price_bar_confirmation
        FOR EACH ROW EXECUTE FUNCTION raw.project_confirmation_staging();
        DROP TRIGGER IF EXISTS quote_confirmation_projection ON raw.quote_confirmation;
        CREATE TRIGGER quote_confirmation_projection
        AFTER INSERT OR UPDATE OF fact_id, fact_available_at, ingest_run_id
        ON raw.quote_confirmation
        FOR EACH ROW EXECUTE FUNCTION raw.project_confirmation_staging();
        """
    )


def downgrade() -> None:
    pass
