"""Support bounded analysis-run retention cascades."""

from __future__ import annotations

from alembic import op


revision = "20260821_0045"
down_revision = "20260821_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL checks child foreign keys during a parent delete.  These
    # indexes keep the bounded analysis.run cascade proportional to the rows
    # being removed instead of rescanning each analytical child table.
    for statement in (
        "CREATE INDEX IF NOT EXISTS ix_analysis_option_decision_relative_value_id ON analysis.option_decision (relative_value_id)",
        "CREATE INDEX IF NOT EXISTS ix_analysis_option_decision_primary_decision_id ON analysis.option_decision (primary_decision_id)",
        "CREATE INDEX IF NOT EXISTS ix_analysis_agent_task_decision_id ON analysis.agent_task (decision_id)",
        "CREATE INDEX IF NOT EXISTS ix_analysis_option_event_signal_decision_id ON analysis.option_event_signal (decision_id)",
        "CREATE INDEX IF NOT EXISTS ix_app_alert_decision_id ON app.alert (decision_id)",
        "CREATE INDEX IF NOT EXISTS ix_app_paper_order_decision_id ON app.paper_order (decision_id)",
        "CREATE INDEX IF NOT EXISTS ix_app_trade_journal_decision_id ON app.trade_journal (decision_id)",
        "CREATE INDEX IF NOT EXISTS ix_app_publication_analysis_run_id ON app.publication (analysis_run_id)",
    ):
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        "DROP INDEX IF EXISTS ix_app_publication_analysis_run_id",
        "DROP INDEX IF EXISTS ix_app_trade_journal_decision_id",
        "DROP INDEX IF EXISTS ix_app_paper_order_decision_id",
        "DROP INDEX IF EXISTS ix_app_alert_decision_id",
        "DROP INDEX IF EXISTS ix_analysis_option_event_signal_decision_id",
        "DROP INDEX IF EXISTS ix_analysis_agent_task_decision_id",
        "DROP INDEX IF EXISTS ix_analysis_option_decision_primary_decision_id",
        "DROP INDEX IF EXISTS ix_analysis_option_decision_relative_value_id",
    ):
        op.execute(statement)
