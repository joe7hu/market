"""Require verified NAS coverage before pruning empty run metadata."""
from alembic import op

revision = "20260924_0038"
down_revision = "20260924_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION analysis.prune_empty_run_metadata(p_ids uuid[], p_before timestamptz)
        RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE ref record; guards text := ''; removed integer;
        BEGIN
            IF p_ids IS NULL OR p_before IS NULL OR cardinality(p_ids) > 100 THEN
                RAISE EXCEPTION 'empty run metadata deletion must be bounded to 100 IDs';
            END IF;
            IF p_before > now() - interval '30 days' THEN
                RAISE EXCEPTION 'empty run metadata deletion requires a 30 days minimum age';
            END IF;
            IF EXISTS (
                SELECT 1 FROM unnest(p_ids) requested(id)
                WHERE requested.id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM ops.storage_archive_manifest manifest
                    WHERE manifest.archive_kind = 'derived'
                      AND manifest.source_relation = 'analysis.run'
                      AND manifest.format = 'json.gz'
                      AND manifest.verification_status IN ('verified', 'restored')
                      AND jsonb_typeof(manifest.metadata->'source_row_ids') = 'array'
                      AND (manifest.metadata->'source_row_ids') ? requested.id::text
                )
            ) THEN
                RAISE EXCEPTION 'empty run metadata requires a verified archive for every ID';
            END IF;
            FOR ref IN
                SELECT n.nspname, c.relname, a.attname, cardinality(f.conkey) AS key_count
                FROM pg_constraint f JOIN pg_class c ON c.oid = f.conrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = f.conrelid AND a.attnum = f.conkey[1]
                WHERE f.contype = 'f' AND f.confrelid = 'analysis.run'::regclass
            LOOP
                IF ref.key_count <> 1 THEN
                    RAISE EXCEPTION 'unreviewed composite analysis-run reference; metadata retained';
                END IF;
                guards := guards || format(
                    ' AND NOT EXISTS (SELECT 1 FROM %I.%I child WHERE child.%I = run.id)',
                    ref.nspname, ref.relname, ref.attname);
            END LOOP;
            EXECUTE 'DELETE FROM analysis.run run WHERE run.id = ANY($1) AND run.started_at < $2' || guards USING p_ids, p_before;
            GET DIAGNOSTICS removed = ROW_COUNT;
            RETURN removed;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION analysis.prune_empty_run_metadata(p_ids uuid[], p_before timestamptz)
        RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp AS $$
        DECLARE ref record; guards text := ''; removed integer;
        BEGIN
            IF p_ids IS NULL OR p_before IS NULL OR cardinality(p_ids) > 100 THEN
                RAISE EXCEPTION 'empty run metadata deletion must be bounded to 100 IDs';
            END IF;
            FOR ref IN
                SELECT n.nspname, c.relname, a.attname, cardinality(f.conkey) AS key_count
                FROM pg_constraint f JOIN pg_class c ON c.oid = f.conrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = f.conrelid AND a.attnum = f.conkey[1]
                WHERE f.contype = 'f' AND f.confrelid = 'analysis.run'::regclass
            LOOP
                IF ref.key_count <> 1 THEN
                    RAISE EXCEPTION 'unreviewed composite analysis-run reference; metadata retained';
                END IF;
                guards := guards || format(
                    ' AND NOT EXISTS (SELECT 1 FROM %I.%I child WHERE child.%I = run.id)',
                    ref.nspname, ref.relname, ref.attname);
            END LOOP;
            EXECUTE 'DELETE FROM analysis.run run WHERE run.id = ANY($1) AND run.started_at < $2' || guards USING p_ids, p_before;
            GET DIAGNOSTICS removed = ROW_COUNT;
            RETURN removed;
        END $$;
    """)
