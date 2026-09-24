"""End-to-end storage contracts against migrated PostgreSQL and a NAS fixture."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import gzip
import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest
from psycopg.types.json import Jsonb

from investment_panel.infrastructure.postgres.decision_inputs import compact_input_batch
from investment_panel.infrastructure.postgres.hot_retention import HotRetention
from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.instruments import reconcile_instrument
from investment_panel.infrastructure.postgres.row_archive import RowArchive, MAX_PACK_BYTES
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.infrastructure.postgres.storage_archive import StorageArchiveService


@pytest.fixture
def storage(application_postgres_dsn, tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_STORAGE_DATABASE_PATH", str(tmp_path))
    monkeypatch.setattr("shutil.disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3, total=200 * 1024**3))
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    root = tmp_path / "nas" / "archive"
    root.parent.mkdir()
    try:
        yield StorageArchiveService(runtime, root)
    finally:
        runtime.close()


def _decision(runtime, revision, manifest):
    with runtime.transaction() as connection:
        instrument_id = reconcile_instrument(connection, "HOT")
        return connection.execute("""
            INSERT INTO analysis.ticker_decision
                (instrument_id, decision_revision, contract_version, as_of, input_hash,
                 code_version, experiment_id, tactical, fundamental, capital_action,
                 risk_policy, input_manifest)
            VALUES (%s, %s, 'test', now(), %s, 'test', 'test', '{}', '{}', '{}', '{}', %s::jsonb)
            RETURNING id
        """, [instrument_id, revision, "a" * 64, manifest]).fetchone()["id"]


def test_input_normalization_lossless_precision_sharing_and_restart(storage):
    # Pass PostgreSQL JSON text directly: Python float must never round this.
    raw = '{"inputs":{"fundamentals":[{"raw":"' + "中文" * 1500 + '","exact":0.12345678901234567890123456789,"revision":null}],"quote":[1,2,null]},"trade_plan":{"id":"keep"},"input_hash":"original"}'
    ids = [_decision(storage.runtime, str(i), raw) for i in range(3)]
    assert compact_input_batch(storage.runtime)["remaining"] == 3
    assert not storage.archive_root.exists()
    assert compact_input_batch(storage.runtime, batch_size=1, execute=True)["compacted"] == 1
    assert compact_input_batch(storage.runtime, execute=True)["compacted"] == 2
    assert compact_input_batch(storage.runtime, execute=True)["compacted"] == 0
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) AS n FROM analysis.decision_input_payload").fetchone()["n"] == 1
        for decision_id in ids:
            assert connection.execute("SELECT input_manifest = %s::jsonb AS same FROM analysis.ticker_decision_read WHERE id = %s", [raw, decision_id]).fetchone()["same"]
            base = connection.execute("SELECT input_manifest, input_payload_refs FROM analysis.ticker_decision WHERE id = %s", [decision_id]).fetchone()
            assert "fundamentals" not in base["input_manifest"]["inputs"]
            assert base["input_manifest"]["trade_plan"] == {"id": "keep"}
            assert base["input_payload_refs"].keys() == {"fundamentals"}
    with pytest.raises(psycopg.Error), storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.decision_input_payload SET payload = '{}'::jsonb")
    with pytest.raises(psycopg.Error), storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET input_payload_refs = %s WHERE id = %s", [Jsonb({"x": "f" * 64}), ids[0]])


def test_small_and_empty_manifests_are_marked_without_starving_backfill(storage):
    for i, raw in enumerate(('{}', '{"inputs":{}}', '{"inputs":{"x":null}}', '{"inputs":{"x":[1]}}')):
        _decision(storage.runtime, str(i), raw)
    assert compact_input_batch(storage.runtime, execute=True)["compacted"] == 4
    assert compact_input_batch(storage.runtime, execute=True)["compacted"] == 0


def test_row_packs_restore_original_typed_rows_and_batch_metadata(storage):
    ids = [_decision(storage.runtime, str(i), '{"inputs":{}}') for i in range(5)]
    with storage.runtime.transaction() as connection:
        records = connection.execute("SELECT to_jsonb(d)::text AS row_json FROM analysis.ticker_decision d WHERE id = ANY(%s) ORDER BY id", [ids]).fetchall()
        packs = RowArchive(storage).write(connection, "analysis.ticker_decision", records)
    assert len(packs) == 1
    assert storage.verify(manifest_id=packs[0])["verified"] == 1
    with storage.runtime.read() as connection:
        manifest = connection.execute("SELECT * FROM ops.storage_archive_manifest WHERE id = %s", [packs[0]]).fetchone()
        with gzip.open(manifest["nas_uri"], "rt") as handle:
            payload = json.load(handle)
        assert len(payload) == 5
        assert payload[0]["columns"]
        for row in payload:
            assert connection.execute("SELECT to_jsonb(jsonb_populate_record(NULL::analysis.ticker_decision, %s::jsonb)) = %s::jsonb AS same", [row["row_json"], row["row_json"]]).fetchone()["same"]
    with storage.runtime.transaction() as connection:
        assert RowArchive(storage).write(connection, "analysis.ticker_decision", records) == packs


def _quotes(storage, *, count=3, old_days=20, profile="radar"):
    now = datetime(2026, 9, 24, 12, tzinfo=UTC)
    repo = IngestionRepository(storage.runtime)
    repo.register_source("hot-test", name="Hot test", family="test", kind="option_chain")
    for label, at in (("old", now - timedelta(days=old_days)), ("new", now - timedelta(days=1))):
        run = repo.start_run("hot-test", "option_quotes", source_run_key=label, started_at=at)
        repo.store_option_snapshot(run, source_id="hot-test", observed_at=at,
            market_session="regular", universe="hot-test", rows=[{
                "symbol": "NVDA", "expiration": "2027-01-15", "strike": 200 + i,
                "option_type": "call", "mid": 5, "provider_payload": {"original": "x" * 2000, "instrument": {"id": f"fixture-{i}"}},
            } for i in range(count)])
        repo.finish_run(run, "succeeded")
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE raw.option_snapshot SET collection_profile = %s WHERE source_id = 'hot-test'", [profile])
        # Exercise real provider payloads independent of ingestion's packing policy.
        connection.execute("UPDATE raw.option_quote SET provider_payload = %s", [Jsonb({"raw": "x" * 2000, "precise": "unchanged"})])
    return now


def test_options_keep_latest_and_warm_history_but_archive_raw_envelopes(storage):
    now = _quotes(storage, profile="history_full")
    worker = HotRetention(storage)
    first = worker.run(phase="options", now=now, batch_size=2, max_batches=1, execute=True)
    assert first["option_provider_payloads"] == 2
    assert first["option_quotes"] == 0
    second = worker.run(phase="options", now=now, batch_size=2, max_batches=3, execute=True)
    assert second["option_provider_payloads"] == 1
    with storage.runtime.read() as connection:
        rows = connection.execute("SELECT q.observed_at, q.mid, q.provider_payload FROM raw.option_quote q ORDER BY q.observed_at").fetchall()
        assert len(rows) == 6 and all(row["mid"] == 5 for row in rows)
        assert all(not row["provider_payload"] for row in rows[:3])
        assert all(row["provider_payload"] for row in rows[3:])
    assert storage.verify()["failed"] == 0


def test_options_archive_before_delete_and_resume_partial_snapshot(storage):
    now = _quotes(storage, count=5)
    worker = HotRetention(storage)
    assert worker.run(phase="options", now=now, batch_size=2, execute=False)["dry_run"]
    assert not storage.archive_root.exists()
    assert worker.run(phase="options", now=now, batch_size=2, execute=True)["option_quotes"] == 2
    assert worker.run(phase="options", now=now, batch_size=2, max_batches=5, execute=True)["option_quotes"] == 3
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) AS n FROM raw.option_quote").fetchone()["n"] == 5
        assert connection.execute("SELECT count(*) AS n FROM raw.option_snapshot").fetchone()["n"] == 2
    assert storage.verify()["failed"] == 0


@pytest.mark.parametrize("failure", ["write", "checkpoint"])
def test_failure_keeps_source_and_committed_cursor(storage, monkeypatch, failure):
    now = _quotes(storage)
    worker = HotRetention(storage)
    if failure == "write":
        monkeypatch.setattr(worker.archive, "write", Mock(side_effect=OSError("NAS offline")))
    else:
        monkeypatch.setattr(worker, "_checkpoint", Mock(side_effect=RuntimeError("commit interrupted")))
    with pytest.raises((OSError, RuntimeError)):
        worker.run(phase="options", now=now, execute=True)
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) AS n FROM raw.option_quote").fetchone()["n"] == 6
    assert not storage._checkpoint_cursor("hot-options-v1").get("snapshot_id")


def test_corrupt_pack_is_not_reused(storage):
    decision_id = _decision(storage.runtime, "corrupt", '{"inputs":{}}')
    with storage.runtime.transaction() as connection:
        records = connection.execute("SELECT to_jsonb(d)::text AS row_json FROM analysis.ticker_decision d WHERE id = %s", [decision_id]).fetchall()
        ids = RowArchive(storage).write(connection, "analysis.ticker_decision", records)
    with storage.runtime.read() as connection:
        path = connection.execute("SELECT nas_uri FROM ops.storage_archive_manifest WHERE id = %s", [ids[0]]).fetchone()["nas_uri"]
    from pathlib import Path
    Path(path).write_bytes(b"corrupt")
    with pytest.raises(ValueError), storage.runtime.transaction() as connection:
        RowArchive(storage).write(connection, "analysis.ticker_decision", records)


def test_oversized_pack_and_invalid_batch_fail_before_mutation(storage):
    with storage.runtime.transaction() as connection:
        with pytest.raises(ValueError, match="budget"):
            RowArchive(storage).write(connection, "analysis.ticker_decision", [{"row_json": "x" * (MAX_PACK_BYTES + 1)}])
    with pytest.raises(ValueError):
        HotRetention(storage).run(phase="options", batch_size=0, execute=True)


def test_quote_timestamp_can_differ_from_snapshot_and_still_archives(storage):
    now = _quotes(storage, count=2)
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE raw.option_quote SET observed_at = observed_at - interval '2 minutes'")
    result = HotRetention(storage).run(phase="options", now=now, execute=True, max_batches=4)
    assert result["option_quotes"] == 2
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) AS n FROM raw.option_quote").fetchone()["n"] == 2


def test_old_latest_capture_is_retained_even_without_new_prices(storage):
    now = _quotes(storage, count=2)
    with storage.runtime.transaction() as connection:
        connection.execute("DELETE FROM raw.option_quote WHERE observed_at > %s", [now - timedelta(days=2)])
        connection.execute("DELETE FROM raw.option_snapshot WHERE observed_at > %s", [now - timedelta(days=2)])
    assert HotRetention(storage).run(phase="options", now=now, execute=True, max_batches=4)["option_quotes"] == 0
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) AS n FROM raw.option_quote WHERE provider_payload <> '{}'::jsonb").fetchone()["n"] == 2


def test_scheduler_has_bounded_regular_storage_job(monkeypatch):
    from investment_panel.core.job_policy import scheduler_intervals, job_definition
    from tests.conftest import typed_config
    monkeypatch.delenv("MARKET_STORAGE_RETENTION_SECONDS", raising=False)
    assert scheduler_intervals(typed_config())["postgres_retention"] == 3600
    assert job_definition("postgres_retention").timeout_seconds == 180
    monkeypatch.setenv("MARKET_STORAGE_RETENTION_SECONDS", "0")
    assert "postgres_retention" not in scheduler_intervals(typed_config())


def test_health_distinguishes_actual_database_volume_from_working_directory(storage, monkeypatch):
    health = storage.health()
    assert health["database_volume"]["status"] == "measured"
    assert health["database_volume"]["free_bytes"] == 100 * 1024**3
    relations = {row["relation"] for row in health["table_sizes"]}
    assert {"analysis.decision_input_payload", "analysis.option_decision", "analysis.decision", "analysis.decision_evidence", "analysis.option_feature", "app.publication_bundle_item"} <= relations
    monkeypatch.delenv("MARKET_STORAGE_DATABASE_PATH")
    health = storage.health()
    assert health["database_volume"]["status"] == "unconfigured"
    assert health["database_volume"]["free_bytes"] is None
    assert health["projected_free_space_bytes"] is None


def test_relative_value_cleanup_skips_protected_prefix_and_keeps_recent(storage):
    now = _quotes(storage, count=1)
    with storage.runtime.transaction() as connection:
        snapshot = connection.execute("SELECT id, ingest_run_id FROM raw.option_snapshot ORDER BY id LIMIT 1").fetchone()
        generation = connection.execute("""INSERT INTO raw.option_capture_generation
            (snapshot_id, ingest_run_id, generation, capture_state) VALUES (%s, %s, 1, 'complete') RETURNING id""", [snapshot["id"], snapshot["ingest_run_id"]]).fetchone()["id"]
        contract_id = connection.execute("SELECT contract_id FROM raw.option_quote LIMIT 1").fetchone()["contract_id"]
        run = connection.execute("""INSERT INTO analysis.run (run_type, input_cutoff, code_version, input_hash, started_at, status)
            VALUES ('storage-test', %s, 'test', %s, now(), 'succeeded') RETURNING id""", [now, "c" * 64]).fetchone()["id"]
        ids = []
        for i, (classification, age) in enumerate((("historical_static_arbitrage_candidate", 90), ("relative_cheap", 90), ("rejected", 90), ("relative_rich", 1))):
            ids.append(connection.execute("""INSERT INTO analysis.option_relative_value
                (analysis_run_id, capture_generation_id, contract_id, model_revision, classification, quality_status, created_at)
                VALUES (%s, %s, %s, %s, %s, 'available', %s) RETURNING id""", [run, generation, contract_id, f"test-{i}", classification, now - timedelta(days=age)]).fetchone()["id"])
    worker = HotRetention(storage)
    assert worker.run(phase="relative-values", now=now, batch_size=1, execute=True)["relative_values"] == 0
    assert worker.run(phase="relative-values", now=now, batch_size=1, max_batches=5, execute=True)["relative_values"] == 2
    with storage.runtime.read() as connection:
        assert [r["id"] for r in connection.execute("SELECT id FROM analysis.option_relative_value ORDER BY id")] == [ids[0], ids[3]]
        assert connection.execute("SELECT count(*) AS n FROM analysis.run WHERE id = %s", [run]).fetchone()["n"] == 1
    assert storage.verify()["failed"] == 0


def test_regular_retention_with_app_login_does_not_grant_run_cascade(storage):
    from investment_panel.infrastructure.postgres.analysis import AnalysisRepository
    from investment_panel.infrastructure.postgres.jobs import JobRepository
    from investment_panel.infrastructure.postgres.retention import RetentionRepository

    now = datetime.now(UTC)
    old = now - timedelta(days=90)
    analysis = AnalysisRepository(storage.runtime)
    runs = [analysis.start_run("storage-role-test", input_cutoff=old,
                code_version="test", inputs={"revision": i}) for i in range(3)]
    for run in runs:
        analysis.finish_run(run, "succeeded")
    recent_run = analysis.start_run("storage-role-test", input_cutoff=now,
                code_version="test", inputs={"revision": "recent"})
    analysis.finish_run(recent_run, "succeeded")
    analysis.publish(runs[0], "today", {"daily_brief": [{"stable_key": "brief", "headline": "old"}]})
    analysis.publish(runs[1], "today", {"daily_brief": [{"stable_key": "brief", "headline": "current"}]})
    jobs = JobRepository(storage.runtime)
    job = jobs.start("old-storage-role-test")
    jobs.finish(job["id"], "succeeded")
    with storage.runtime.transaction() as connection:
        assert connection.execute("SELECT current_user AS role").fetchone()["role"] == "market_app"
        connection.execute("UPDATE analysis.run SET started_at = %s WHERE id = ANY(%s)", [old, runs])
        connection.execute("UPDATE app.publication SET created_at = %s, published_at = %s WHERE status = 'superseded'", [old, old])
        connection.execute("UPDATE ops.job_run SET started_at = %s, finished_at = %s WHERE id = %s", [old, old, job["id"]])
        assert not connection.execute("SELECT has_table_privilege('market_app', 'analysis.run', 'DELETE') AS allowed").fetchone()["allowed"]
        with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
            connection.execute("DELETE FROM analysis.run WHERE false")
    counts = RetentionRepository(storage.runtime, archive_root=storage.archive_root).prune(now=now)
    assert counts["publications"] == 1
    assert counts["analysis_runs"] == 2
    assert counts["job_runs"] == 1
    with storage.runtime.transaction() as connection:
        retained = {row["id"] for row in connection.execute("SELECT id FROM analysis.run").fetchall()}
        assert retained == {runs[1], recent_run}
        assert connection.execute("SELECT count(*) AS n FROM analysis.run WHERE id = %s", [recent_run]).fetchone()["n"] == 1
        assert connection.execute(
            "SELECT analysis.prune_empty_run_metadata(%s::uuid[], %s) AS n", [[recent_run], old],
        ).fetchone()["n"] == 0
        with pytest.raises(psycopg.Error, match="bounded"), connection.transaction():
            connection.execute("SELECT analysis.prune_empty_run_metadata(%s::uuid[], %s)", [[runs[1]] * 101, old])
    assert storage.verify()["failed"] == 0


def test_relative_value_pin_waits_for_inflight_publication(storage, monkeypatch):
    now = _quotes(storage, old_days=800, profile="history_full")
    from investment_panel.infrastructure.postgres.analysis import AnalysisRepository

    analysis = AnalysisRepository(storage.runtime)
    run_id = analysis.start_run("storage-pin-race", input_cutoff=now, code_version="test", inputs={})
    analysis.finish_run(run_id, "succeeded")
    old = now - timedelta(days=90)
    with storage.runtime.transaction() as connection:
        snapshot = connection.execute(
            "SELECT id, ingest_run_id FROM raw.option_snapshot WHERE source_id = 'hot-test' ORDER BY observed_at LIMIT 1"
        ).fetchone()
        quote = connection.execute(
            "SELECT contract_id FROM raw.option_quote WHERE snapshot_id = %s ORDER BY contract_id LIMIT 1",
            [snapshot["id"]],
        ).fetchone()
        generation_id = connection.execute("""
            INSERT INTO raw.option_capture_generation
                (snapshot_id, ingest_run_id, generation, capture_state, expected_contract_count,
                 received_contract_count, completeness, capture_started_at, capture_finished_at)
            SELECT %s, %s, 1, 'complete', count(*), count(*), 1, %s, %s
            FROM raw.option_quote WHERE snapshot_id = %s
            RETURNING id
        """, [snapshot["id"], snapshot["ingest_run_id"], old, old, snapshot["id"]]).fetchone()["id"]
        connection.execute(
            "UPDATE raw.option_quote SET capture_generation_id = %s WHERE snapshot_id = %s",
            [generation_id, snapshot["id"]],
        )
        connection.execute("""
            INSERT INTO analysis.option_relative_value
                (analysis_run_id, capture_generation_id, contract_id, model_revision, classification,
                 quality_status, created_at)
            VALUES (%s, %s, %s, 'test', 'relative_cheap', 'available', %s)
        """, [run_id, generation_id, quote["contract_id"], old])

    publication_started, publish = Event(), Event()
    archive_written = Event()
    worker = HotRetention(storage)
    write_archive = worker.archive.write

    def signal_archive(connection, relation, records):
        result = write_archive(connection, relation, records)
        if relation == "analysis.option_relative_value":
            archive_written.set()
        return result

    monkeypatch.setattr(worker.archive, "write", signal_archive)

    def publish_while_retention_runs():
        with storage.runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO app.publication (scope, analysis_run_id, status) VALUES ('storage-pin-race', %s, 'published')",
                [run_id],
            )
            publication_started.set()
            assert publish.wait(10)

    with ThreadPoolExecutor(max_workers=2) as executor:
        publisher = executor.submit(publish_while_retention_runs)
        assert publication_started.wait(5)
        retention = executor.submit(worker.run, phase="relative-values", now=now, execute=True)
        try:
            assert archive_written.wait(5)
            with pytest.raises(TimeoutError):
                retention.result(timeout=1)
        finally:
            publish.set()
        publisher.result(timeout=5)
        assert retention.result(timeout=5)["relative_values"] == 0

    with storage.runtime.read() as connection:
        assert connection.execute(
            "SELECT count(*) AS n FROM analysis.option_relative_value WHERE analysis_run_id = %s", [run_id],
        ).fetchone()["n"] == 1
    assert storage.verify()["failed"] == 0
