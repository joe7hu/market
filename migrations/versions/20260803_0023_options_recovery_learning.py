"""Add full-denominator counterfactual observations for recovery learning.

Revision ID: 20260803_0023
Revises: 20260803_0022
"""

from __future__ import annotations

from alembic import op


revision = "20260803_0023"
down_revision = "20260803_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE analysis.option_opportunity_observation (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            event_id UUID NOT NULL REFERENCES analysis.option_event(id) ON DELETE CASCADE,
            capture_id UUID REFERENCES analysis.option_event_capture(id) ON DELETE SET NULL,
            capture_generation_id BIGINT REFERENCES raw.option_capture_generation(id) ON DELETE SET NULL,
            capture_generation_key TEXT NOT NULL,
            event_contract_id BIGINT REFERENCES analysis.option_event_contract(id) ON DELETE SET NULL,
            contract_id BIGINT NOT NULL REFERENCES catalog.option_contract(id) ON DELETE RESTRICT,
            strategy_key TEXT NOT NULL,
            strategy_revision_id BIGINT REFERENCES analysis.strategy_revision(id) ON DELETE SET NULL,
            objective_version TEXT NOT NULL DEFAULT 'short_horizon_convex_v1',
            observed_at TIMESTAMPTZ NOT NULL,
            available_at TIMESTAMPTZ NOT NULL,
            expiration DATE NOT NULL,
            quote JSONB NOT NULL DEFAULT '{}'::jsonb,
            liquid BOOLEAN NOT NULL,
            data_status TEXT NOT NULL DEFAULT 'ok',
            selection_stage TEXT NOT NULL DEFAULT 'observed',
            miss_reason TEXT,
            signal_id UUID REFERENCES analysis.option_event_signal(id) ON DELETE SET NULL,
            paper_order_id UUID REFERENCES app.paper_order(id) ON DELETE SET NULL,
            selection_score DOUBLE PRECISION,
            lower_confidence_expectancy DOUBLE PRECISION,
            entry_fill_at TIMESTAMPTZ,
            entry_fill_price NUMERIC(20, 6),
            return_1_session DOUBLE PRECISION,
            return_3_session DOUBLE PRECISION,
            return_5_session DOUBLE PRECISION,
            return_10_session DOUBLE PRECISION,
            time_to_2x_sessions INTEGER,
            time_to_3x_sessions INTEGER,
            time_to_4x_sessions INTEGER,
            executable_peak_return DOUBLE PRECISION,
            realized_return DOUBLE PRECISION,
            mae DOUBLE PRECISION,
            giveback DOUBLE PRECISION,
            exit_efficiency DOUBLE PRECISION,
            exit_fill_at TIMESTAMPTZ,
            exit_fill_price NUMERIC(20, 6),
            outcome_classification TEXT NOT NULL DEFAULT 'observing',
            measured_through TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_option_opportunity_data_status CHECK (data_status IN (
                'ok', 'stale_quote', 'continuity_missing', 'lookahead_blocked'
            )),
            CONSTRAINT ck_option_opportunity_selection_stage CHECK (selection_stage IN (
                'observed', 'eligible', 'ranked_out', 'published', 'ticketed', 'filled', 'exited'
            )),
            CONSTRAINT ck_option_opportunity_miss_reason CHECK (miss_reason IS NULL OR miss_reason IN (
                'not_featured', 'gate_reject', 'ranked_out', 'not_published', 'unfilled',
                'risk_blocked', 'captured', 'unmeasurable'
            )),
            CONSTRAINT ck_option_opportunity_classification CHECK (outcome_classification IN (
                'observing', 'captured', 'missed', 'unfilled', 'unmeasurable'
            )),
            -- Provider quote timestamps may be sequence-stamped just after a
            -- capture finishes; only the explicit availability timestamp is
            -- used for point-in-time decisions.
            CONSTRAINT ck_option_opportunity_availability CHECK (available_at IS NOT NULL),
            UNIQUE (event_id, capture_generation_key, contract_id, strategy_key, strategy_revision_id)
        );
        CREATE INDEX ix_option_opportunity_event_selection
        ON analysis.option_opportunity_observation (event_id, strategy_key, selection_stage, available_at DESC);
        CREATE INDEX ix_option_opportunity_measurement
        ON analysis.option_opportunity_observation (strategy_key, outcome_classification, measured_through DESC);
        CREATE INDEX ix_option_opportunity_signal
        ON analysis.option_opportunity_observation (signal_id, paper_order_id)
        WHERE signal_id IS NOT NULL OR paper_order_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS analysis.ix_option_opportunity_signal")
    op.execute("DROP INDEX IF EXISTS analysis.ix_option_opportunity_measurement")
    op.execute("DROP INDEX IF EXISTS analysis.ix_option_opportunity_event_selection")
    op.execute("DROP TABLE IF EXISTS analysis.option_opportunity_observation")
