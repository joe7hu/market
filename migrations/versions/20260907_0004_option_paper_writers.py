"""Allow the application role to record the existing option paper lifecycle."""

from alembic import op

revision = "20260907_0004"
down_revision = "20260907_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep decision, source, ticket and policy identities outside UPDATE grants.
    op.execute("""GRANT UPDATE (
        status, pending_entry_reason, entry_at, entry_price,
        exit_at, exit_price, fill_basis, metrics
    ) ON analysis.shadow_trade TO market_app""")
    op.execute("""GRANT INSERT (
        decision_id, instrument_id, side, quantity, limit_price, status,
        policy_result, policy_snapshot, lane, structure, reserved_collateral,
        idempotency_key, ticket_version, ticket_snapshot, intended_limit_price
    ) ON app.paper_order TO market_app""")
    op.execute("""GRANT INSERT (
        paper_order_id, leg_index, contract_id, option_type, side, strike,
        bid, ask, bid_size, ask_size, quote_time, open_interest, volume
    ) ON app.paper_order_leg TO market_app""")
    op.execute("""GRANT UPDATE (
        status, actual_fill_price, filled_at, fill_evidence_at, execution_quote,
        contract_multiplier, filled_quantity, fees, entry_fees, entry_slippage,
        updated_at, unfilled_reason, submitted_at, exited_quantity, exit_price,
        exit_at, exit_fees, exit_slippage
    ) ON app.paper_order TO market_app""")


def downgrade() -> None:
    op.execute("""REVOKE UPDATE (
        status, actual_fill_price, filled_at, fill_evidence_at, execution_quote,
        contract_multiplier, filled_quantity, fees, entry_fees, entry_slippage,
        updated_at, unfilled_reason, submitted_at, exited_quantity, exit_price,
        exit_at, exit_fees, exit_slippage
    ) ON app.paper_order FROM market_app""")
    op.execute("""REVOKE INSERT (
        paper_order_id, leg_index, contract_id, option_type, side, strike,
        bid, ask, bid_size, ask_size, quote_time, open_interest, volume
    ) ON app.paper_order_leg FROM market_app""")
    op.execute("""REVOKE INSERT (
        decision_id, instrument_id, side, quantity, limit_price, status,
        policy_result, policy_snapshot, lane, structure, reserved_collateral,
        idempotency_key, ticket_version, ticket_snapshot, intended_limit_price
    ) ON app.paper_order FROM market_app""")
    op.execute("""REVOKE UPDATE (
        status, pending_entry_reason, entry_at, entry_price,
        exit_at, exit_price, fill_basis, metrics
    ) ON analysis.shadow_trade FROM market_app""")
