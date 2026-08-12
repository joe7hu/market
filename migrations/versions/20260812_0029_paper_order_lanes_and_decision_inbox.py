"""Add lane-owned paper lifecycle metadata and decision notification outbox.

Revision ID: 20260812_0029
Revises: 20260812_0028
"""

from __future__ import annotations

from alembic import op


revision = "20260812_0029"
down_revision = "20260812_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.paper_order
          ADD COLUMN IF NOT EXISTS lane TEXT NOT NULL DEFAULT 'radar',
          ADD COLUMN IF NOT EXISTS policy_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN IF NOT EXISTS exit_at TIMESTAMPTZ,
          ADD COLUMN IF NOT EXISTS exit_price NUMERIC(20, 6),
          ADD COLUMN IF NOT EXISTS fees NUMERIC(20, 6) NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

        UPDATE app.paper_order
        SET lane = CASE
          WHEN event_id IS NOT NULL THEN 'recovery'
          WHEN lower(coalesce(ticket_snapshot->>'lane', '')) = 'qqq' THEN 'qqq'
          ELSE 'radar'
        END
        WHERE lane NOT IN ('radar', 'qqq', 'recovery')
           OR lane = 'radar';

        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_app_paper_order_lane'
              AND conrelid = 'app.paper_order'::regclass
          ) THEN
            ALTER TABLE app.paper_order
              ADD CONSTRAINT ck_app_paper_order_lane
              CHECK (lane IN ('radar', 'qqq', 'recovery'));
          END IF;
        END $$;

        CREATE INDEX IF NOT EXISTS ix_app_paper_order_lane_status_created
          ON app.paper_order (lane, status, created_at DESC);

        -- Ticket intent includes its lane and policy snapshot.  Lifecycle
        -- fields (status, fills, exits, fees) stay mutable, but neither a
        -- scheduler nor a repair may later rewrite the decision that was
        -- accepted for paper execution.
        CREATE OR REPLACE FUNCTION app.prevent_paper_order_ticket_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.ticket_version IS NOT NULL THEN
            IF TG_OP = 'DELETE' THEN
              RAISE EXCEPTION 'ticketed paper order audit is append-only';
            END IF;
            IF ROW(
              NEW.decision_id, NEW.instrument_id, NEW.side, NEW.quantity,
              NEW.limit_price, NEW.intended_limit_price, NEW.structure,
              NEW.reserved_collateral, NEW.idempotency_key,
              NEW.ticket_version, NEW.ticket_snapshot,
              NEW.policy_result, NEW.policy_snapshot, NEW.lane, NEW.created_at
            ) IS DISTINCT FROM ROW(
              OLD.decision_id, OLD.instrument_id, OLD.side, OLD.quantity,
              OLD.limit_price, OLD.intended_limit_price, OLD.structure,
              OLD.reserved_collateral, OLD.idempotency_key,
              OLD.ticket_version, OLD.ticket_snapshot,
              OLD.policy_result, OLD.policy_snapshot, OLD.lane, OLD.created_at
            ) THEN
              RAISE EXCEPTION 'ticketed paper order intent is immutable';
            END IF;
          END IF;
          RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;

        CREATE TABLE app.decision_inbox_item (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          dedupe_key TEXT NOT NULL UNIQUE,
          event_type TEXT NOT NULL CHECK (event_type IN (
            'ready', 'revoked', 'expired', 'paper_filled', 'paper_exited',
            'portfolio_critical', 'paper_engine_halt'
          )),
          opportunity_id UUID,
          ticket_version INTEGER,
          paper_order_id UUID REFERENCES app.paper_order(id) ON DELETE RESTRICT,
          lane TEXT,
          severity TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info', 'warning', 'critical')),
          status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'resolved')),
          payload JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          resolved_at TIMESTAMPTZ
        );
        CREATE INDEX ix_decision_inbox_active_created
          ON app.decision_inbox_item (status, created_at DESC, id DESC);
        CREATE INDEX ix_decision_inbox_opportunity
          ON app.decision_inbox_item (opportunity_id, ticket_version, event_type);

        CREATE TABLE app.notification_outbox (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          dedupe_key TEXT NOT NULL UNIQUE,
          inbox_item_id UUID NOT NULL REFERENCES app.decision_inbox_item(id) ON DELETE RESTRICT,
          channel TEXT NOT NULL DEFAULT 'telegram_owner'
            CHECK (channel = 'telegram_owner'),
          event_type TEXT NOT NULL,
          payload JSONB NOT NULL DEFAULT '{}'::jsonb,
          status TEXT NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued', 'sending', 'sent', 'failed', 'dry_run')),
          attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
          next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_error TEXT,
          sent_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_notification_outbox_due
          ON app.notification_outbox (status, next_attempt_at, created_at)
          WHERE status IN ('queued', 'failed');
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.ix_notification_outbox_due")
    op.execute("DROP TABLE IF EXISTS app.notification_outbox")
    op.execute("DROP INDEX IF EXISTS app.ix_decision_inbox_opportunity")
    op.execute("DROP INDEX IF EXISTS app.ix_decision_inbox_active_created")
    op.execute("DROP TABLE IF EXISTS app.decision_inbox_item")
    op.execute("DROP INDEX IF EXISTS app.ix_app_paper_order_lane_status_created")
    op.execute("ALTER TABLE app.paper_order DROP CONSTRAINT IF EXISTS ck_app_paper_order_lane")
    # Restore the 0018 trigger body before dropping fields that this revision
    # added.  Leaving the replacement body in place would make a downgrade
    # fail later when the legacy trigger fires.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.prevent_paper_order_ticket_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.ticket_version IS NOT NULL THEN
            IF TG_OP = 'DELETE' THEN
              RAISE EXCEPTION 'ticketed paper order audit is append-only';
            END IF;
            IF ROW(
              NEW.decision_id, NEW.instrument_id, NEW.side, NEW.quantity,
              NEW.limit_price, NEW.intended_limit_price, NEW.structure,
              NEW.reserved_collateral, NEW.idempotency_key,
              NEW.ticket_version, NEW.ticket_snapshot,
              NEW.policy_result, NEW.created_at
            ) IS DISTINCT FROM ROW(
              OLD.decision_id, OLD.instrument_id, OLD.side, OLD.quantity,
              OLD.limit_price, OLD.intended_limit_price, OLD.structure,
              OLD.reserved_collateral, OLD.idempotency_key,
              OLD.ticket_version, OLD.ticket_snapshot,
              OLD.policy_result, OLD.created_at
            ) THEN
              RAISE EXCEPTION 'ticketed paper order intent is immutable';
            END IF;
          END IF;
          RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;
        """
    )
    for column in ("updated_at", "fees", "exit_price", "exit_at", "policy_snapshot", "lane"):
        op.execute(f"ALTER TABLE app.paper_order DROP COLUMN IF EXISTS {column}")
