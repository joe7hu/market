"""Make persisted strategy qualification authority immutable.

Revision ID: 20260830_0058
Revises: 20260830_0057
"""

from __future__ import annotations

from alembic import op


revision = "20260830_0058"
down_revision = "20260830_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DROP TRIGGER enforce_strategy_evaluation_availability
            ON analysis.strategy_evaluation;

        CREATE OR REPLACE FUNCTION analysis.enforce_strategy_evaluation_availability()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                NEW.available_at := clock_timestamp();
            ELSIF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'strategy evaluation authority is immutable';
            ELSIF NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'strategy evaluation authority is immutable';
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER enforce_strategy_evaluation_availability
            BEFORE INSERT OR UPDATE OR DELETE ON analysis.strategy_evaluation
            FOR EACH ROW
            EXECUTE FUNCTION analysis.enforce_strategy_evaluation_availability();

        CREATE FUNCTION analysis.enforce_strategy_revision_parameters_immutable()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.parameters IS DISTINCT FROM OLD.parameters THEN
                RAISE EXCEPTION 'strategy revision parameters are immutable';
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER enforce_strategy_revision_parameters_immutable
            BEFORE UPDATE OF parameters ON analysis.strategy_revision
            FOR EACH ROW
            EXECUTE FUNCTION analysis.enforce_strategy_revision_parameters_immutable();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS enforce_strategy_revision_parameters_immutable
            ON analysis.strategy_revision;
        DROP FUNCTION IF EXISTS analysis.enforce_strategy_revision_parameters_immutable();

        DROP TRIGGER IF EXISTS enforce_strategy_evaluation_availability
            ON analysis.strategy_evaluation;

        CREATE OR REPLACE FUNCTION analysis.enforce_strategy_evaluation_availability()
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
