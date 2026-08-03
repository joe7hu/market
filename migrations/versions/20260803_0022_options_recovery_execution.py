"""Add typed recovery signals and immutable v4 paper-order attribution.

Revision ID: 20260803_0022
Revises: 20260803_0021
"""

from __future__ import annotations

from alembic import op


revision = "20260803_0022"
down_revision = "20260803_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE analysis.option_event_signal (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            event_id UUID NOT NULL REFERENCES analysis.option_event(id) ON DELETE CASCADE,
            event_contract_id BIGINT REFERENCES analysis.option_event_contract(id) ON DELETE SET NULL,
            capture_id UUID REFERENCES analysis.option_event_capture(id) ON DELETE SET NULL,
            decision_id UUID REFERENCES analysis.decision(id) ON DELETE SET NULL,
            snapshot_id BIGINT REFERENCES raw.option_snapshot(id) ON DELETE SET NULL,
            contract_id BIGINT NOT NULL REFERENCES catalog.option_contract(id) ON DELETE RESTRICT,
            strategy_key TEXT NOT NULL,
            strategy_revision_id BIGINT REFERENCES analysis.strategy_revision(id) ON DELETE SET NULL,
            objective_version TEXT NOT NULL DEFAULT 'short_horizon_convex_v1',
            status TEXT NOT NULL DEFAULT 'shadow',
            signal_at TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL,
            selection_score DOUBLE PRECISION,
            lower_confidence_expectancy DOUBLE PRECISION,
            maximum_loss NUMERIC(20, 6),
            gate_result JSONB NOT NULL DEFAULT '{}'::jsonb,
            ticket JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_option_event_signal_status CHECK (status IN (
                'shadow', 'ticketed', 'risk_blocked', 'stale', 'unfilled', 'entered',
                'partial_exited', 'exited', 'invalidated', 'unmeasurable', 'rejected'
            )),
            -- A decision may only be created after its source quote became
            -- available; the later paper fill still requires a future capture.
            CONSTRAINT ck_option_event_signal_availability CHECK (signal_at >= available_at),
            UNIQUE (event_id, event_contract_id, capture_id, strategy_key, strategy_revision_id)
        );
        CREATE INDEX ix_option_event_signal_event_status
        ON analysis.option_event_signal (event_id, status, available_at DESC);
        CREATE INDEX ix_option_event_signal_contract_family
        ON analysis.option_event_signal (contract_id, strategy_key, available_at DESC);

        ALTER TABLE app.paper_order
          ADD COLUMN event_id UUID REFERENCES analysis.option_event(id) ON DELETE SET NULL,
          ADD COLUMN event_signal_id UUID REFERENCES analysis.option_event_signal(id) ON DELETE SET NULL,
          ADD COLUMN strategy_family TEXT,
          ADD COLUMN objective_version TEXT,
          ADD COLUMN entry_capture_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE app.paper_order
          ADD CONSTRAINT ck_paper_order_entry_capture_count CHECK (entry_capture_count >= 0);
        CREATE UNIQUE INDEX uq_recovery_paper_order_event_family
        ON app.paper_order (event_id, strategy_family)
        WHERE event_id IS NOT NULL AND strategy_family IS NOT NULL;
        CREATE INDEX ix_recovery_paper_order_signal
        ON app.paper_order (event_signal_id, status, created_at DESC)
        WHERE event_signal_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.ix_recovery_paper_order_signal")
    op.execute("DROP INDEX IF EXISTS app.uq_recovery_paper_order_event_family")
    op.execute("ALTER TABLE app.paper_order DROP CONSTRAINT IF EXISTS ck_paper_order_entry_capture_count")
    for column in ("entry_capture_count", "objective_version", "strategy_family", "event_signal_id", "event_id"):
        op.execute(f"ALTER TABLE app.paper_order DROP COLUMN IF EXISTS {column}")
    op.execute("DROP INDEX IF EXISTS analysis.ix_option_event_signal_contract_family")
    op.execute("DROP INDEX IF EXISTS analysis.ix_option_event_signal_event_status")
    op.execute("DROP TABLE IF EXISTS analysis.option_event_signal")
