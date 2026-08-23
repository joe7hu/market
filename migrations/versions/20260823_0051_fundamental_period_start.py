"""Preserve SEC duration starts in normalized financial observations.

Revision ID: 20260823_0051
Revises: 20260823_0050
"""

from __future__ import annotations

from alembic import op


revision = "20260823_0051"
down_revision = "20260823_0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE raw.fundamental_observation "
        "ADD COLUMN IF NOT EXISTS period_start DATE"
    )
    # The original key was sufficient for point-in-time balance-sheet facts,
    # but not for a 10-Q that carries both the quarter and year-to-date
    # duration for the same period end.
    op.execute(
        "ALTER TABLE raw.fundamental_observation "
        "DROP CONSTRAINT IF EXISTS "
        "fundamental_observation_instrument_id_source_id_metric_set_period_end_observed_at_key"
    )
    op.execute(
        """
        DO $$
        DECLARE
            constraint_name TEXT;
        BEGIN
            -- PostgreSQL may truncate the generated constraint name. Match the
            -- original key by its definition, not by a guessed identifier.
            FOR constraint_name IN
                SELECT conname
                FROM pg_constraint
                WHERE conrelid = 'raw.fundamental_observation'::regclass
                  AND contype = 'u'
                  AND pg_get_constraintdef(oid) =
                      'UNIQUE (instrument_id, source_id, metric_set, period_end, observed_at)'
            LOOP
                EXECUTE format(
                    'ALTER TABLE raw.fundamental_observation DROP CONSTRAINT %I',
                    constraint_name
                );
            END LOOP;
        END $$
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_raw_fundamental_observation_period
            ON raw.fundamental_observation
            (instrument_id, source_id, metric_set, period_end, observed_at,
             (coalesce(period_start, DATE '0001-01-01')))
        """
    )


def downgrade() -> None:
    # Do not silently destroy distinct quarter/YTD facts while restoring the
    # old key. The transaction rolls back with an explicit message if data
    # already uses the expanded period identity.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM raw.fundamental_observation
                GROUP BY instrument_id, source_id, metric_set, period_end, observed_at
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 20260823_0051: distinct fundamental periods would collide';
            END IF;
        END $$
        """
    )
    op.execute("DROP INDEX IF EXISTS raw.ux_raw_fundamental_observation_period")
    op.execute(
        """
        ALTER TABLE raw.fundamental_observation
            ADD CONSTRAINT fundamental_observation_instrument_id_source_id_metric_set_period_end_observed_at_key
            UNIQUE (instrument_id, source_id, metric_set, period_end, observed_at)
        """
    )
    op.execute("ALTER TABLE raw.fundamental_observation DROP COLUMN IF EXISTS period_start")
