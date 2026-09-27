"""Read-only, catalog-wide storage inventory; never an automatic deletion policy.

No table payloads, common-value samples, histograms, or signing secrets are
read. Allocated files and statistical estimates are deliberately separate.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime, RuntimeProfile

SCHEMAS = ("analysis", "app", "catalog", "ingest", "ops", "raw")
AUDIT_PROFILE = RuntimeProfile(statement_timeout_ms=30_000, lock_timeout_ms=2_000, jit=False)

TABLES_QUERY = """
    SELECT c.oid::bigint AS oid, n.nspname || '.' || c.relname AS relation,
           c.relkind::text AS kind, c.relispartition AS is_partition,
           ARRAY(SELECT parent.inhparent::regclass::text FROM pg_inherits parent
                 WHERE parent.inhrelid = c.oid ORDER BY parent.inhparent) AS parents,
           CASE WHEN c.relkind = 'p' THEN 0 ELSE pg_total_relation_size(c.oid) END AS allocated_bytes,
           CASE WHEN c.relkind = 'p' THEN 0 ELSE pg_relation_size(c.oid) END AS heap_bytes,
           CASE WHEN c.reltoastrelid = 0 THEN 0 ELSE pg_total_relation_size(c.reltoastrelid) END AS toast_bytes,
           CASE WHEN c.relkind = 'p' THEN 0 ELSE pg_indexes_size(c.oid) END AS index_bytes,
           c.reltuples::bigint AS estimated_rows, c.reloptions AS storage_options,
           stats.n_live_tup AS estimated_live_tuples, stats.n_dead_tup AS estimated_dead_tuples,
           stats.n_tup_ins AS inserted_tuples, stats.n_tup_upd AS updated_tuples,
           stats.n_tup_del AS deleted_tuples, stats.n_tup_hot_upd AS hot_updates,
           stats.seq_scan, stats.idx_scan, stats.last_analyze, stats.last_autoanalyze,
           stats.last_vacuum, stats.last_autovacuum,
           has_table_privilege(c.oid, 'SELECT') AS can_read,
           pg_get_partkeydef(c.oid) AS partition_key
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_stat_all_tables stats ON stats.relid = c.oid
    WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p', 'm')
    ORDER BY n.nspname, c.relname
"""

COLUMNS_QUERY = """
    SELECT n.nspname || '.' || c.relname AS relation, a.attnum AS ordinal,
           a.attname::text AS column, format_type(a.atttypid, a.atttypmod) AS type,
           a.attnotnull AS not_null, a.attidentity::text AS identity,
           a.attgenerated::text AS generated, a.attstorage::text AS storage,
           a.attcompression::text AS compression,
           d.oid IS NOT NULL AS has_default,
           stats.null_frac AS estimated_null_fraction, stats.avg_width AS estimated_average_width,
           stats.n_distinct AS estimated_distinct_values,
           stats.attname IS NOT NULL AS statistics_visible
    FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
    LEFT JOIN pg_stats stats ON stats.schemaname = n.nspname AND stats.tablename = c.relname
      AND stats.attname = a.attname AND NOT stats.inherited
    WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p', 'm')
      AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY n.nspname, c.relname, a.attnum
"""

INDEXES_QUERY = """
    SELECT n.nspname || '.' || c.relname AS relation, idx.oid::bigint AS oid,
           idx.relname::text AS name, access.amname::text AS access_method,
           pg_relation_size(idx.oid) AS allocated_bytes,
           pg_get_indexdef(idx.oid) AS definition, pg_get_expr(i.indpred, i.indrelid) AS predicate,
           pg_get_expr(i.indexprs, i.indrelid) AS expressions,
           i.indkey::text AS attribute_numbers, i.indnkeyatts AS key_count,
           i.indclass::text AS operator_classes, i.indcollation::text AS collations,
           i.indoption::text AS key_options, i.indisunique AS is_unique,
           i.indnullsnotdistinct AS nulls_not_distinct, i.indisexclusion AS is_exclusion,
           i.indimmediate AS is_immediate, i.indisvalid AS is_valid, i.indisready AS is_ready,
           i.indisprimary AS is_primary, i.indisreplident AS is_replica_identity,
           idx.reloptions AS storage_options, idx.reltablespace::bigint AS tablespace_oid,
           ARRAY(SELECT co.conname::text FROM pg_constraint co
                 WHERE co.conindid = idx.oid AND co.conrelid = c.oid ORDER BY co.conname) AS constraints,
           stats.idx_scan AS scans, stats.idx_tup_read AS tuples_read,
           stats.idx_tup_fetch AS tuples_fetched
    FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid
    JOIN pg_class idx ON idx.oid = i.indexrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_am access ON access.oid = idx.relam
    LEFT JOIN pg_stat_all_indexes stats ON stats.indexrelid = idx.oid
    WHERE n.nspname = ANY(%s)
    ORDER BY n.nspname, c.relname, idx.relname
"""

CONSTRAINTS_QUERY = """
    SELECT n.nspname || '.' || c.relname AS relation, co.conname::text AS name,
           co.contype::text AS type, pg_get_constraintdef(co.oid) AS definition,
           co.convalidated AS validated, co.condeferrable AS deferrable,
           co.conparentid::bigint AS parent_constraint_oid,
           co.conkey AS attribute_numbers,
           CASE WHEN co.confrelid <> 0 THEN co.confrelid::regclass::text END AS referenced_relation,
           ARRAY(SELECT a.attname::text FROM unnest(co.conkey) WITH ORDINALITY k(attnum, pos)
                 JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
                 ORDER BY k.pos) AS columns
    FROM pg_constraint co JOIN pg_class c ON c.oid = co.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = ANY(%s)
    ORDER BY n.nspname, c.relname, co.conname
"""


def _index_advice(indexes: list[dict[str, Any]], constraints: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fields = ("relation", "access_method", "attribute_numbers", "key_count", "operator_classes",
              "collations", "key_options", "predicate", "expressions", "is_unique",
              "nulls_not_distinct", "is_exclusion", "is_immediate", "is_valid", "is_ready")
    for index in indexes:
        groups[tuple(index[field] for field in fields)].append(index)
        by_table[index["relation"]].append(index)
    duplicates = [{"relation": rows[0]["relation"], "indexes": [row["name"] for row in rows],
                   "action": "review_only_preserve_constraint_and_replica_identity_indexes"}
                  for rows in groups.values() if len(rows) > 1]
    missing = []
    for constraint in constraints:
        if constraint["type"] != "f" or constraint["parent_constraint_oid"]:
            continue
        keys = constraint["attribute_numbers"] or []
        covered = False
        for index in by_table[constraint["relation"]]:
            if index["access_method"] != "btree" or not index["is_valid"] or not index["is_ready"]:
                continue
            leading = [int(value) for value in index["attribute_numbers"].split()][:len(keys)]
            if index["key_count"] < len(keys) or set(leading) != set(keys):
                continue
            predicate = index["predicate"]
            null_filter = (len(keys) == 1 and predicate is not None and
                           predicate.strip("() ") == constraint["columns"][0] + " IS NOT NULL")
            if predicate is None or null_filter:
                covered = True
                break
        if not covered:
            missing.append({"relation": constraint["relation"], "constraint": constraint["name"],
                            "columns": constraint["columns"], "action": "measure_before_adding_index"})
    return {"equivalent_index_candidates": duplicates, "foreign_key_index_candidates": missing,
            "unused_indexes": "not_inferred_from_zero_scans"}


def audit_storage(runtime: DatabaseRuntime) -> dict[str, Any]:
    """Return a complete catalog snapshot, bounded per statement, without writes.

    Timeouts and permission errors propagate: a partial report is never passed
    off as a complete zero-sized database. No ANALYZE/VACUUM or table scan runs.
    """
    with runtime.snapshot(AUDIT_PROFILE) as connection:
        identity = connection.execute("""SELECT current_database() AS database,
            current_user AS inspection_role, current_setting('server_version') AS server_version,
            current_setting('transaction_read_only') = 'on' AS read_only,
            current_timestamp AS observed_at, pg_database_size(current_database()) AS database_bytes,
            (SELECT oid::bigint FROM pg_database WHERE datname = current_database()) AS database_oid,
            pg_postmaster_start_time() AS server_started_at,
            (SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()) AS statistics_reset_at
        """).fetchone()
        revision = connection.execute("SELECT version_num FROM public.alembic_version").fetchone()
        tables = connection.execute(TABLES_QUERY, [list(SCHEMAS)]).fetchall()
        columns = connection.execute(COLUMNS_QUERY, [list(SCHEMAS)]).fetchall()
        indexes = connection.execute(INDEXES_QUERY, [list(SCHEMAS)]).fetchall()
        constraints = connection.execute(CONSTRAINTS_QUERY, [list(SCHEMAS)]).fetchall()
    for table in tables:
        table["auxiliary_bytes"] = (table["allocated_bytes"] - table["heap_bytes"]
                                    - table["toast_bytes"] - table["index_bytes"])
    return {"format": "market-storage-audit.v1", **identity,
            "schema_revision": revision["version_num"] if revision else None,
            "coverage": {"schemas": list(SCHEMAS), "table_count": len(tables),
                         "column_count": len(columns), "index_count": len(indexes),
                         "constraint_count": len(constraints),
                         "columns_without_visible_statistics": sum(not row["statistics_visible"] for row in columns),
                         "unreadable_tables": [row["relation"] for row in tables if not row["can_read"]]},
            "application_allocated_bytes": sum(row["allocated_bytes"] for row in tables),
            "filesystem_bytes_recovered": None, "reusable_bytes": None,
            "growth_bytes_per_day": None,
            "measurement_notes": [
                "Physical child tables are counted once; partition parents have zero allocated bytes.",
                "TOAST includes its own indexes; main-table indexes are separate.",
                "Tuple, width and distinct counts are statistics, not exact counts or live-byte measurements.",
                "Absent column statistics can mean no ANALYZE or restricted visibility, not unused data.",
                "Index equivalence is advisory; constraints, replica identity and deployment dependencies still matter.",
                "No provider values, default expressions, common-value arrays or histograms are read.",
                "Allocated file sizes include reusable pages and are not logical evidence size.",
                "One observation cannot establish growth, rewrite headroom, or filesystem recovery.",
            ],
            "tables": tables, "columns": columns, "indexes": indexes, "constraints": constraints,
            **_index_advice(indexes, constraints)}
