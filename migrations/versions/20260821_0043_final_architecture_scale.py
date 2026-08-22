"""Make availability projection and option partition policy authoritative.

Revision ID: 20260821_0043
Revises: 20260820_0042

This migration contains only bounded DDL.  Large confirmation relations are
reclaimed by the explicit storage cutover command after its backup and
coverage gates pass.
"""

from __future__ import annotations

from alembic import op

from migrations.current_price_selector_sql import (
    optimized_current_price_selector_sql,
    current_price_selector_sql,
)


revision = "20260821_0043"
down_revision = "20260820_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The first non-overlapping boundary after the current monthly partition
    # becomes daily.  The live monthly partition is therefore never rewritten
    # by this migration.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ops.option_quote_partition_policy (
            policy_key TEXT PRIMARY KEY,
            daily_start DATE NOT NULL,
            hot_retention_days INTEGER NOT NULL DEFAULT 7,
            archive_retention_days INTEGER NOT NULL DEFAULT 730,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        INSERT INTO ops.option_quote_partition_policy (policy_key, daily_start)
        VALUES ('default', (date_trunc('month', current_date) + interval '1 month')::date)
        ON CONFLICT (policy_key) DO NOTHING
        """
    )
    op.execute(
        """
        ALTER TABLE app.option_history_policy
            DROP CONSTRAINT IF EXISTS ck_option_history_policy_retention;
        UPDATE app.option_history_policy
        SET derived_retention_days = 30, updated_at = now();
        ALTER TABLE app.option_history_policy
            ALTER COLUMN derived_retention_days SET DEFAULT 30;
        ALTER TABLE app.option_history_policy
            ADD CONSTRAINT ck_option_history_policy_retention CHECK (
                (profile = 'history_full'
                 AND normalized_retention_days = 730
                 AND derived_retention_days = 30
                 AND provider_payload_retention_days = 90
                 AND event_id IS NULL)
                OR
                (profile = 'event_strip'
                 AND normalized_retention_days = 365
                 AND derived_retention_days = 30
                 AND provider_payload_retention_days = 30
                 AND event_id IS NOT NULL)
            );
        """
    )

    # No current-price request may touch the large confirmation staging tables.
    op.execute(
        optimized_current_price_selector_sql(
            use_availability_projection=True,
            include_legacy_fallback=False,
        )
    )
    op.execute(
        """
        CREATE OR REPLACE VIEW raw.confirmed_price_bar AS
        SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.interval, fact.observed_at)
               fact.*
        FROM (
            SELECT * FROM raw.price_bar
            UNION ALL
            SELECT * FROM raw.price_bar_history
        ) fact
        JOIN raw.price_bar_fact_availability availability
          ON availability.fact_id = fact.id
         AND availability.fact_available_at = fact.available_at
        JOIN ingest.run price_run
          ON price_run.id = availability.ingest_run_id
         AND price_run.status IN ('succeeded', 'partial')
         AND price_run.finished_at IS NOT NULL
        ORDER BY fact.instrument_id, fact.source_id, fact.interval,
                 fact.observed_at, fact.available_at DESC
        """
    )
    op.execute(
        """
        CREATE OR REPLACE VIEW raw.confirmed_quote AS
        SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.observed_at)
               fact.*
        FROM (
            SELECT * FROM raw.quote
            UNION ALL
            SELECT * FROM raw.quote_history
        ) fact
        JOIN raw.quote_fact_availability availability
          ON availability.fact_id = fact.id
         AND availability.fact_available_at = fact.available_at
        JOIN ingest.run price_run
          ON price_run.id = availability.ingest_run_id
         AND price_run.status IN ('succeeded', 'partial')
         AND price_run.finished_at IS NOT NULL
        ORDER BY fact.instrument_id, fact.source_id, fact.observed_at,
                 fact.available_at DESC
        """
    )

    # These relations are deterministic children of analysis.run.  Cascade
    # only the reproducible detail; the retention query protects outcomes,
    # journals, publications, and event-study evidence before deleting a run.
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('analysis.option_relative_value') IS NOT NULL THEN
                ALTER TABLE analysis.option_relative_value
                    DROP CONSTRAINT IF EXISTS option_relative_value_analysis_run_id_fkey;
                ALTER TABLE analysis.option_relative_value
                    ADD CONSTRAINT option_relative_value_analysis_run_id_fkey
                    FOREIGN KEY (analysis_run_id) REFERENCES analysis.run(id) ON DELETE CASCADE NOT VALID;
            END IF;
            IF to_regclass('analysis.option_surface_summary') IS NOT NULL THEN
                ALTER TABLE analysis.option_surface_summary
                    DROP CONSTRAINT IF EXISTS option_surface_summary_analysis_run_id_fkey;
                ALTER TABLE analysis.option_surface_summary
                    ADD CONSTRAINT option_surface_summary_analysis_run_id_fkey
                    FOREIGN KEY (analysis_run_id) REFERENCES analysis.run(id) ON DELETE CASCADE NOT VALID;
            END IF;
            IF to_regclass('analysis.option_surface_shift') IS NOT NULL THEN
                ALTER TABLE analysis.option_surface_shift
                    DROP CONSTRAINT IF EXISTS option_surface_shift_current_analysis_run_id_fkey;
                ALTER TABLE analysis.option_surface_shift
                    DROP CONSTRAINT IF EXISTS option_surface_shift_previous_analysis_run_id_fkey;
                ALTER TABLE analysis.option_surface_shift
                    ADD CONSTRAINT option_surface_shift_current_analysis_run_id_fkey
                    FOREIGN KEY (current_analysis_run_id) REFERENCES analysis.run(id) ON DELETE CASCADE NOT VALID;
                ALTER TABLE analysis.option_surface_shift
                    ADD CONSTRAINT option_surface_shift_previous_analysis_run_id_fkey
                    FOREIGN KEY (previous_analysis_run_id) REFERENCES analysis.run(id) ON DELETE CASCADE NOT VALID;
            END IF;
        END $$
        """
    )
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
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS price_bar_confirmation_projection ON raw.price_bar_confirmation;
        CREATE TRIGGER price_bar_confirmation_projection
        AFTER INSERT ON raw.price_bar_confirmation
        FOR EACH ROW EXECUTE FUNCTION raw.project_confirmation_staging();
        DROP TRIGGER IF EXISTS quote_confirmation_projection ON raw.quote_confirmation;
        CREATE TRIGGER quote_confirmation_projection
        AFTER INSERT ON raw.quote_confirmation
        FOR EACH ROW EXECUTE FUNCTION raw.project_confirmation_staging();
        """
    )


def downgrade() -> None:
    # Restore the pre-scale retention contract before older migrations remove
    # the profile columns and recreate their original constraint.
    op.execute(
        """
        ALTER TABLE app.option_history_policy
            DROP CONSTRAINT IF EXISTS ck_option_history_policy_retention;
        UPDATE app.option_history_policy
        SET derived_retention_days = 730, updated_at = now();
        ALTER TABLE app.option_history_policy
            ALTER COLUMN derived_retention_days SET DEFAULT 730;
        ALTER TABLE app.option_history_policy
            ADD CONSTRAINT ck_option_history_policy_retention CHECK (
                (profile = 'history_full'
                 AND normalized_retention_days = 730
                 AND derived_retention_days = 730
                 AND provider_payload_retention_days = 90
                 AND event_id IS NULL)
                OR
                (profile = 'event_strip'
                 AND normalized_retention_days = 365
                 AND derived_retention_days = 730
                 AND provider_payload_retention_days = 30
                 AND event_id IS NOT NULL)
            );
        """
    )
    op.execute(current_price_selector_sql(use_availability_projection=True))
    op.execute("DROP TRIGGER IF EXISTS price_bar_confirmation_projection ON raw.price_bar_confirmation")
    op.execute("DROP TRIGGER IF EXISTS quote_confirmation_projection ON raw.quote_confirmation")
    op.execute("DROP FUNCTION IF EXISTS raw.project_confirmation_staging()")
    op.execute("DROP VIEW IF EXISTS raw.confirmed_quote")
    op.execute("DROP VIEW IF EXISTS raw.confirmed_price_bar")
    op.execute(
        """
        CREATE VIEW raw.confirmed_price_bar AS
        SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.interval, fact.observed_at) fact.*
        FROM (SELECT * FROM raw.price_bar UNION ALL SELECT * FROM raw.price_bar_history) fact
        WHERE EXISTS (
            SELECT 1 FROM raw.price_bar_confirmation confirmation
            JOIN ingest.run price_run ON price_run.id = confirmation.ingest_run_id
            WHERE confirmation.fact_id = fact.id
              AND confirmation.fact_available_at = fact.available_at
              AND price_run.status IN ('succeeded', 'partial')
              AND price_run.finished_at IS NOT NULL
        )
        ORDER BY fact.instrument_id, fact.source_id, fact.interval, fact.observed_at, fact.available_at DESC
        """
    )
    op.execute(
        """
        CREATE VIEW raw.confirmed_quote AS
        SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.observed_at) fact.*
        FROM (SELECT * FROM raw.quote UNION ALL SELECT * FROM raw.quote_history) fact
        WHERE EXISTS (
            SELECT 1 FROM raw.quote_confirmation confirmation
            JOIN ingest.run price_run ON price_run.id = confirmation.ingest_run_id
            WHERE confirmation.fact_id = fact.id
              AND confirmation.fact_available_at = fact.available_at
              AND price_run.status IN ('succeeded', 'partial')
              AND price_run.finished_at IS NOT NULL
        )
        ORDER BY fact.instrument_id, fact.source_id, fact.observed_at, fact.available_at DESC
        """
    )
    op.execute("DROP TABLE IF EXISTS ops.option_quote_partition_policy")
    # Leave projection tables for revision 0033's downgrade.  Revision 0034
    # still recreates its projection-backed function before 0033 removes them.
