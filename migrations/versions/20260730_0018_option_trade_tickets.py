"""Shared option trade tickets and immutable paper-order attribution.

Revision ID: 20260730_0018
Revises: 20260725_0017
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260730_0018"
down_revision = "20260725_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO analysis.option_history_canary (model_revision)
        SELECT 'history-v3-price-shape-r4-ticket'
        WHERE NOT EXISTS (
            SELECT 1 FROM analysis.option_history_canary
            WHERE model_revision = 'history-v3-price-shape-r4-ticket'
        )
        """
    )
    op.add_column("paper_order", sa.Column("ticket_version", sa.Integer(), nullable=True), schema="app")
    op.add_column(
        "paper_order",
        sa.Column("ticket_snapshot", postgresql.JSONB(), nullable=True),
        schema="app",
    )
    op.add_column("paper_order", sa.Column("intended_limit_price", sa.Numeric(20, 6), nullable=True), schema="app")
    op.add_column("paper_order", sa.Column("actual_fill_price", sa.Numeric(20, 6), nullable=True), schema="app")
    op.add_column("paper_order", sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True), schema="app")
    op.add_column(
        "agent_task",
        sa.Column("result_available_at", sa.DateTime(timezone=True), nullable=True),
        schema="analysis",
    )
    op.add_column(
        "agent_task",
        sa.Column("validation_available_at", sa.DateTime(timezone=True), nullable=True),
        schema="analysis",
    )
    op.execute(
        """
        UPDATE analysis.agent_task
        SET result_available_at = updated_at
        WHERE result IS NOT NULL;
        UPDATE analysis.agent_task
        SET validation_available_at = updated_at
        WHERE validation IS NOT NULL;

        CREATE OR REPLACE FUNCTION analysis.stamp_agent_task_payload_availability()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'INSERT' THEN
            NEW.result_available_at := CASE WHEN NEW.result IS NULL THEN NULL ELSE now() END;
            NEW.validation_available_at := CASE WHEN NEW.validation IS NULL THEN NULL ELSE now() END;
            RETURN NEW;
          END IF;
          IF OLD.result IS NOT NULL AND NEW.result IS NULL THEN
            RAISE EXCEPTION 'published agent result cannot be cleared';
          END IF;
          IF OLD.validation IS NOT NULL AND NEW.validation IS NULL THEN
            RAISE EXCEPTION 'published agent validation cannot be cleared';
          END IF;
          NEW.result_available_at := CASE
            WHEN OLD.result_available_at IS NOT NULL THEN OLD.result_available_at
            WHEN NEW.result IS NOT NULL THEN now()
            ELSE NULL
          END;
          NEW.validation_available_at := CASE
            WHEN OLD.validation_available_at IS NOT NULL THEN OLD.validation_available_at
            WHEN NEW.validation IS NOT NULL THEN now()
            ELSE NULL
          END;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER agent_task_payload_availability
        BEFORE INSERT OR UPDATE ON analysis.agent_task
        FOR EACH ROW EXECUTE FUNCTION analysis.stamp_agent_task_payload_availability();
        """
    )
    op.create_table(
        "paper_order_leg",
        sa.Column(
            "paper_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("app.paper_order.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("leg_index", sa.Integer(), primary_key=True),
        sa.Column("contract_id", sa.BigInteger(), nullable=False),
        sa.Column("option_type", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("strike", sa.Numeric(20, 6), nullable=False),
        sa.Column("bid", sa.Numeric(20, 6), nullable=False),
        sa.Column("ask", sa.Numeric(20, 6), nullable=False),
        sa.Column("bid_size", sa.Integer(), nullable=False),
        sa.Column("ask_size", sa.Integer(), nullable=False),
        sa.Column("quote_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_interest", sa.Integer(), nullable=True),
        sa.Column("volume", sa.Integer(), nullable=True),
        schema="app",
    )
    op.create_table(
        "review_page_snapshot",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rows", postgresql.JSONB(), nullable=False),
        schema="app",
    )
    op.create_index(
        "ix_review_page_snapshot_expires_at",
        "review_page_snapshot",
        ["expires_at"],
        schema="app",
    )
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
        CREATE TRIGGER paper_order_ticket_immutable
        BEFORE UPDATE OR DELETE ON app.paper_order
        FOR EACH ROW EXECUTE FUNCTION app.prevent_paper_order_ticket_mutation();

        CREATE OR REPLACE FUNCTION app.prevent_paper_order_leg_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          target_order_id uuid;
          expected jsonb;
        BEGIN
          IF TG_OP = 'UPDATE' AND (
            NEW.paper_order_id IS DISTINCT FROM OLD.paper_order_id
            OR NEW.leg_index IS DISTINCT FROM OLD.leg_index
          ) THEN
            RAISE EXCEPTION 'paper order leg identity is immutable';
          END IF;
          target_order_id := CASE WHEN TG_OP = 'INSERT'
            THEN NEW.paper_order_id ELSE OLD.paper_order_id END;
          IF EXISTS (
            SELECT 1 FROM app.paper_order
            WHERE id = target_order_id AND ticket_version IS NOT NULL
          ) THEN
            IF TG_OP = 'INSERT' THEN
              SELECT ticket_snapshot->'legs'->NEW.leg_index
              INTO expected
              FROM app.paper_order
              WHERE id = NEW.paper_order_id;
              IF expected IS NULL OR ROW(
                expected->>'contract_id',
                expected->>'option_type',
                expected->>'side',
                (expected->>'strike')::numeric,
                (expected->>'bid')::numeric,
                (expected->>'ask')::numeric,
                (expected->>'bid_size')::integer,
                (expected->>'ask_size')::integer,
                (expected->>'quote_time')::timestamptz,
                (expected->>'open_interest')::integer,
                (expected->>'volume')::integer
              ) IS DISTINCT FROM ROW(
                NEW.contract_id::text,
                NEW.option_type,
                NEW.side,
                NEW.strike,
                NEW.bid,
                NEW.ask,
                NEW.bid_size,
                NEW.ask_size,
                NEW.quote_time,
                NEW.open_interest,
                NEW.volume
              ) THEN
                RAISE EXCEPTION 'paper order leg does not match immutable ticket';
              END IF;
              RETURN NEW;
            END IF;
            RAISE EXCEPTION 'ticketed paper order legs are append-only';
          END IF;
          RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;
        CREATE TRIGGER paper_order_leg_append_only
        BEFORE INSERT OR UPDATE OR DELETE ON app.paper_order_leg
        FOR EACH ROW EXECUTE FUNCTION app.prevent_paper_order_leg_mutation();

        CREATE OR REPLACE FUNCTION app.require_complete_paper_order_leg_set()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          expected_count integer;
          actual_count integer;
        BEGIN
          IF NEW.ticket_version IS NULL THEN
            RETURN NEW;
          END IF;
          expected_count := jsonb_array_length(coalesce(NEW.ticket_snapshot->'legs', '[]'::jsonb));
          SELECT count(*) INTO actual_count
          FROM app.paper_order_leg
          WHERE paper_order_id = NEW.id;
          IF actual_count IS DISTINCT FROM expected_count THEN
            RAISE EXCEPTION 'ticketed paper order requires the complete immutable leg set';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER paper_order_leg_set_complete
        AFTER INSERT OR UPDATE ON app.paper_order
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION app.require_complete_paper_order_leg_set();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS paper_order_leg_set_complete ON app.paper_order")
    op.execute("DROP FUNCTION IF EXISTS app.require_complete_paper_order_leg_set()")
    op.execute("DROP TRIGGER IF EXISTS paper_order_leg_append_only ON app.paper_order_leg")
    op.execute("DROP FUNCTION IF EXISTS app.prevent_paper_order_leg_mutation()")
    op.execute("DROP TRIGGER IF EXISTS paper_order_ticket_immutable ON app.paper_order")
    op.execute("DROP FUNCTION IF EXISTS app.prevent_paper_order_ticket_mutation()")
    op.drop_index(
        "ix_review_page_snapshot_expires_at",
        table_name="review_page_snapshot",
        schema="app",
    )
    op.drop_table("review_page_snapshot", schema="app")
    op.drop_table("paper_order_leg", schema="app")
    op.execute("DROP TRIGGER IF EXISTS agent_task_payload_availability ON analysis.agent_task")
    op.execute("DROP FUNCTION IF EXISTS analysis.stamp_agent_task_payload_availability()")
    op.drop_column("agent_task", "validation_available_at", schema="analysis")
    op.drop_column("agent_task", "result_available_at", schema="analysis")
    for column in (
        "filled_at",
        "actual_fill_price",
        "intended_limit_price",
        "ticket_snapshot",
        "ticket_version",
    ):
        op.drop_column("paper_order", column, schema="app")
    op.execute(
        "DELETE FROM analysis.option_history_canary "
        "WHERE model_revision = 'history-v3-price-shape-r4-ticket'"
    )
