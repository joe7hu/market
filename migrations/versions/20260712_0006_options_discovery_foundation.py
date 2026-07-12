"""Options-radar discovery manifests and honest quote readiness.

Revision ID: 20260712_0006
Revises: 20260712_0005
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260712_0006"
down_revision = "20260712_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("option_quote", sa.Column("bid_size", sa.BigInteger(), nullable=True), schema="raw")
    op.add_column("option_quote", sa.Column("ask_size", sa.BigInteger(), nullable=True), schema="raw")
    op.add_column("option_quote", sa.Column("last_trade_at", sa.DateTime(timezone=True), nullable=True), schema="raw")
    op.add_column("option_quote", sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True), schema="raw")
    op.add_column("option_quote", sa.Column("market_data_status", sa.Text(), nullable=True), schema="raw")

    op.create_table(
        "option_discovery_run",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("analysis.run.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("universe_hash", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("market_session", sa.Text(), nullable=True),
        sa.Column("symbols_considered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("symbols_with_chains", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("contracts_evaluated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("manifest", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        schema="analysis",
    )
    op.create_table(
        "option_discovery_candidate",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("analysis.option_discovery_run.run_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("instrument_id", sa.BigInteger(), sa.ForeignKey("catalog.instrument.id"), primary_key=True),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("discovery_score", sa.Double(), nullable=False),
        sa.Column("surface_reason", sa.Text(), nullable=False),
        sa.Column("primary_edge", sa.Text(), nullable=False),
        sa.Column("causal_exposure", sa.Text(), nullable=False),
        sa.Column("catalyst_start", sa.Date(), nullable=True),
        sa.Column("catalyst_end", sa.Date(), nullable=True),
        sa.Column("earliest_signal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timeliness", sa.Text(), nullable=False),
        sa.Column("source_root_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_completeness", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("data_readiness", sa.Text(), nullable=False),
        sa.Column("execution_ready", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("next_evidence", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("stage IN ('DISCOVERED','UNDERWRITING','STRUCTURED','PUBLISHED')", name="ck_option_discovery_stage"),
        sa.CheckConstraint("data_readiness IN ('A','B','C','D')", name="ck_option_discovery_readiness"),
        sa.CheckConstraint("evidence_completeness BETWEEN 0 AND 5", name="ck_option_discovery_evidence"),
        schema="analysis",
    )
    op.create_table(
        "option_gate_result",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("gate_code", sa.Text(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.ForeignKeyConstraint(
            ["run_id", "instrument_id"],
            ["analysis.option_discovery_candidate.run_id", "analysis.option_discovery_candidate.instrument_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "instrument_id", "gate_code"),
        schema="analysis",
    )


def downgrade() -> None:
    op.drop_table("option_gate_result", schema="analysis")
    op.drop_table("option_discovery_candidate", schema="analysis")
    op.drop_table("option_discovery_run", schema="analysis")
    op.drop_column("option_quote", "last_trade_at", schema="raw")
    op.drop_column("option_quote", "captured_at", schema="raw")
    op.drop_column("option_quote", "market_data_status", schema="raw")
    op.drop_column("option_quote", "ask_size", schema="raw")
    op.drop_column("option_quote", "bid_size", schema="raw")
