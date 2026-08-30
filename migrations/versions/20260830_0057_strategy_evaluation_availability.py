"""Record immutable database-owned strategy evaluation availability.

Revision ID: 20260830_0057
Revises: 20260829_0056
"""

from __future__ import annotations

from alembic import op


revision = "20260830_0057"
down_revision = "20260829_0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE analysis.strategy_evaluation
            ADD COLUMN available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp();

        CREATE FUNCTION analysis.enforce_strategy_evaluation_availability()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                NEW.available_at := clock_timestamp();
            ELSIF NEW.available_at IS DISTINCT FROM OLD.available_at THEN
                RAISE EXCEPTION 'strategy evaluation available_at is immutable';
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER enforce_strategy_evaluation_availability
            BEFORE INSERT OR UPDATE OF available_at ON analysis.strategy_evaluation
            FOR EACH ROW
            EXECUTE FUNCTION analysis.enforce_strategy_evaluation_availability();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS enforce_strategy_evaluation_availability
            ON analysis.strategy_evaluation;
        DROP FUNCTION IF EXISTS analysis.enforce_strategy_evaluation_availability();
        ALTER TABLE analysis.strategy_evaluation DROP COLUMN IF EXISTS available_at;
        """
    )
