"""Keep app-role option partition maintenance non-destructive."""

from alembic import op


revision = "20260910_0016"
down_revision = "20260910_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        GRANT SELECT ON TABLE ops.option_quote_partition_policy TO market_app;

        CREATE OR REPLACE FUNCTION raw.detach_option_quote_partition(
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
                IF p_require_empty IS NULL THEN
                    RAISE EXCEPTION 'option quote partition emptiness requirement is required';
                END IF;
                IF NOT p_require_empty THEN
                    RAISE EXCEPTION 'non-empty option quote partition detach requires a privileged maintenance role';
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
                EXECUTE format(
                    'SELECT EXISTS (SELECT 1 FROM raw.%I LIMIT 1)', p_partition
                ) INTO has_rows;
                IF has_rows THEN
                    RETURN false;
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
        """,
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE SELECT ON TABLE ops.option_quote_partition_policy FROM market_app;

        CREATE OR REPLACE FUNCTION raw.detach_option_quote_partition(
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
                IF p_require_empty IS NULL THEN
                    RAISE EXCEPTION 'option quote partition emptiness requirement is required';
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
                IF NOT p_require_empty AND NOT EXISTS (
                    SELECT 1
                    FROM ops.storage_archive_manifest manifest
                    WHERE manifest.archive_kind = 'options'
                      AND manifest.source_relation = 'raw.' || p_partition
                      AND manifest.format = 'custom'
                      AND manifest.verification_status IN ('verified', 'restored')
                      AND manifest.verified_at IS NOT NULL
                      AND manifest.range_end <= now() - interval '7 days'
                      AND manifest.metadata ->> 'partition' = p_partition
                      AND manifest.metadata ->> 'restore_verified' = 'true'
                ) THEN
                    RAISE EXCEPTION 'verified archive is required before dropping option quote partition: %', p_partition;
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
        """,
    )
