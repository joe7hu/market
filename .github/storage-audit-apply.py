"""One-shot, source-hash-guarded edit bridge; removed by the verification job."""
from pathlib import Path
import hashlib
import re
import textwrap

expected = {
    'src/investment_panel/infrastructure/postgres/ingestion.py': 'fe21ebd16d0fe664bfd64ba8b968e78d575ae95fe704c69e849d2d1d52635f05',
    'src/investment_panel/infrastructure/postgres/migrations.py': '9a4c3a024e88e2ec6c3790e34a9921ab5f724a07e0997d13560a917afc595c45',
    'src/investment_panel/jobs/storage.py': '023005e891cdcca4d7a569c667711a6866ce5045357894cc40b4108ab0684665',
    'tests/postgres/test_schema_baseline.py': '7f54327ffbae21dc0a500d5f9958a0ebf6b2663a35c5b6b1c7c74f70987f1aed',
}
for path, digest in expected.items():
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest, path
p = Path('src/investment_panel/infrastructure/postgres/ingestion.py')
s = p.read_text()
s = s.replace('"UPDATE ingest.source SET name = %s, capabilities = %s, updated_at = now() WHERE id = %s",\n                    [name, Jsonb(capabilities or {}), source_id],', '''"""UPDATE ingest.source SET name = %s, capabilities = %s, updated_at = now()
                       WHERE id = %s AND (name, capabilities) IS DISTINCT FROM (%s, %s::jsonb)""",
                    [name, Jsonb(capabilities or {}), source_id, name, Jsonb(capabilities or {})],''')
start = s.index('                ON CONFLICT (source_id, observed_at, universe) DO UPDATE', s.index('def store_option_snapshot'))
end = s.index('                RETURNING id', start)
fields = ['ingest_run_id', 'payload_id', 'market_session', 'completeness', 'contract_count', 'collection_profile', 'history_symbol', 'slot_at', 'capture_started_at', 'capture_finished_at', 'expected_contract_count', 'received_contract_count', 'capture_state']
lhs = ', '.join('raw.option_snapshot.' + field for field in fields)
rhs = ', '.join('COALESCE(EXCLUDED.payload_id, raw.option_snapshot.payload_id)' if field == 'payload_id' else 'EXCLUDED.' + field for field in fields)
where = '                WHERE ROW(\n' + textwrap.fill(lhs, width=105, initial_indent='                    ', subsequent_indent='                    ') + ') IS DISTINCT FROM ROW(\n' + textwrap.fill(rhs, width=105, initial_indent='                    ', subsequent_indent='                    ') + ')\n'
s = s[:end] + where + s[end:]
s = s.replace('            snapshot_id = int(snapshot["id"])', '''            # An unchanged ON CONFLICT row is locked but not RETURNING-ed.
            # Use a new READ COMMITTED statement so concurrent first inserts
            # are visible without generating a needless tuple version.
            if snapshot is None:
                snapshot = connection.execute(
                    """SELECT id FROM raw.option_snapshot
                       WHERE source_id = %s AND observed_at = %s AND universe = %s""",
                    [source_id, observed_at, universe],
                ).fetchone()
            if snapshot is None:
                raise RuntimeError("option snapshot identity disappeared during ingestion")
            snapshot_id = int(snapshot["id"])''')
start = s.index('                      standard_contract_verified = (', s.index('                    INSERT INTO catalog.option_contract'))
end = s.index(',\n                      deliverable_key', start)
expr = s[start:end].split(' = ', 1)[1]
where = '''
                    WHERE catalog.option_contract.provider_symbols IS DISTINCT FROM
                          (catalog.option_contract.provider_symbols || EXCLUDED.provider_symbols)
                       OR (EXCLUDED.style IS NOT NULL AND
                           catalog.option_contract.style IS DISTINCT FROM EXCLUDED.style)
                       OR (EXCLUDED.settlement IS NOT NULL AND
                           catalog.option_contract.settlement IS DISTINCT FROM EXCLUDED.settlement)
                       OR catalog.option_contract.standard_contract_verified IS DISTINCT FROM ''' + expr
s = s.replace('                      deliverable_key = catalog.option_contract.deliverable_key\n', '                      deliverable_key = catalog.option_contract.deliverable_key' + where + '\n')
start = s.index('                    ON CONFLICT (snapshot_id, contract_id, observed_at) DO UPDATE')
end = s.index('\n                    """,', start)
columns = re.findall(r'(\w+) = EXCLUDED\.\1', s[start:end])
assert len(columns) == 34, columns
left = ', '.join('raw.option_quote.' + column for column in columns)
right = ', '.join('EXCLUDED.' + column for column in columns)
where = '\n                    WHERE ROW(\n' + textwrap.fill(left, width=108, initial_indent='                        ', subsequent_indent='                        ') + ') IS DISTINCT FROM ROW(\n' + textwrap.fill(right, width=108, initial_indent='                        ', subsequent_indent='                        ') + ')'
s = s[:end] + where + s[end:]
s = s.replace('"UPDATE ingest.run SET item_count = %s, instrument_count = %s WHERE id = %s",\n                [len(normalized), len({row["underlying_symbol"] for row in normalized}), run_id],', '''"""UPDATE ingest.run SET item_count = %s, instrument_count = %s WHERE id = %s
                   AND (item_count, instrument_count) IS DISTINCT FROM (%s, %s)""",
                [len(normalized), len({row["underlying_symbol"] for row in normalized}), run_id,
                 len(normalized), len({row["underlying_symbol"] for row in normalized})],''')
p.write_text(s)
p = Path('src/investment_panel/infrastructure/postgres/migrations.py')
s = p.read_text().replace('HEAD_REVISION = "20260927_0041"', 'HEAD_REVISION = "20260927_0042"')
ix = s.index('_SUPPORTED_UPGRADE_REVISIONS')
if '"20260927_0041"' not in s[ix:s.index('})', ix)]:
    s = s[:ix] + s[ix:].replace('frozenset({', 'frozenset({\n    "20260927_0041",', 1)
p.write_text(s)
p = Path('tests/postgres/test_schema_baseline.py')
p.write_text(p.read_text().replace("        '20260927_0041_publication_decision_refs.py',", "        '20260927_0041_publication_decision_refs.py',\n        '20260927_0042_storage_access_paths.py',"))
p = Path('src/investment_panel/jobs/storage.py')
s = p.read_text().replace('from investment_panel.infrastructure.postgres.storage_archive import ARCHIVE_KINDS, StorageArchiveService', 'from investment_panel.infrastructure.postgres.storage_archive import ARCHIVE_KINDS, StorageArchiveService\nfrom investment_panel.infrastructure.postgres.storage_audit import audit_storage')
s = s.replace('    if command == "account":', '    if command == "audit":\n        if execute:\n            raise ValueError("storage audit is read-only; omit --execute")\n        return audit_storage(service.runtime)\n    if command == "account":')
s = s.replace('choices=("plan", "account", "archive", "verify", "compact", "restore")', 'choices=("plan", "audit", "account", "archive", "verify", "compact", "restore")')
p.write_text(s)
for path in expected:
    compile(Path(path).read_text(), path, 'exec')
