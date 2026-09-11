"""Complete the runtime option capture write path."""

from alembic import op


revision = "20260910_0012"
down_revision = "20260910_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT SELECT,INSERT,UPDATE ON TABLE catalog.option_contract TO market_app;
        GRANT SELECT,INSERT ON TABLE analysis.option_surface_summary TO market_app;
        GRANT SELECT,USAGE ON SEQUENCE analysis.option_surface_summary_id_seq TO market_app;
        GRANT SELECT,INSERT ON TABLE analysis.option_relative_value TO market_app;
        GRANT SELECT,USAGE ON SEQUENCE analysis.option_relative_value_id_seq TO market_app;
        GRANT SELECT,INSERT ON TABLE analysis.option_surface_shift TO market_app;
        GRANT SELECT,INSERT ON TABLE analysis.option_relative_value_verification TO market_app;
        GRANT SELECT,USAGE ON SEQUENCE analysis.option_relative_value_verification_id_seq TO market_app;

        CREATE FUNCTION raw.ensure_option_quote_partition(
            p_partition text,
            p_partition_start date,
            p_partition_end date
        ) RETURNS void
            LANGUAGE plpgsql SECURITY DEFINER
            SET search_path TO 'pg_catalog', 'raw'
            AS $$
            DECLARE
                expected_end date;
            BEGIN
                IF p_partition IS NULL OR p_partition_start IS NULL OR p_partition_end IS NULL THEN
                    RAISE EXCEPTION 'option quote partition arguments are required';
                END IF;
                IF p_partition = 'option_quote_' || to_char(p_partition_start, 'YYYYMM') THEN
                    expected_end := (date_trunc('month', p_partition_start::timestamp) + interval '1 month')::date;
                ELSIF p_partition = 'option_quote_' || to_char(p_partition_start, 'YYYYMMDD') THEN
                    expected_end := p_partition_start + 1;
                ELSE
                    RAISE EXCEPTION 'invalid option quote partition name: %', p_partition;
                END IF;
                IF p_partition_end <> expected_end THEN
                    RAISE EXCEPTION 'invalid option quote partition bounds for %', p_partition;
                END IF;
                EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS raw.%I PARTITION OF raw.option_quote FOR VALUES FROM (%L) TO (%L)',
                    p_partition, p_partition_start, p_partition_end
                );
            END;
            $$;
        REVOKE ALL ON FUNCTION raw.ensure_option_quote_partition(text, date, date) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION raw.ensure_option_quote_partition(text, date, date) TO market_app;
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE EXECUTE ON FUNCTION raw.ensure_option_quote_partition(text, date, date) FROM market_app;
        DROP FUNCTION raw.ensure_option_quote_partition(text, date, date);
        REVOKE INSERT,UPDATE ON TABLE catalog.option_contract FROM market_app;
        REVOKE INSERT ON TABLE analysis.option_surface_summary FROM market_app;
        REVOKE SELECT,USAGE ON SEQUENCE analysis.option_surface_summary_id_seq FROM market_app;
        REVOKE INSERT ON TABLE analysis.option_relative_value FROM market_app;
        REVOKE SELECT,USAGE ON SEQUENCE analysis.option_relative_value_id_seq FROM market_app;
        REVOKE INSERT ON TABLE analysis.option_surface_shift FROM market_app;
        REVOKE INSERT ON TABLE analysis.option_relative_value_verification FROM market_app;
        REVOKE SELECT,USAGE ON SEQUENCE analysis.option_relative_value_verification_id_seq FROM market_app;
        """,
    )
