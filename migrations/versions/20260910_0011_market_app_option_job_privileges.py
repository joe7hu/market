"""Allow the runtime role to complete the option capture lifecycle."""

from alembic import op


revision = "20260910_0011"
down_revision = "20260909_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT SELECT,INSERT,UPDATE ON TABLE analysis.option_recovery_program_session TO market_app;
        GRANT SELECT,INSERT,UPDATE ON TABLE raw.option_capture_generation TO market_app;
        GRANT SELECT,USAGE ON SEQUENCE raw.option_capture_generation_id_seq TO market_app;
        GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE raw.option_quote TO market_app;
        GRANT SELECT,USAGE ON SEQUENCE raw.option_quote_id_seq TO market_app;
        GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE raw.option_snapshot TO market_app;
        GRANT SELECT,USAGE ON SEQUENCE raw.option_snapshot_id_seq TO market_app;
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE INSERT,UPDATE ON TABLE analysis.option_recovery_program_session FROM market_app;
        REVOKE INSERT,UPDATE ON TABLE raw.option_capture_generation FROM market_app;
        REVOKE SELECT,USAGE ON SEQUENCE raw.option_capture_generation_id_seq FROM market_app;
        REVOKE INSERT,DELETE,UPDATE ON TABLE raw.option_quote FROM market_app;
        REVOKE SELECT,USAGE ON SEQUENCE raw.option_quote_id_seq FROM market_app;
        REVOKE INSERT,DELETE,UPDATE ON TABLE raw.option_snapshot FROM market_app;
        REVOKE SELECT,USAGE ON SEQUENCE raw.option_snapshot_id_seq FROM market_app;
        """,
    )
