"""Track explicit instrument lifecycle and delisting terminal marks.

Revision ID: 20260823_0052
Revises: 20260823_0051
"""

from __future__ import annotations

from alembic import op


revision = "20260823_0052"
down_revision = "20260823_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE catalog.instrument
            ADD COLUMN IF NOT EXISTS delisted_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS delisting_price DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS delisting_available_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS delisting_source TEXT;
        ALTER TABLE catalog.instrument
            ADD CONSTRAINT ck_instrument_delisting_price
                CHECK (delisting_price IS NULL OR delisting_price > 0),
            ADD CONSTRAINT ck_instrument_delisting_availability
                CHECK (delisting_available_at IS NULL OR delisted_at IS NOT NULL)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE catalog.instrument
            DROP CONSTRAINT IF EXISTS ck_instrument_delisting_availability,
            DROP CONSTRAINT IF EXISTS ck_instrument_delisting_price,
            DROP COLUMN IF EXISTS delisting_source,
            DROP COLUMN IF EXISTS delisting_available_at,
            DROP COLUMN IF EXISTS delisting_price,
            DROP COLUMN IF EXISTS delisted_at
        """
    )
