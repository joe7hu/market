"""Apply exact audited edits; verification removes this bridge before testing."""
from pathlib import Path
import hashlib

expected = {
    'migrations/versions/20260927_0042_storage_access_paths.py': 'db08a9ed91e0158f0a00dd9bd58fa880077ed48b396026cdb178d89fcdc7bfdc',
    'src/investment_panel/infrastructure/postgres/storage_audit.py': 'ff66c9f41d644c776fb5e5215b708c68901062446add1ad833064ff0b064fadb',
    'src/investment_panel/infrastructure/postgres/storage_archive.py': 'a07a2982390508df8f52fde115a63bb8fac8fb100084cd41191f3ffa6fede9e9',
    'src/investment_panel/infrastructure/postgres/ingestion.py': 'bbf3f86a4f9fd5d26d0413253e0b9e8b5448d8dda3c29a00dd8bf6571de80229',
}
for name, digest in expected.items():
    assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest, name
p = Path('migrations/versions/20260927_0042_storage_access_paths.py')
s = p.read_text()
s = s.replace('    ("app.publication_bundle_item", "publication_bundle_content_hash_idx", "content_hash"),\n', '')
s = s.replace('    ("app.current_publication_item", "current_publication_content_hash_idx", "content_hash"),', '    ("app.current_publication_item", "current_publication_content_hash_idx", "content_hash"),\n    ("analysis.ticker_decision", "ticker_decision_market_context_idx", "market_state_context_hash"),\n    ("analysis.ticker_decision", "ticker_decision_policy_context_idx", "risk_policy_context_hash"),')
s = s.replace('    for _, name, _ in reversed(_INDEXES):\n        op.execute(f"DROP INDEX app.{name}")', '    for relation, name, _ in reversed(_INDEXES):\n        schema = relation.split(".", 1)[0]\n        op.execute(f"DROP INDEX {schema}.{name}")')
s = s.replace('_INDEXES = (', '# Reuse 0031\'s existing full content-hash index on publication_bundle_item.\n# Do not add a second partial copy of the same lookup path.\n_INDEXES = (')
p.write_text(s)
p = Path('src/investment_panel/infrastructure/postgres/storage_audit.py')
s = p.read_text().replace('from collections import defaultdict', 'from collections import defaultdict\nimport re')
s = s.replace('    return {"equivalent_index_candidates": duplicates, "foreign_key_index_candidates": missing,', '''    # Identical key layouts can still overlap when one index omits only NULLs.
    # This is advisory: a deliberately small partial index may serve a distinct
    # workload, and the full index might be required for IS NULL lookups.
    nullable_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for index in indexes:
        if index["key_count"] == 1 and index["expressions"] is None:
            nullable_groups[tuple(index[field] for field in fields if field != "predicate")].append(index)
    overlaps = []
    for rows in nullable_groups.values():
        full = [row for row in rows if row["predicate"] is None]
        partial = [row for row in rows if row["predicate"] is not None
                   and re.fullmatch(r'\\(?"?\\w+"? IS NOT NULL\\)?', row["predicate"])]
        for complete in full:
            for subset in partial:
                overlaps.append({"relation": complete["relation"], "full_index": complete["name"],
                                 "partial_index": subset["name"], "action": "review_only"})
    return {"equivalent_index_candidates": duplicates, "overlapping_nonnull_index_candidates": overlaps,
            "foreign_key_index_candidates": missing,''')
s = s.replace('        tables = connection.execute(TABLES_QUERY, [list(SCHEMAS)]).fetchall()', '''        # Discover all non-system schemas; otherwise a forgotten legacy table
        # in public or a custom schema would silently escape a storage audit.
        schemas = [row["schema"] for row in connection.execute("""
            SELECT nspname::text AS schema FROM pg_namespace
            WHERE left(nspname, 3) <> 'pg_' AND nspname <> 'information_schema'
            ORDER BY nspname
        """).fetchall()]
        tables = connection.execute(TABLES_QUERY, [schemas]).fetchall()''')
s = s.replace('[list(SCHEMAS)]).fetchall()', '[schemas]).fetchall()')
s = s.replace('"coverage": {"schemas": list(SCHEMAS), "table_count": len(tables),', '''"coverage": {"schemas": schemas, "managed_schemas": list(SCHEMAS), "table_count": len(tables),
                         "unmanaged_relations": [row["relation"] for row in tables
                             if row["relation"].split(".", 1)[0] not in SCHEMAS
                             and row["relation"] != "public.alembic_version"],''')
s = s.replace('"application_allocated_bytes": sum(row["allocated_bytes"] for row in tables),', '''"user_relation_allocated_bytes": sum(row["allocated_bytes"] for row in tables),
            "application_allocated_bytes": sum(row["allocated_bytes"] for row in tables
                if row["relation"].split(".", 1)[0] in SCHEMAS),''')
s = s.replace('"Index equivalence is advisory; constraints, replica identity and deployment dependencies still matter.",', '''"Index equivalence is advisory; constraints, replica identity and deployment dependencies still matter.",
                "Foreign-key advice checks leading columns, not operator-family or planner selectivity guarantees.",
                "Full versus non-NULL-only index overlap requires workload review; it is not an automatic drop instruction.",''')
p.write_text(s)
p = Path('src/investment_panel/infrastructure/postgres/storage_archive.py')
s = p.read_text()
s = s.replace('"volume_free_bytes": usage.free, "logical_evidence_bytes": logical,', '"volume_free_bytes": usage.free, "tracked_evidence_allocated_bytes": logical,')
s = s.replace('"logical_evidence_growth_bytes_per_day": None if logical_rate is None else int(logical_rate),', '"tracked_evidence_allocated_growth_bytes_per_day": None if logical_rate is None else int(logical_rate),')
s = s.replace('"measured_growth_bytes_per_day": int(rate), "forecast_30d_free_bytes": forecast,', '''"measured_growth_bytes_per_day": int(rate) if confidence == "measured" else None,
            "forecast_growth_bytes_per_day": int(rate),
            "forecast_growth_basis": "observed_endpoint_max" if confidence == "measured" else "fallback_0.7_GiB_per_day",
            "forecast_30d_free_bytes": forecast,
            "tracked_evidence_bytes_basis": "allocated_heap_toast_and_indexes_not_logical_payload_size",
            "accounting_column_note": "logical_evidence_bytes is the legacy database column name for allocated files",
            "archive_backlog_basis": "old_local_rows_not_eligibility_count",
            "filesystem_bytes_recovered": None, "reusable_bytes": None,''')
p.write_text(s)
p = Path('src/investment_panel/infrastructure/postgres/ingestion.py')
s = p.read_text()
old = '                        filed_at = EXCLUDED.filed_at, values = EXCLUDED.values\n'
assert s.count(old) == 1
s = s.replace(old, old + '''                    WHERE (raw.fundamental_observation.ingest_run_id,
                           raw.fundamental_observation.filed_at,
                           raw.fundamental_observation.values) IS DISTINCT FROM
                          (EXCLUDED.ingest_run_id, EXCLUDED.filed_at, EXCLUDED.values)
''')
p.write_text(s)
for name in expected:
    compile(Path(name).read_text(), name, 'exec')
