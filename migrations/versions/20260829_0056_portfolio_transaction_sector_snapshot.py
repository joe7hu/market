"""Capture instrument sector as append-only portfolio transaction evidence.

Revision ID: 20260829_0056
Revises: 20260825_0055
"""

from __future__ import annotations

from alembic import op


revision = "20260829_0056"
down_revision = "20260825_0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.portfolio_transaction
            ADD COLUMN IF NOT EXISTS instrument_sector TEXT;

        CREATE OR REPLACE FUNCTION app.capture_portfolio_transaction_sector()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.instrument_id IS NOT NULL AND NEW.instrument_sector IS NULL THEN
                SELECT instrument.sector
                  INTO NEW.instrument_sector
                  FROM catalog.instrument instrument
                 WHERE instrument.id = NEW.instrument_id;
            END IF;
            RETURN NEW;
        END;
        $$;

        DROP TRIGGER IF EXISTS capture_portfolio_transaction_sector
            ON app.portfolio_transaction;
        CREATE TRIGGER capture_portfolio_transaction_sector
            BEFORE INSERT ON app.portfolio_transaction
            FOR EACH ROW
            EXECUTE FUNCTION app.capture_portfolio_transaction_sector();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS capture_portfolio_transaction_sector
            ON app.portfolio_transaction;
        DROP FUNCTION IF EXISTS app.capture_portfolio_transaction_sector();
        ALTER TABLE app.portfolio_transaction
            DROP COLUMN IF EXISTS instrument_sector;
        """
    )
