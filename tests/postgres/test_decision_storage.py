from __future__ import annotations

from datetime import UTC, datetime, timedelta
import gzip
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from investment_panel.infrastructure.postgres.decision_storage import (
    compact_context_batch, context_digest, require_maintenance_headroom, store_context,
)
from investment_panel.infrastructure.postgres.instruments import reconcile_instrument
from investment_panel.infrastructure.postgres.manifest_archive import (
    CHECKPOINT, FORMAT, KIND, MAX_CHUNK_BYTES, ManifestArchive, verify_copy_file,
)
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.infrastructure.postgres.storage_archive import StorageArchiveService
from investment_panel.infrastructure.postgres.retention import RetentionRepository
from investment_panel.infrastructure.postgres.analysis import AnalysisRepository


@pytest.fixture
def storage(migrated_postgres_dsn, tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_STORAGE_DATABASE_PATH", str(tmp_path))
    # Capacity failure has separate tests; other cases are filesystem-size independent.
    monkeypatch.setattr("shutil.disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3, total=200 * 1024**3))
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    service = StorageArchiveService(runtime, tmp_path / "nas" / "storage-archive" / "v1")
    service.archive_root.parent.mkdir(parents=True)
    try:
        yield service
    finally:
        runtime.close()


def _decision(runtime, *, revision="first", context=None, policy=None):
    with runtime.transaction() as connection:
        instrument_id = reconcile_instrument(connection, "STOR")
        return connection.execute(
            """INSERT INTO analysis.ticker_decision
               (instrument_id, decision_revision, contract_version, as_of, input_hash,
                code_version, experiment_id, tactical, fundamental, capital_action,
                risk_policy, input_manifest, market_state_snapshot, risk_policy_snapshot)
               VALUES (%s, %s, 'test', now(), %s, 'test', 'test', '{}', '{}', '{}', '{}', %s, %s, %s)
               RETURNING id""",
            [instrument_id, revision, "a" * 64,
             Jsonb({"inputs": {"quote": [{"value": 10, "revision": None, "raw": "line\n中文"}]}}),
             Jsonb(context or {}), Jsonb(policy or {})],
        ).fetchone()["id"]


def _legacy(runtime, decision_id, *, rows=3):
    with runtime.transaction() as connection:
        for index in range(rows):
            connection.execute(
                """INSERT INTO analysis.ticker_input_manifest_legacy
                   (ticker_decision_id, field, source_id, available_at, revision, original_value, revised_value)
                   VALUES (%s, 'quote', 'test', '2026-08-01 00:00:00+00', NULL, %s, %s)""",
                [decision_id, Jsonb({"value": index, "escaped": "tab\tline\n\\N", "null": None}),
                 Jsonb({"value": index + 1, "unicode": "中文"})],
            )


def _backup(service):
    root = service.archive_root.parent.parent / "postgres-backups"
    root.mkdir(parents=True, exist_ok=True)
    dump = root / "fixture.dump"
    dump.write_bytes(b"verified backup fixture")
    token = sha256(dump.read_bytes()).hexdigest()
    (root / "fixture.json").write_text(json.dumps({"status": "verified", "sha256": token, "dump_path": str(dump)}))
    return token


def test_context_normalization_is_lossless_shared_and_idempotent(storage):
    context = {"as_of": "2026-08-01T00:00:00Z", "facts": [{"raw": "中文", "revision": None}], "zero": 0.0}
    policy = {"version": "policy.v1", "limits": {"max": 0.1}}
    first = _decision(storage.runtime, context=context, policy=policy)
    second = _decision(storage.runtime, revision="second", context=context, policy=policy)
    before = {}
    with storage.runtime.read() as connection:
        for row in connection.execute("SELECT * FROM analysis.ticker_decision_read ORDER BY id").fetchall():
            before[row["id"]] = dict(row)
    assert compact_context_batch(storage.runtime)["remaining"] == 2
    assert compact_context_batch(storage.runtime, batch_size=1, execute=True)["compacted"] == 1
    assert compact_context_batch(storage.runtime, execute=True)["compacted"] == 1
    assert compact_context_batch(storage.runtime, execute=True)["compacted"] == 0
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) FROM analysis.decision_context").fetchone()["count"] == 2
        assert connection.execute("SELECT count(*) FROM analysis.ticker_input_manifest_legacy").fetchone()["count"] == 0
        for row in connection.execute("SELECT * FROM analysis.ticker_decision_read ORDER BY id").fetchall():
            expected = before[row["id"]]
            for key in expected:
                if not key.endswith("_context_hash"):
                    assert row[key] == expected[key], key
        raw = connection.execute("SELECT market_state_snapshot, risk_policy_snapshot FROM analysis.ticker_decision WHERE id = %s", [first]).fetchone()
        assert raw == {"market_state_snapshot": {}, "risk_policy_snapshot": {}}
        assert first != second
    with storage.runtime.transaction() as connection:
        assert store_context(connection, context) == context_digest(context)
        assert store_context(connection, None) is None
        assert store_context(connection, {}) is None
    with pytest.raises(psycopg.Error, match="immutable"), storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.decision_context SET payload = '{}'")
    with pytest.raises(ValueError), storage.runtime.transaction() as connection:
        connection.execute("INSERT INTO analysis.decision_context (content_hash, payload) VALUES (%s, '{}')", [context_digest({"collision": 1})])
        store_context(connection, {"collision": 1})


def test_context_hash_is_order_independent_but_keeps_values_and_versions():
    assert context_digest({"b": 1, "a": None}) == context_digest({"a": None, "b": 1})
    assert context_digest({"a": None}) != context_digest({})
    assert context_digest({"revision": 1}) != context_digest({"revision": 2})
    with pytest.raises(ValueError):
        context_digest({"bad": float("nan")})


def test_manifest_archive_complete_restore_and_only_legacy_drop(storage, tmp_path):
    decision_id = _decision(storage.runtime, context={"critical": "retained"})
    _legacy(storage.runtime, decision_id, rows=5)
    with storage.runtime.transaction() as connection:
        connection.execute("INSERT INTO analysis.ticker_outcome (ticker_decision_id, horizon, horizon_sessions) VALUES (%s, 'TACTICAL', 5)", [decision_id])
    archive = ManifestArchive(storage)
    assert archive.run(state="backfill")["dry_run"] is True
    assert not storage.archive_root.exists()
    first = archive.run(state="backfill", execute=True, batch_size=2)
    assert first["rows"] == 2
    with pytest.raises(ValueError, match="incomplete"):
        archive.run(state="verify")
    result = archive.run(state="backfill", execute=True, batch_size=2, max_batches=10)
    assert result["status"] == "export_complete"
    assert result["rows"] == 3
    assert archive.run(state="verify")["rows"] == 5
    with storage.runtime.read() as connection:
        manifest = connection.execute("SELECT * FROM ops.storage_archive_manifest ORDER BY id LIMIT 1").fetchone()
    assert manifest["format"] == FORMAT
    assert Path(manifest["nas_uri"]).with_suffix(".json").exists()
    destination = tmp_path / "restored.copy"
    storage.restore_to_file(manifest["id"], destination)
    assert len(destination.read_bytes().splitlines()) == 2
    assert storage.verify()["verified"] == 3
    with pytest.raises(ValueError, match="backup"):
        archive.run(state="cutover", execute=True)
    token = _backup(storage)
    result = archive.run(state="cutover", execute=True, backup_token=token)
    assert result["dropped"] and result["rows"] == 5
    assert result["relation_bytes_released_on_commit"] > 0
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT to_regclass('analysis.ticker_input_manifest_legacy') AS relation").fetchone()["relation"] is None
        assert connection.execute("SELECT input_manifest FROM analysis.ticker_decision WHERE id = %s", [decision_id]).fetchone()["input_manifest"]["inputs"]["quote"][0]["raw"] == "line\n中文"
        assert connection.execute("SELECT count(*) FROM analysis.ticker_outcome WHERE ticker_decision_id = %s", [decision_id]).fetchone()["count"] == 1
    assert archive.run(state="cutover", execute=True, backup_token=token)["status"] == "already_compacted"


@pytest.mark.parametrize("failure", ["corrupt", "changed", "tail", "gap"])
def test_cutover_refuses_incomplete_or_modified_archives(storage, failure):
    decision_id = _decision(storage.runtime)
    _legacy(storage.runtime, decision_id, rows=3)
    archive = ManifestArchive(storage)
    archive.run(state="backfill", execute=True, batch_size=2, max_batches=10)
    with storage.runtime.transaction() as connection:
        manifest = connection.execute("SELECT * FROM ops.storage_archive_manifest ORDER BY id LIMIT 1").fetchone()
        if failure == "corrupt":
            Path(manifest["nas_uri"]).write_bytes(b"broken")
        elif failure == "changed":
            connection.execute("UPDATE analysis.ticker_input_manifest_legacy SET original_value = '{\"changed\":true}' WHERE id = 1")
        elif failure == "gap":
            connection.execute("UPDATE ops.storage_archive_manifest SET metadata = jsonb_set(metadata, '{lower_id_exclusive}', '999') WHERE id = %s", [manifest["id"]])
    if failure == "tail":
        _legacy(storage.runtime, decision_id, rows=1)
    with pytest.raises(ValueError):
        archive.run(state="cutover", execute=True, backup_token=_backup(storage))
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) FROM analysis.ticker_input_manifest_legacy").fetchone()["count"] == (4 if failure == "tail" else 3)
        assert connection.execute("SELECT count(*) FROM analysis.ticker_decision").fetchone()["count"] == 1


def test_export_retry_after_checkpoint_failure_reuses_exact_bytes(storage, monkeypatch):
    _legacy(storage.runtime, _decision(storage.runtime), rows=2)
    archive = ManifestArchive(storage)
    save = storage._set_checkpoint
    monkeypatch.setattr(storage, "_set_checkpoint", Mock(side_effect=RuntimeError("checkpoint interrupted")))
    with pytest.raises(RuntimeError, match="interrupted"):
        archive.run(state="backfill", execute=True, batch_size=2)
    monkeypatch.setattr(storage, "_set_checkpoint", save)
    assert archive.run(state="backfill", execute=True, batch_size=2)["rows"] == 2
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) FROM ops.storage_archive_manifest WHERE archive_kind = %s", [KIND]).fetchone()["count"] == 1
    assert len(list(storage.archive_root.rglob("*.copy.gz"))) == 1
    assert storage._checkpoint_cursor(CHECKPOINT)["last_id"] == 2


def test_copy_verifier_rejects_row_count_hash_and_budget_mismatches(tmp_path, monkeypatch):
    raw = b"1\trow\\nvalue\n"
    path = tmp_path / "chunk.gz"
    path.write_bytes(gzip.compress(raw, mtime=0))
    digest = sha256(path.read_bytes()).hexdigest()
    metadata = {"content_sha256": sha256(raw).hexdigest(), "uncompressed_bytes": len(raw), "columns": [{"name": "id"}]}
    assert verify_copy_file(path, digest, row_count=1, metadata=metadata)[0]
    assert not verify_copy_file(path, "f" * 64, row_count=1, metadata=metadata)[0]
    assert not verify_copy_file(path, digest, row_count=2, metadata=metadata)[0]
    assert not verify_copy_file(path, digest, row_count=1, metadata={**metadata, "content_sha256": "bad"})[0]
    assert not verify_copy_file(path, digest, row_count=1, metadata={**metadata, "columns": []})[0]
    monkeypatch.setattr("investment_panel.infrastructure.postgres.manifest_archive.MAX_CHUNK_BYTES", 1)
    assert verify_copy_file(path, digest, row_count=1, metadata=metadata)[1] == "chunk_exceeds_restore_budget"
    assert MAX_CHUNK_BYTES == 64 * 1024**2


def test_maintenance_requires_real_data_volume_headroom(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKET_STORAGE_DATABASE_PATH", raising=False)
    with pytest.raises(ValueError, match="MARKET_STORAGE_DATABASE_PATH"):
        require_maintenance_headroom(minimum_bytes=100)
    monkeypatch.setenv("MARKET_STORAGE_DATABASE_PATH", str(tmp_path))
    monkeypatch.setattr("shutil.disk_usage", lambda _: SimpleNamespace(free=99))
    with pytest.raises(RuntimeError, match="measured 99"):
        require_maintenance_headroom(minimum_bytes=100)


def test_backup_token_rechecks_actual_file_bytes(storage):
    token = _backup(storage)
    assert storage._require_verified_backup(token)["status"] == "verified"
    (storage.archive_root.parent.parent / "postgres-backups" / "fixture.dump").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="verified NAS"):
        storage._require_verified_backup(token)


def test_legacy_table_cannot_be_written_by_application_role(storage):
    decision_id = _decision(storage.runtime)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), storage.runtime.transaction() as connection:
        connection.execute("SET LOCAL ROLE market_app")
        connection.execute("INSERT INTO analysis.ticker_input_manifest_legacy (ticker_decision_id, field, source_id, available_at) VALUES (%s, 'x', 'x', now())", [decision_id])


def test_plan_includes_toast_and_partition_children_without_scanning_json(storage):
    _decision(storage.runtime, context={"retained": "context"})
    plan = storage.plan()
    relations = {row["relation"]: row for row in plan["tables"]}
    assert "analysis.ticker_decision" in relations
    assert "analysis.ticker_input_manifest_legacy" in relations
    assert "app.publication_payload" in relations
    assert "toast_bytes" in relations["analysis.ticker_decision"]
    assert plan["decision_manifest_chunk_limit_bytes"] == MAX_CHUNK_BYTES


def test_option_plan_does_not_write_and_scratch_uses_configured_dsn(storage, monkeypatch, tmp_path):
    archive_call = Mock(side_effect=AssertionError("dry run must not archive"))
    monkeypatch.setattr(storage, "_archive_option_partition", archive_call)
    with storage.runtime.transaction() as connection:
        connection.execute("SELECT raw.ensure_option_quote_partition('option_quote_202001', '2020-01-01', '2020-02-01')")
    plan = storage.archive_options(now=datetime(2028, 1, 1, tzinfo=UTC))
    assert plan["status"] == "dry_run" and plan["candidates"]
    assert archive_call.call_count == 0
    path = tmp_path / "options.dump"
    path.write_bytes(b"test dump")
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout="listing" if "--list" in args else "")
    monkeypatch.setattr("investment_panel.infrastructure.postgres.storage_archive.subprocess.run", command)
    monkeypatch.setenv("MARKET_ARCHIVE_VERIFY_DATABASE_URL", "postgresql://archive_user@nas-test:5544/postgres?sslmode=require")
    result = storage._verify_native_dump(path, sha256(path.read_bytes()).hexdigest(), 0,
                                        sha256(b"listing").hexdigest(), scratch=True)
    assert result[0]
    for args in calls:
        if "--dbname" in args:
            dsn = conninfo_to_dict(args[args.index("--dbname") + 1])
            assert dsn["host"] == "nas-test" and dsn["port"] == "5544" and dsn["user"] == "archive_user"
            assert dsn["dbname"].startswith("market_archive_verify_")
            assert dsn["sslmode"] == "require"


def _publications(runtime):
    reference = datetime.now(UTC)
    analysis = AnalysisRepository(runtime)
    ids = []
    for i in range(2):
        run = analysis.start_run("storage-test", input_cutoff=reference, code_version=f"v{i}", inputs={"i": i})
        analysis.finish_run(run, "succeeded")
        ids.append(analysis.publish(run, "research", {"brief": [{"stable_key": "one", "evidence": {"i": i}}]}))
    with runtime.transaction() as connection:
        connection.execute("UPDATE app.publication SET created_at = %s, published_at = %s WHERE id = %s",
                           [reference - timedelta(days=100), reference - timedelta(days=100), ids[0]])
    return reference, ids


def test_publication_retention_requires_archive_and_preserves_full_rows(storage, monkeypatch):
    reference, ids = _publications(storage.runtime)
    monkeypatch.delenv("MARKET_STORAGE_ARCHIVE_DIR", raising=False)
    assert RetentionRepository(storage.runtime).prune_publications(now=reference)["publication_archive_required"] == 1
    retention = RetentionRepository(storage.runtime, archive_root=storage.archive_root)
    assert retention.prune_publications(now=reference)["publications"] == 1
    objects = [json.loads(gzip.decompress(path.read_bytes())) for path in storage.archive_root.rglob("*.json.gz")]
    assert any(item["relation"] == "app.publication" and json.loads(item["row_json"])["id"] == str(ids[0]) for item in objects)
    assert any(item["relation"] == "app.publication_payload" and json.loads(item["row_json"])["payload"]["evidence"] == {"i": 0} for item in objects)
    assert any(item["relation"] == "analysis.run" for item in objects)
    assert storage.verify()["failed"] == 0
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) FROM app.publication WHERE id = %s", [ids[1]]).fetchone()["count"] == 1


def test_publication_export_failure_aborts_delete(storage, monkeypatch):
    reference, ids = _publications(storage.runtime)
    retention = RetentionRepository(storage.runtime, archive_root=storage.archive_root)
    monkeypatch.setattr(retention.archive.service, "_write_json_gzip", Mock(side_effect=OSError("NAS unavailable")))
    with pytest.raises(OSError, match="NAS unavailable"):
        retention.prune_publications(now=reference)
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) FROM app.publication WHERE id = ANY(%s)", [ids]).fetchone()["count"] == 2
        assert connection.execute("SELECT count(*) FROM app.publication_payload").fetchone()["count"] == 2


def test_context_backfill_bounds_bytes_as_well_as_rows(storage, monkeypatch):
    _decision(storage.runtime, context={"raw": "x" * 60})
    _decision(storage.runtime, revision="second", context={"raw": "x" * 60})
    monkeypatch.setattr("investment_panel.infrastructure.postgres.decision_storage.MAX_CONTEXT_BATCH_BYTES", 100)
    assert compact_context_batch(storage.runtime, batch_size=100, execute=True)["compacted"] == 1
    assert compact_context_batch(storage.runtime, batch_size=100, execute=True)["compacted"] == 1


def test_context_backfill_refuses_numeric_precision_loss(storage):
    decision_id = _decision(storage.runtime)
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET market_state_snapshot = '{\"exact\":9007199254740993.1}' WHERE id = %s", [decision_id])
    with pytest.raises(ValueError, match="changed original JSON"):
        compact_context_batch(storage.runtime, execute=True)
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT market_state_context_hash FROM analysis.ticker_decision WHERE id = %s", [decision_id]).fetchone()["market_state_context_hash"] is None
        assert connection.execute("SELECT count(*) FROM analysis.decision_context").fetchone()["count"] == 0


def test_publication_archive_preserves_exact_postgres_numeric_text(storage):
    from investment_panel.infrastructure.postgres.publication_archive import PublicationArchive
    _, ids = _publications(storage.runtime)
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.run SET inputs = '{\"exact\":9007199254740993.1}' WHERE id = (SELECT analysis_run_id FROM app.publication WHERE id = %s)", [ids[0]])
        PublicationArchive(storage.runtime, storage.archive_root).publications(connection, [ids[0]])
    objects = [json.loads(gzip.decompress(path.read_bytes())) for path in storage.archive_root.rglob("*.json.gz")]
    assert any(item["relation"] == "analysis.run" and "9007199254740993.1" in item["row_json"] for item in objects)


def test_context_backfill_finishes_partially_normalized_decision(storage):
    context, policy = {"facts": ["retained"]}, {"version": "risk.v1"}
    decision_id = _decision(storage.runtime, context=context, policy=policy)
    with storage.runtime.transaction() as connection:
        digest = store_context(connection, context)
        connection.execute("UPDATE analysis.ticker_decision SET market_state_context_hash = %s, market_state_snapshot = '{}' WHERE id = %s", [digest, decision_id])
    assert compact_context_batch(storage.runtime, execute=True)["compacted"] == 1
    with storage.runtime.read() as connection:
        result = connection.execute("SELECT market_state_snapshot, risk_policy_snapshot FROM analysis.ticker_decision_read WHERE id = %s", [decision_id]).fetchone()
        assert result == {"market_state_snapshot": context, "risk_policy_snapshot": policy}
    assert compact_context_batch(storage.runtime, execute=True)["compacted"] == 0
