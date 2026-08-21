"""Add the point-in-time Event Decision Packet and shared decision truth.

Revision ID: 20260820_0042
Revises: 20260819_0041
"""

from __future__ import annotations

from alembic import op


revision = "20260820_0042"
down_revision = "20260819_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE analysis.event_decision_packet (
            event_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            event_kind TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            as_of TIMESTAMPTZ NOT NULL,
            publication_id TEXT,
            headline TEXT,
            market_tape JSONB NOT NULL DEFAULT '{}'::jsonb,
            positioning JSONB NOT NULL DEFAULT '{}'::jsonb,
            event_fundamentals JSONB NOT NULL DEFAULT '{}'::jsonb,
            platform_optionality JSONB NOT NULL DEFAULT '{}'::jsonb,
            historical_cases JSONB NOT NULL DEFAULT '{}'::jsonb,
            tactical_decision JSONB NOT NULL DEFAULT '{}'::jsonb,
            fundamental_decision JSONB NOT NULL DEFAULT '{}'::jsonb,
            decision_truth JSONB NOT NULL DEFAULT '{}'::jsonb,
            evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_event_decision_packet_json_objects CHECK (
                jsonb_typeof(market_tape) = 'object'
                AND jsonb_typeof(positioning) = 'object'
                AND jsonb_typeof(event_fundamentals) = 'object'
                AND jsonb_typeof(platform_optionality) = 'object'
                AND jsonb_typeof(tactical_decision) = 'object'
                AND jsonb_typeof(fundamental_decision) = 'object'
                AND jsonb_typeof(decision_truth) = 'object'
            )
        );
        CREATE INDEX ix_event_decision_packet_symbol_as_of
            ON analysis.event_decision_packet (symbol, as_of DESC);

        CREATE TABLE analysis.event_scout_event (
            event_id TEXT PRIMARY KEY REFERENCES analysis.event_decision_packet(event_id) ON DELETE CASCADE,
            symbol TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            source_url TEXT,
            source_kind TEXT,
            status TEXT NOT NULL,
            cooldown_until TIMESTAMPTZ,
            collection_status JSONB NOT NULL DEFAULT '{}'::jsonb,
            raw JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE INDEX ix_event_scout_event_symbol_observed
            ON analysis.event_scout_event (symbol, observed_at DESC);

        CREATE TABLE app.decision_truth (
            symbol TEXT NOT NULL,
            lane TEXT NOT NULL,
            as_of TIMESTAMPTZ NOT NULL,
            publication_id TEXT,
            candidate_state TEXT,
            route_verdict TEXT,
            readiness_state TEXT,
            execution_state TEXT,
            primary_blocker TEXT,
            blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
            next_action TEXT,
            route_version TEXT,
            evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            event_id TEXT REFERENCES analysis.event_decision_packet(event_id) ON DELETE SET NULL,
            raw JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, lane),
            CONSTRAINT ck_decision_truth_json_arrays CHECK (
                jsonb_typeof(blockers) = 'array' AND jsonb_typeof(evidence_refs) = 'array'
            )
        );
        CREATE INDEX ix_decision_truth_as_of ON app.decision_truth (as_of DESC, symbol);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE app.decision_truth;
        DROP TABLE analysis.event_scout_event;
        DROP TABLE analysis.event_decision_packet;
        """
    )
