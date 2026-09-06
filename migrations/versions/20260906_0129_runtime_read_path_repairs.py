"""Repair application-role access and the option-history evidence join path."""

from alembic import op


revision = "20260906_0129"
down_revision = "20260906_0128"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA analysis, app TO market_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON analysis.agent_experiment TO market_app")
    op.execute("GRANT SELECT, INSERT ON app.thesis_review_event TO market_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.option_history_policy TO market_app")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_option_relative_value_generation_contract "
        "ON analysis.option_relative_value (capture_generation_id, contract_id, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS analysis.ix_option_relative_value_generation_contract")
    op.execute("REVOKE SELECT, INSERT, UPDATE ON app.option_history_policy FROM market_app")
    op.execute("REVOKE SELECT, INSERT ON app.thesis_review_event FROM market_app")
    op.execute("REVOKE SELECT, INSERT, UPDATE ON analysis.agent_experiment FROM market_app")
