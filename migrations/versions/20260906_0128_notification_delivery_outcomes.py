"""Preserve uncertain notification delivery without duplicate retries."""
from alembic import op

revision = "20260906_0128"
down_revision = "20260906_0127"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT INSERT ON app.decision_inbox_item, app.notification_outbox TO market_app")
    op.execute("GRANT UPDATE (status, resolved_at) ON app.decision_inbox_item TO market_app")
    op.execute("GRANT UPDATE (status, attempts, next_attempt_at, last_error, sent_at, updated_at) ON app.notification_outbox TO market_app")
    op.execute("ALTER TABLE app.notification_outbox DROP CONSTRAINT notification_outbox_status_check")
    op.execute("ALTER TABLE app.notification_outbox ADD CONSTRAINT notification_outbox_status_check CHECK (status IN ('queued', 'sending', 'sent', 'failed', 'dry_run', 'suppressed', 'uncertain'))")
    # Previous failed rows cannot prove that the external relay did not send.
    op.execute("UPDATE app.notification_outbox SET status = 'uncertain', last_error = concat_ws('; ', last_error, 'legacy relay outcome unknown; reconcile before resend') WHERE status = 'failed'")


def downgrade() -> None:
    # Fail closed: old code cannot understand these terminal outcomes. Never
    # map them to a retryable status or report an unproven successful delivery.
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM app.notification_outbox WHERE status IN ('suppressed', 'uncertain')) THEN RAISE EXCEPTION 'reconcile terminal notification outcomes before downgrade'; END IF; END $$")
    op.execute("REVOKE INSERT ON app.decision_inbox_item, app.notification_outbox FROM market_app")
    op.execute("REVOKE UPDATE (status, resolved_at) ON app.decision_inbox_item FROM market_app")
    op.execute("REVOKE UPDATE (status, attempts, next_attempt_at, last_error, sent_at, updated_at) ON app.notification_outbox FROM market_app")
    op.execute("ALTER TABLE app.notification_outbox DROP CONSTRAINT notification_outbox_status_check")
    op.execute("ALTER TABLE app.notification_outbox ADD CONSTRAINT notification_outbox_status_check CHECK (status IN ('queued', 'sending', 'sent', 'failed', 'dry_run'))")

