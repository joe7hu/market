"""Project successful price-fact confirmations for bounded current-price reads.

Revision ID: 20260812_0033
Revises: 20260812_0032
"""

from __future__ import annotations

from alembic import op

from migrations.current_price_selector_sql import current_price_selector_sql


revision = "20260812_0033"
down_revision = "20260812_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The legacy confirmation relations retain detailed retry audit rows.  This
    # projection has one earliest successful run per fact version, so a live
    # selector never needs to walk that audit history.  Do not use a trigger on
    # ingest.run here: that would scan a 50M-row audit table for every run.
    # Existing rows are projected by the bounded maintenance repository after
    # this DDL commits; a migration-wide scan would not be safe on this host.
    for kind in ("quote", "price_bar"):
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS raw.{kind}_fact_availability (
                fact_id BIGINT NOT NULL,
                fact_available_at TIMESTAMPTZ NOT NULL,
                ingest_run_id UUID NOT NULL REFERENCES ingest.run(id),
                PRIMARY KEY (fact_id, fact_available_at)
            )
            """
        )
    op.execute(current_price_selector_sql(use_availability_projection=True))


def downgrade() -> None:
    op.execute(current_price_selector_sql(use_availability_projection=False))
    op.execute("DROP TABLE IF EXISTS raw.price_bar_fact_availability")
    op.execute("DROP TABLE IF EXISTS raw.quote_fact_availability")
