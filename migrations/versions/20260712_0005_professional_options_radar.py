"""Professional options-radar contracts and cash-secured-put support.

Revision ID: 20260712_0005
Revises: 20260711_0004
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260712_0005"
down_revision = "20260711_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("option_decision", sa.Column("structure", sa.Text(), nullable=False, server_default="long_option"), schema="analysis")
    op.add_column("option_decision", sa.Column("entry_price", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_decision", sa.Column("exit_cost_estimate", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_decision", sa.Column("secured_cash", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_decision", sa.Column("max_profit", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_decision", sa.Column("max_loss", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_decision", sa.Column("break_even", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_decision", sa.Column("effective_assignment_price", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_decision", sa.Column("probability_profit", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_decision", sa.Column("probability_assignment", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_decision", sa.Column("probability_touch", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_decision", sa.Column("expected_value", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_decision", sa.Column("risk_adjusted_expectancy", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_decision", sa.Column("tail_cvar", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_decision", sa.Column("data_confidence", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_decision", sa.Column("execution_confidence", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_decision", sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'::jsonb")), schema="analysis")

    op.add_column("paper_order", sa.Column("structure", sa.Text(), nullable=True), schema="app")
    op.add_column("paper_order", sa.Column("reserved_collateral", sa.Numeric(24, 4), nullable=True), schema="app")
    op.add_column("paper_order", sa.Column("idempotency_key", sa.Text(), nullable=True), schema="app")
    op.create_unique_constraint("uq_app_paper_order_idempotency", "paper_order", ["idempotency_key"], schema="app")

    op.add_column("option_outcome", sa.Column("paper_status", sa.Text(), nullable=True), schema="analysis")
    op.add_column("option_outcome", sa.Column("credit_captured", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_outcome", sa.Column("collateral_return", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_outcome", sa.Column("assigned_basis", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_outcome", sa.Column("strike_touched", sa.Boolean(), nullable=True), schema="analysis")
    op.add_column("option_outcome", sa.Column("assignment_return_1d", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_outcome", sa.Column("assignment_return_5d", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_outcome", sa.Column("assignment_return_20d", sa.Double(), nullable=True), schema="analysis")
    op.add_column("option_outcome", sa.Column("assignment_return_60d", sa.Double(), nullable=True), schema="analysis")

    op.create_index("ix_analysis_option_decision_structure", "option_decision", ["structure"], schema="analysis")


def downgrade() -> None:
    op.drop_index("ix_analysis_option_decision_structure", table_name="option_decision", schema="analysis")
    for column in (
        "assignment_return_60d", "assignment_return_20d", "assignment_return_5d", "assignment_return_1d",
        "strike_touched", "assigned_basis", "collateral_return", "credit_captured", "paper_status",
    ):
        op.drop_column("option_outcome", column, schema="analysis")
    op.drop_constraint("uq_app_paper_order_idempotency", "paper_order", schema="app", type_="unique")
    for column in ("idempotency_key", "reserved_collateral", "structure"):
        op.drop_column("paper_order", column, schema="app")
    for column in (
        "details", "execution_confidence", "data_confidence", "tail_cvar", "risk_adjusted_expectancy",
        "expected_value", "probability_touch", "probability_assignment", "probability_profit",
        "effective_assignment_price", "break_even", "max_loss", "max_profit", "secured_cash",
        "exit_cost_estimate", "entry_price", "structure",
    ):
        op.drop_column("option_decision", column, schema="analysis")
