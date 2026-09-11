"""Preserve safe retention behavior and archive access for option partitions."""

from alembic import op


revision = "20260910_0014"
down_revision = "20260910_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION raw.ensure_option_quote_partition(
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
                    IF p_partition_start <> date_trunc('month', p_partition_start::timestamp)::date THEN
                        RAISE EXCEPTION 'monthly option quote partition must start on the first day: %', p_partition;
                    END IF;
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
                EXECUTE format('GRANT SELECT ON TABLE raw.%I TO market_app', p_partition);
            END;
            $$;
        REVOKE ALL ON FUNCTION raw.ensure_option_quote_partition(text, date, date) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION raw.ensure_option_quote_partition(text, date, date) TO market_app;

        DROP FUNCTION raw.detach_option_quote_partition(text, boolean);
        CREATE FUNCTION raw.detach_option_quote_partition(
            p_partition text,
            p_require_empty boolean
        ) RETURNS boolean
            LANGUAGE plpgsql SECURITY DEFINER
            SET search_path TO 'pg_catalog', 'raw'
            AS $$
            DECLARE
                has_rows boolean;
            BEGIN
                IF p_partition IS NULL OR p_partition !~ '^option_quote_[0-9]{6}([0-9]{2})?$' THEN
                    RAISE EXCEPTION 'invalid option quote partition name: %', p_partition;
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_partition_tree('raw.option_quote'::regclass) tree
                    JOIN pg_class child ON child.oid = tree.relid
                    JOIN pg_namespace namespace ON namespace.oid = child.relnamespace
                    WHERE tree.isleaf
                      AND namespace.nspname = 'raw'
                      AND child.relname = p_partition
                ) THEN
                    RAISE EXCEPTION 'option quote partition does not exist: %', p_partition;
                END IF;
                IF p_require_empty THEN
                    EXECUTE format(
                        'SELECT EXISTS (SELECT 1 FROM raw.%I LIMIT 1)', p_partition
                    ) INTO has_rows;
                    IF has_rows THEN
                        RETURN false;
                    END IF;
                END IF;
                EXECUTE format(
                    'ALTER TABLE raw.option_quote DETACH PARTITION raw.%I', p_partition
                );
                EXECUTE format('DROP TABLE raw.%I', p_partition);
                RETURN true;
            END;
            $$;
        REVOKE ALL ON FUNCTION raw.detach_option_quote_partition(text, boolean) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION raw.detach_option_quote_partition(text, boolean) TO market_app;

        DO $$
        DECLARE
            partition_name text;
        BEGIN
            FOR partition_name IN
                SELECT child.relname
                FROM pg_partition_tree('raw.option_quote'::regclass) tree
                JOIN pg_class child ON child.oid = tree.relid
                JOIN pg_namespace namespace ON namespace.oid = child.relnamespace
                WHERE tree.isleaf
                  AND namespace.nspname = 'raw'
                  AND child.relname ~ '^option_quote_[0-9]{6}([0-9]{2})?$'
            LOOP
                EXECUTE format('GRANT SELECT ON TABLE raw.%I TO market_app', partition_name);
            END LOOP;
        END;
        $$;
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            partition_name text;
        BEGIN
            FOR partition_name IN
                SELECT child.relname
                FROM pg_partition_tree('raw.option_quote'::regclass) tree
                JOIN pg_class child ON child.oid = tree.relid
                JOIN pg_namespace namespace ON namespace.oid = child.relnamespace
                WHERE tree.isleaf
                  AND namespace.nspname = 'raw'
                  AND child.relname ~ '^option_quote_[0-9]{6}([0-9]{2})?$'
            LOOP
                EXECUTE format('REVOKE SELECT ON TABLE raw.%I FROM market_app', partition_name);
            END LOOP;
        END;
        $$;

        REVOKE EXECUTE ON FUNCTION raw.detach_option_quote_partition(text, boolean) FROM market_app;
        DROP FUNCTION raw.detach_option_quote_partition(text, boolean);
        CREATE FUNCTION raw.detach_option_quote_partition(
            p_partition text,
            p_require_empty boolean
        ) RETURNS void
            LANGUAGE plpgsql SECURITY DEFINER
            SET search_path TO 'pg_catalog', 'raw'
            AS $$
            DECLARE
                has_rows boolean;
            BEGIN
                IF p_partition IS NULL OR p_partition !~ '^option_quote_[0-9]{6}([0-9]{2})?$' THEN
                    RAISE EXCEPTION 'invalid option quote partition name: %', p_partition;
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_partition_tree('raw.option_quote'::regclass) tree
                    JOIN pg_class child ON child.oid = tree.relid
                    JOIN pg_namespace namespace ON namespace.oid = child.relnamespace
                    WHERE tree.isleaf
                      AND namespace.nspname = 'raw'
                      AND child.relname = p_partition
                ) THEN
                    RAISE EXCEPTION 'option quote partition does not exist: %', p_partition;
                END IF;
                IF p_require_empty THEN
                    EXECUTE format(
                        'SELECT EXISTS (SELECT 1 FROM raw.%I LIMIT 1)', p_partition
                    ) INTO has_rows;
                    IF has_rows THEN
                        RAISE EXCEPTION 'option quote partition is not empty: %', p_partition;
                    END IF;
                END IF;
                EXECUTE format(
                    'ALTER TABLE raw.option_quote DETACH PARTITION raw.%I', p_partition
                );
                EXECUTE format('DROP TABLE raw.%I', p_partition);
            END;
            $$;
        REVOKE ALL ON FUNCTION raw.detach_option_quote_partition(text, boolean) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION raw.detach_option_quote_partition(text, boolean) TO market_app;

        CREATE OR REPLACE FUNCTION raw.ensure_option_quote_partition(
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
                    IF p_partition_start <> date_trunc('month', p_partition_start::timestamp)::date THEN
                        RAISE EXCEPTION 'monthly option quote partition must start on the first day: %', p_partition;
                    END IF;
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
