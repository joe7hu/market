"""End-to-end storage contracts against migrated PostgreSQL and a NAS fixture."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import gzip
from hashlib import sha256
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from investment_panel.infrastructure.postgres.decision_inputs import compact_input_batch
from investment_panel.infrastructure.postgres.decision_storage import backfill_ranking_publication_payloads, store_context
from investment_panel.infrastructure.postgres.analysis import AnalysisRepository
from investment_panel.infrastructure.postgres.hot_retention import HotRetention
from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.instruments import reconcile_instrument
from investment_panel.infrastructure.postgres.row_archive import RowArchive, MAX_PACK_BYTES
from investment_panel.infrastructure.postgres.option_evidence_archive import OptionEvidenceArchive
from investment_panel.infrastructure.postgres.ticker_evidence_archive import TickerEvidenceArchive
from investment_panel.infrastructure.postgres.archive_io import MAX_CHUNK_BYTES
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.infrastructure.postgres.storage_archive import StorageArchiveService
from investment_panel.domain.decision import build_ticker_decision
from investment_panel.infrastructure.postgres.ticker_decisions import TickerDecisionRepository


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


def _complete_ticker(runtime, decision_id):
    with runtime.transaction() as connection:
        connection.execute("""UPDATE analysis.ticker_decision
            SET ranking_ref_checked = true, evidence_normalized = true WHERE id = %s""",
                           [decision_id])
        for horizon, sessions in (("TACTICAL", (1, 5, 20)), ("FUNDAMENTAL", (63, 126, 252))):
            for horizon_sessions in sessions:
                connection.execute("""INSERT INTO analysis.ticker_outcome
                    (ticker_decision_id, horizon, horizon_sessions, state)
                    VALUES (%s, %s, %s, 'resolved')
                    ON CONFLICT (ticker_decision_id, horizon, horizon_sessions)
                    DO UPDATE SET state = 'resolved'""", [decision_id, horizon, horizon_sessions])


def test_application_role_can_intern_decision_and_write_accounting(storage):
    assert storage.account(record=True)["sample_count"] == 1
    with storage.runtime.transaction() as connection:
        reconcile_instrument(connection, "APPROLE")
    at = datetime(2026, 9, 25, 14, tzinfo=UTC)
    decision = build_ticker_decision("APPROLE", {"quotes": [{
        "symbol": "APPROLE", "price": 100, "observed_at": at - timedelta(minutes=1),
        "available_at": at - timedelta(minutes=1), "confirmed": True,
    }]}, as_of=at)
    assert TickerDecisionRepository(storage.runtime).publish(decision)["status"] == "published"


def test_storage_forecast_keeps_fallback_until_three_distinct_days(storage):
    today = datetime.now(UTC).date()
    with storage.runtime.transaction() as connection:
        for day in (today - timedelta(days=1), today):
            connection.execute("""
                INSERT INTO ops.storage_daily_accounting
                  (sample_day, sampled_at, database_bytes, volume_free_bytes,
                   logical_evidence_bytes, archived_bytes, archive_rows)
                VALUES (%s, now(), 1000, %s, 100, 0, 0)
            """, [day, 100 * 1024**3])
    forecast = storage.account()
    assert forecast["sample_count"] == 2
    assert forecast["forecast_confidence"] == "provisional"
    assert forecast["forecast_30d_free_bytes"] <= 79 * 1024**3


def test_storage_account_reports_old_local_evidence_bytes_separately(storage):
    decision_id = _decision(storage.runtime, "protected-old", Jsonb({"inputs": {"raw": "x" * 4096}}))
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET as_of = now() - interval '40 days' WHERE id = %s",
                           [decision_id])
    account = storage.account()
    assert account["protected_bytes"] > 0
    assert account["protected_rows"]["ticker_decisions"] >= 1
    assert account["protected_bytes_status"] == "old_local_row_lower_bound_excludes_shared_dependencies"


def test_representative_busy_day_replay_measures_unique_writes(storage):
    symbols = [f"BUSY{i:03d}" for i in range(100)]
    at = datetime(2026, 9, 25, 14, tzinfo=UTC)
    with storage.runtime.transaction() as connection:
        for symbol in symbols:
            reconcile_instrument(connection, symbol)
    decisions = TickerDecisionRepository(storage.runtime)
    analysis = AnalysisRepository(storage.runtime)
    published_decisions = unchanged = 0
    publication_ids = []
    for check in range(3):
        check_at = at + timedelta(minutes=check)
        ranks = []
        for index, symbol in enumerate(symbols):
            corrected = check == 2 and index < 10
            quote = {"symbol": symbol, "price": 101 if corrected else 100,
                     "observed_at": at - timedelta(minutes=1),
                     "available_at": at - timedelta(minutes=1), "confirmed": True}
            result = decisions.publish(build_ticker_decision(symbol, {"quotes": [quote]}, as_of=check_at))
            published_decisions += result["status"] == "published"
            unchanged += result["status"] == "unchanged"
            ranks.append({"ticker": symbol, "stable_key": symbol, "trade_rank": index + 1,
                          "source_price": quote["price"]})
        run_id = analysis.start_run("ticker-opportunity-ranking", input_cutoff=check_at,
                                    code_version="busy-day.v1", inputs={"source_day": at.date().isoformat(),
                                                                          "revision": int(check == 2)})
        publication_ids.append(analysis.publish(run_id, "ticker-opportunity-ranking",
                                                 {"opportunity_rank": ranks}))
    with storage.runtime.read() as connection:
        measured = connection.execute("""
            SELECT
              (SELECT count(*) FROM analysis.ticker_decision) AS decisions,
              (SELECT count(*) FROM analysis.ticker_decision_checkpoint) AS checkpoints,
              (SELECT count(*) FROM analysis.decision_input_payload) AS unique_payloads,
              (SELECT COALESCE(sum(pg_column_size(payload)), 0)
               FROM analysis.decision_input_payload) AS unique_payload_bytes,
              (SELECT count(*) FROM app.publication) AS publications,
              (SELECT count(*) FROM app.publication_bundle_item) AS publication_items,
              (SELECT count(*) FROM app.publication_payload) AS duplicate_publication_payloads
        """).fetchone()
    assert published_decisions == measured["decisions"] == 110
    assert unchanged == 190
    assert publication_ids[0] == publication_ids[1] != publication_ids[2]
    assert measured["publications"] == 2
    assert measured["publication_items"] == 200
    assert measured["duplicate_publication_payloads"] == 0
    assert measured["unique_payload_bytes"] > 0
    print("BUSY_DAY_REPLAY=" + json.dumps({"evaluations": 300, "decision_writes": published_decisions,
        "unchanged_checks": unchanged, **dict(measured)}, default=int, sort_keys=True))


def test_legacy_ranking_publication_payloads_move_once_with_exact_reads(storage):
    raw = '{"score":0.12345678901234567890123456789,"why":"unchanged"}'
    with storage.runtime.transaction() as connection:
        run_id = connection.execute("""INSERT INTO analysis.run
            (run_type, input_cutoff, code_version, input_hash, started_at, status)
            VALUES ('rank-backfill', now(), 'test', %s, now(), 'succeeded') RETURNING id""",
            ["a" * 64]).fetchone()["id"]
        bundle_id = connection.execute("""INSERT INTO app.publication_bundle(scope, bundle_hash, item_count)
            VALUES ('ticker-opportunity-ranking', %s, 1) RETURNING id""", ["b" * 64]).fetchone()["id"]
        publication_id = connection.execute("""INSERT INTO app.publication
            (scope, analysis_run_id, status, bundle_id)
            VALUES ('ticker-opportunity-ranking', %s, 'published', %s) RETURNING id""",
            [run_id, bundle_id]).fetchone()["id"]
        connection.execute("INSERT INTO app.publication_payload(content_hash, payload) VALUES (%s, %s::jsonb)",
                           ["c" * 64, raw])
        connection.execute("""INSERT INTO app.publication_bundle_item
            (bundle_id, model_name, stable_key, rank, content_hash)
            VALUES (%s, 'opportunity_rank', 'ONE', 1, %s)""", [bundle_id, "c" * 64])
        connection.execute("""INSERT INTO app.current_publication_item
            (scope, publication_id, model_name, stable_key, rank, content_hash)
            VALUES ('ticker-opportunity-ranking', %s, 'opportunity_rank', 'ONE', 1, %s)""",
            [publication_id, "c" * 64])
        before = connection.execute("""SELECT payload::text AS body FROM app.publication_bundle_item_read
            WHERE bundle_id = %s""", [bundle_id]).fetchone()["body"]
    assert backfill_ranking_publication_payloads(storage.runtime)["remaining"] == 1
    result = backfill_ranking_publication_payloads(storage.runtime, batch_size=1, execute=True)
    assert result["converted"] == 1
    assert backfill_ranking_publication_payloads(storage.runtime, batch_size=1, execute=True)["converted"] == 0
    with storage.runtime.read() as connection:
        item = connection.execute("""SELECT content_hash, decision_payload_hash, payload::text AS body
            FROM app.publication_bundle_item_read WHERE bundle_id = %s""", [bundle_id]).fetchone()
        current = connection.execute("""SELECT content_hash, decision_payload_hash, payload::text AS body
            FROM app.current_publication_item_read WHERE publication_id = %s""", [publication_id]).fetchone()
        assert item["content_hash"] is None and item["decision_payload_hash"]
        assert current["content_hash"] is None
        assert current["decision_payload_hash"] == item["decision_payload_hash"]
        assert item["body"] == current["body"] == before
        assert connection.execute("SELECT count(*) AS n FROM app.publication_payload").fetchone()["n"] == 0


def test_option_snapshot_and_features_are_read_projections_of_candidate(storage):
    snapshot_keys = ("snapshot_time", "ticker", "underlying_price", "expiration", "strike",
                     "option_type", "bid", "ask", "mid", "volume", "open_interest", "iv",
                     "delta", "dte", "spread_pct", "data_source", "contract_id", "raw")
    feature_keys = ("snapshot_time", "contract_id", "ticker", "required_2x_price",
                    "required_5x_price", "required_10x_price", "required_move_pct",
                    "liquidity_score", "convexity_score", "raw")
    candidate = {key: None for key in (*snapshot_keys, *feature_keys)}
    candidate.update({"candidate_event_id": str(uuid4()), "contract_id": "123", "ticker": "OPTION",
                      "snapshot_time": "2026-09-25T14:00:00+00:00", "mid": 1.25,
                      "required_move_pct": 0.12345678901234568, "raw": {"source": "exact"}})
    snapshot = {key: candidate[key] for key in snapshot_keys}
    feature = {key: candidate[key] for key in feature_keys}
    analysis = AnalysisRepository(storage.runtime)
    run_id = analysis.start_run("options-radar", input_cutoff=datetime.now(UTC),
                                code_version="projection-test", inputs={"candidate": candidate["candidate_event_id"]})
    publication_id = analysis.publish(run_id, "options-radar", {
        "candidate_event": [candidate], "option_snapshot": [snapshot], "option_features": [feature],
    })
    with storage.runtime.read() as connection:
        physical = connection.execute("""SELECT count(*) AS n FROM app.publication_bundle_item item
            JOIN app.publication publication ON publication.bundle_id = item.bundle_id
            WHERE publication.id = %s""", [publication_id]).fetchone()["n"]
        rows = connection.execute("""SELECT model_name, payload FROM app.publication_content_item
            WHERE publication_id = %s ORDER BY model_name""", [publication_id]).fetchall()
    assert physical == 1
    assert {row["model_name"]: row["payload"] for row in rows} == {
        "candidate_event": candidate, "option_snapshot": snapshot, "option_features": feature,
    }
    assert analysis.publication_rows("options-radar", "option_snapshot") == [snapshot]
    assert analysis.publication_rows("options-radar", "option_features") == [feature]
    distinct_feature = {**feature, "distinct_model_result": "keep"}
    second_run = analysis.start_run("options-radar", input_cutoff=datetime.now(UTC),
                                    code_version="projection-test", inputs={"distinct": str(uuid4())})
    second_id = analysis.publish(second_run, "options-radar", {
        "candidate_event": [candidate], "option_snapshot": [snapshot],
        "option_features": [distinct_feature],
    })
    with storage.runtime.read() as connection:
        second = connection.execute("""SELECT bundle.projection_version,
            (SELECT count(*) FROM app.publication_bundle_item item
             WHERE item.bundle_id = bundle.id) AS physical
            FROM app.publication publication JOIN app.publication_bundle bundle
              ON bundle.id = publication.bundle_id WHERE publication.id = %s""",
            [second_id]).fetchone()
    assert second == {"projection_version": None, "physical": 3}
    assert analysis.publication_rows("options-radar", "option_features") == [distinct_feature]


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


def _quotes(storage, *, count=3, old_days=40, profile="radar"):
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


def _completed_option_scan(storage, *, count=1):
    now = _quotes(storage, count=count, old_days=40)
    with storage.runtime.transaction() as connection:
        quote = connection.execute("""
            SELECT q.snapshot_id, q.contract_id, q.observed_at, instrument.id AS instrument_id
            FROM raw.option_quote q JOIN catalog.option_contract contract ON contract.id = q.contract_id
            JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
            ORDER BY q.observed_at LIMIT 1
        """).fetchone()
        old_run = connection.execute("""
            INSERT INTO analysis.run (run_type, input_cutoff, code_version, input_hash, started_at, status)
            VALUES ('option-evidence-fixture', %s, 'fixture', %s, %s, 'succeeded') RETURNING id
        """, [quote["observed_at"], "a" * 64, quote["observed_at"]]).fetchone()["id"]
        newer_run = connection.execute("""
            INSERT INTO analysis.run (run_type, input_cutoff, code_version, input_hash, started_at, status)
            VALUES ('option-evidence-fixture', %s, 'fixture', %s, %s, 'succeeded') RETURNING id
        """, [now, "b" * 64, now]).fetchone()["id"]
        decision_id = connection.execute("""
            INSERT INTO analysis.decision
              (run_id, decision_key, kind, instrument_id, as_of, state, input_hash)
            VALUES (%s, 'old-option', 'option', %s, %s, 'REJECT', %s) RETURNING id
        """, [old_run, quote["instrument_id"], quote["observed_at"], "c" * 64]).fetchone()["id"]
        connection.execute("""
            INSERT INTO analysis.option_decision
              (decision_id, contract_id, snapshot_id, quote_observed_at, paper_state, details)
            VALUES (%s, %s, %s, %s, 'REJECT', %s)
        """, [decision_id, quote["contract_id"], quote["snapshot_id"],
              quote["observed_at"], '{"exact":0.12345678901234567890123456789,"raw":"中文",'
              '"historical_paths":{"lower_95_expected_value":0.12345678901234567890123456789,'
              '"scenario_count":17,"paths":[1,2,3]},"calibration":{"sample_size":17},'
              '"quote_package":{"max_quote_age_seconds":12,"liquidity":{"spread":0.1}},'
              '"thesis":{"invalidation":"price"},"probability_semantics":"calibrated"}'])
        connection.execute("""
            INSERT INTO analysis.decision_evidence
              (decision_id, evidence_kind, reference_key, detail)
            VALUES (%s, 'quote', 'source-v1', %s)
        """, [decision_id, '{"revision":2,"exact":0.12345678901234567890123456789}'])
        new_quote = connection.execute("""
            SELECT q.snapshot_id, q.contract_id, q.observed_at
            FROM raw.option_quote q JOIN catalog.option_contract contract ON contract.id = q.contract_id
            WHERE contract.underlying_instrument_id = %s
            ORDER BY q.observed_at DESC LIMIT 1
        """, [quote["instrument_id"]]).fetchone()
        successor_id = connection.execute("""
            INSERT INTO analysis.decision
              (run_id, decision_key, kind, instrument_id, as_of, state, input_hash)
            VALUES (%s, 'new-option', 'option', %s, %s, 'REJECT', %s) RETURNING id
        """, [newer_run, quote["instrument_id"], now, "d" * 64]).fetchone()["id"]
        connection.execute("""
            INSERT INTO analysis.option_decision
              (decision_id, contract_id, snapshot_id, quote_observed_at, paper_state, details)
            VALUES (%s, %s, %s, %s, 'REJECT', '{}'::jsonb)
        """, [successor_id, new_quote["contract_id"], new_quote["snapshot_id"],
              new_quote["observed_at"]])
    return now, decision_id


def _verified_backup(storage):
    root = storage.archive_root.parent.parent / "postgres-backups"
    root.mkdir(parents=True, exist_ok=True)
    dump = root / "fixture.dump"
    dump.write_bytes(b"verified backup fixture")
    token = sha256(dump.read_bytes()).hexdigest()
    (root / "fixture.json").write_text(json.dumps({"status": "verified", "sha256": token,
                                                  "dump_path": str(dump),
                                                  "created_at": datetime.now(UTC).isoformat(),
                                                  "format": "postgresql-custom"}))
    return token


@pytest.mark.parametrize("copy_pack", [False, True])
def test_completed_ticker_evidence_archives_exact_inputs_and_restores_typed_rows(
    storage, migrated_postgres_dsn, monkeypatch, copy_pack,
):
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    old = _decision(storage.runtime, "ticker-archive-old", Jsonb({
        "inputs": {"quote": {"source_id": "provider", "revision": "first", "raw": "x" * 4000}},
        "source_versions": {"quote": "first"},
    }))
    _decision(storage.runtime, "ticker-archive-new", Jsonb({"inputs": {"quote": {"raw": "new"}}}))
    _complete_ticker(storage.runtime, old)
    with storage.runtime.transaction() as connection:
        connection.execute("""UPDATE analysis.ticker_decision SET as_of = %s, status = 'superseded'
            WHERE id = %s""", [now - timedelta(days=40), old])
        context_hash = store_context(connection, {"revision": "market-first", "raw": "m" * 3000})
        connection.execute("""UPDATE analysis.ticker_decision
            SET market_state_context_hash = %s, market_state_snapshot = '{}'::jsonb
            WHERE id = %s""", [context_hash, old])
    assert compact_input_batch(storage.runtime, execute=True)["compacted"] == 2
    if copy_pack:
        monkeypatch.setattr("investment_panel.infrastructure.postgres.row_archive.MAX_PACK_BYTES", 1024)
    archive = TickerEvidenceArchive(storage)
    assert archive.run(now=now)["eligible"] == 1
    with storage.runtime.read() as connection:
        original = connection.execute("SELECT to_jsonb(decision)::text AS row_json FROM analysis.ticker_decision decision WHERE id = %s",
                                      [old]).fetchone()["row_json"]
    result = archive.run(now=now, execute=True, backup_token=_verified_backup(storage))
    assert result["archived"] == 1
    with storage.runtime.read() as connection:
        compact = connection.execute("""SELECT evidence_state, evidence_archive_manifest_id,
            input_manifest, input_payload_refs, archived_input_refs,
            archived_context_refs, market_state_context_hash
            FROM analysis.ticker_decision WHERE id = %s""", [old]).fetchone()
    assert compact["evidence_state"] == "archived"
    assert compact["evidence_archive_manifest_id"] == result["manifest_id"]
    assert compact["input_manifest"]["inputs"] == {}
    assert compact["input_payload_refs"] == {}
    assert compact["archived_input_refs"]
    assert compact["archived_context_refs"]["market"] == context_hash
    assert compact["market_state_context_hash"] is None
    collected = archive.collect_for_decision(old, execute=True, backup_token=_verified_backup(storage))
    assert collected["payloads_released"] == 1
    assert collected["contexts_released"] == 1
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) AS n FROM analysis.decision_input_payload WHERE content_hash = %s",
                                  [next(iter(compact["archived_input_refs"].values()))]).fetchone()["n"] == 0
        assert connection.execute("SELECT count(*) AS n FROM analysis.decision_context WHERE content_hash = %s",
                                  [context_hash]).fetchone()["n"] == 0
    assert archive.full_evidence(old) == {"status": "archived", "archive_manifest_id": result["manifest_id"]}
    with pytest.raises(psycopg.errors.RaiseException, match="active ticker consumer requires local evidence"):
        with psycopg.connect(migrated_postgres_dsn) as connection:
            connection.execute("""INSERT INTO app.paper_order
                (ticker_decision_id, instrument_id, side, quantity, status)
                SELECT id, instrument_id, 'buy', 1, 'open'
                FROM analysis.ticker_decision WHERE id = %s""", [old])
    with pytest.raises(psycopg.errors.RaiseException, match="active ticker consumer requires local evidence"):
        with psycopg.connect(migrated_postgres_dsn) as connection:
            connection.execute("""INSERT INTO analysis.ticker_outcome
                (ticker_decision_id, horizon, horizon_sessions, state)
                VALUES (%s, 'TACTICAL', 1, 'observing')""", [old])
    with psycopg.connect(migrated_postgres_dsn) as connection:
        connection.execute("DELETE FROM analysis.ticker_outcome WHERE ticker_decision_id = %s", [old])
        connection.execute("ALTER TABLE analysis.ticker_decision DISABLE TRIGGER ticker_decision_archive_state_valid")
        connection.execute("DELETE FROM analysis.ticker_decision WHERE id = %s", [old])
        connection.execute("ALTER TABLE analysis.ticker_decision ENABLE TRIGGER ticker_decision_archive_state_valid")
    receipt = archive.restore_decision(old, manifest_id=result["manifest_id"],
                                       destination_dsn=migrated_postgres_dsn)
    assert receipt["typed_rows"]["analysis.ticker_decision"] == 1
    with psycopg.connect(migrated_postgres_dsn) as connection:
        restored = connection.execute(f"SELECT to_jsonb(decision)::text FROM {receipt['staging_schema']}.analysis_ticker_decision decision").fetchone()[0]
    assert restored == original


def test_ticker_archive_keeps_shared_input_for_current_decision(storage):
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    manifest = Jsonb({"inputs": {"quote": {"raw": "shared" * 1000}}})
    old = _decision(storage.runtime, "ticker-shared-old", manifest)
    current = _decision(storage.runtime, "ticker-shared-new", manifest)
    _complete_ticker(storage.runtime, old)
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET as_of = %s, status = 'superseded' WHERE id = %s",
                           [now - timedelta(days=40), old])
    compact_input_batch(storage.runtime, execute=True)
    with storage.runtime.read() as connection:
        old_hash = connection.execute("SELECT input_payload_refs->>'quote' AS hash FROM analysis.ticker_decision WHERE id = %s",
                                      [old]).fetchone()["hash"]
    result = TickerEvidenceArchive(storage).run(now=now, execute=True, backup_token=_verified_backup(storage))
    assert result["archived"] == 1
    assert TickerEvidenceArchive(storage).collect_for_decision(
        old, execute=True, backup_token=_verified_backup(storage))["payloads_released"] == 0
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) AS n FROM analysis.decision_input_payload WHERE content_hash = %s",
                                  [old_hash]).fetchone()["n"] == 1
        assert connection.execute("SELECT input_payload_refs->>'quote' AS hash FROM analysis.ticker_decision WHERE id = %s",
                                  [current]).fetchone()["hash"] == old_hash
    assert TickerEvidenceArchive(storage).full_evidence(current)["status"] == "complete"


def test_ticker_archive_missing_input_dependency_keeps_source(storage, migrated_postgres_dsn):
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    old = _decision(storage.runtime, "ticker-missing-old", Jsonb({"inputs": {"quote": {"raw": "x" * 4000}}}))
    _decision(storage.runtime, "ticker-missing-new", Jsonb({"inputs": {"quote": {"raw": "new"}}}))
    _complete_ticker(storage.runtime, old)
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET as_of = %s, status = 'superseded' WHERE id = %s",
                           [now - timedelta(days=40), old])
    compact_input_batch(storage.runtime, execute=True)
    with storage.runtime.read() as connection:
        digest = connection.execute("SELECT input_payload_refs->>'quote' AS hash FROM analysis.ticker_decision WHERE id = %s",
                                    [old]).fetchone()["hash"]
    with psycopg.connect(migrated_postgres_dsn) as connection:
        connection.execute("ALTER TABLE analysis.decision_input_payload DISABLE TRIGGER decision_input_payload_immutable")
        connection.execute("DELETE FROM analysis.decision_input_payload WHERE content_hash = %s", [digest])
        connection.execute("ALTER TABLE analysis.decision_input_payload ENABLE TRIGGER decision_input_payload_immutable")
    with pytest.raises(psycopg.errors.RaiseException, match="missing immutable decision input payload"):
        TickerEvidenceArchive(storage).run(now=now, execute=True, backup_token=_verified_backup(storage))
    assert TickerEvidenceArchive(storage).full_evidence(old) == {
        "status": "unavailable", "reason": "local_dependency_missing",
    }
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT evidence_state FROM analysis.ticker_decision WHERE id = %s",
                                  [old]).fetchone()["evidence_state"] == "local"


def test_ticker_archive_waits_for_rank_backfill_and_all_outcomes(storage):
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    old = _decision(storage.runtime, "ticker-incomplete-old", Jsonb({"inputs": {"quote": {"raw": "old"}}}))
    _decision(storage.runtime, "ticker-incomplete-new", Jsonb({"inputs": {"quote": {"raw": "new"}}}))
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET as_of = %s, status = 'superseded' WHERE id = %s",
                           [now - timedelta(days=40), old])
    archive = TickerEvidenceArchive(storage)
    assert archive.run(now=now)["archived"] == 0
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET ranking_ref_checked = true WHERE id = %s", [old])
        connection.execute("""INSERT INTO analysis.ticker_outcome
            (ticker_decision_id, horizon, horizon_sessions, state)
            VALUES (%s, 'TACTICAL', 1, 'resolved')""", [old])
    assert archive.run(now=now)["archived"] == 0
    _complete_ticker(storage.runtime, old)
    assert archive.run(now=now)["archived"] == 0
    compact_input_batch(storage.runtime, execute=True)
    assert archive.run(now=now)["eligible"] == 1


def test_ticker_full_evidence_reports_missing_local_context(storage, migrated_postgres_dsn):
    decision_id = _decision(storage.runtime, "ticker-missing-context", Jsonb({"inputs": {}}))
    with storage.runtime.transaction() as connection:
        digest = store_context(connection, {"revision": "exact-context"})
        connection.execute("UPDATE analysis.ticker_decision SET market_state_context_hash = %s WHERE id = %s",
                           [digest, decision_id])
    with psycopg.connect(migrated_postgres_dsn) as connection:
        connection.execute("ALTER TABLE analysis.decision_context DISABLE TRIGGER ALL")
        connection.execute("DELETE FROM analysis.decision_context WHERE content_hash = %s", [digest])
        connection.execute("ALTER TABLE analysis.decision_context ENABLE TRIGGER ALL")
    assert TickerEvidenceArchive(storage).full_evidence(decision_id) == {
        "status": "unavailable", "reason": "local_dependency_missing",
    }


def test_ticker_archive_requires_rank_forecast_dependency(storage):
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    old = _decision(storage.runtime, "ticker-rank-forecast-old", Jsonb({
        "inputs": {}, "opportunity_rank": {"strategy_forecast_id": str(uuid4())},
    }))
    _decision(storage.runtime, "ticker-rank-forecast-new", Jsonb({"inputs": {}}))
    _complete_ticker(storage.runtime, old)
    compact_input_batch(storage.runtime, execute=True)
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET as_of = %s, status = 'superseded' WHERE id = %s",
                           [now - timedelta(days=40), old])
    with pytest.raises(ValueError, match="strategy_forecast"):
        TickerEvidenceArchive(storage).run(now=now, execute=True, backup_token=_verified_backup(storage))
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT evidence_state FROM analysis.ticker_decision WHERE id = %s",
                                  [old]).fetchone()["evidence_state"] == "local"


@pytest.mark.parametrize("failure", ["interrupt", "corrupt"])
def test_ticker_archive_failed_verification_keeps_source(storage, monkeypatch, failure):
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    old = _decision(storage.runtime, "ticker-failure-old", Jsonb({"inputs": {"quote": {"raw": "old"}}}))
    _decision(storage.runtime, "ticker-failure-new", Jsonb({"inputs": {"quote": {"raw": "new"}}}))
    _complete_ticker(storage.runtime, old)
    compact_input_batch(storage.runtime, execute=True)
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET as_of = %s, status = 'superseded' WHERE id = %s",
                           [now - timedelta(days=40), old])
    archive = TickerEvidenceArchive(storage)
    if failure == "interrupt":
        monkeypatch.setattr(archive, "restore_decision", Mock(side_effect=RuntimeError("interrupted")))
        expected = RuntimeError
    else:
        dependencies = archive._dependencies

        def corrupt(connection, rows):
            packs, hashes, contexts = dependencies(connection, rows)
            manifest = connection.execute("SELECT nas_uri FROM ops.storage_archive_manifest WHERE id = %s",
                                          [packs["analysis.ticker_decision"][0]]).fetchone()
            Path(manifest["nas_uri"]).write_bytes(b"corrupt")
            return packs, hashes, contexts

        monkeypatch.setattr(archive, "_dependencies", corrupt)
        expected = ValueError
    with pytest.raises(expected):
        archive.run(now=now, execute=True, backup_token=_verified_backup(storage))
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT evidence_state FROM analysis.ticker_decision WHERE id = %s",
                                  [old]).fetchone()["evidence_state"] == "local"


def test_concurrent_ticker_order_keeps_source_local(storage, migrated_postgres_dsn):
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    old = _decision(storage.runtime, "ticker-concurrent-old", Jsonb({"inputs": {"quote": {"raw": "old"}}}))
    _decision(storage.runtime, "ticker-concurrent-new", Jsonb({"inputs": {"quote": {"raw": "new"}}}))
    _complete_ticker(storage.runtime, old)
    compact_input_batch(storage.runtime, execute=True)
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET as_of = %s, status = 'superseded' WHERE id = %s",
                           [now - timedelta(days=40), old])
    inserted, commit = Event(), Event()

    def add_order():
        with psycopg.connect(migrated_postgres_dsn) as connection:
            connection.execute("""INSERT INTO app.paper_order
                (ticker_decision_id, instrument_id, side, quantity, status)
                SELECT id, instrument_id, 'buy', 1, 'open'
                FROM analysis.ticker_decision WHERE id = %s""", [old])
            inserted.set()
            assert commit.wait(10)

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(add_order)
        if not inserted.wait(5):
            writer.result(timeout=1)
            pytest.fail("concurrent ticker order was not inserted")
        archiver = executor.submit(TickerEvidenceArchive(storage).run, now=now, execute=True,
                                   backup_token=_verified_backup(storage))
        try:
            assert archiver.result(timeout=5)["archived"] == 0
        finally:
            commit.set()
        writer.result(timeout=5)
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT evidence_state FROM analysis.ticker_decision WHERE id = %s",
                                  [old]).fetchone()["evidence_state"] == "local"


def test_concurrent_ticker_outcome_update_keeps_source_local(storage, migrated_postgres_dsn):
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    old = _decision(storage.runtime, "ticker-outcome-old", Jsonb({"inputs": {"quote": {"raw": "old"}}}))
    _decision(storage.runtime, "ticker-outcome-new", Jsonb({"inputs": {"quote": {"raw": "new"}}}))
    _complete_ticker(storage.runtime, old)
    compact_input_batch(storage.runtime, execute=True)
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET as_of = %s, status = 'superseded' WHERE id = %s",
                           [now - timedelta(days=40), old])
    changed, commit = Event(), Event()

    def reopen_outcome():
        with psycopg.connect(migrated_postgres_dsn) as connection:
            connection.execute("""UPDATE analysis.ticker_outcome SET state = 'observing'
                WHERE ticker_decision_id = %s AND horizon = 'TACTICAL' AND horizon_sessions = 1""", [old])
            changed.set()
            assert commit.wait(10)

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(reopen_outcome)
        assert changed.wait(5)
        archiver = executor.submit(TickerEvidenceArchive(storage).run, now=now, execute=True,
                                   backup_token=_verified_backup(storage))
        try:
            assert archiver.result(timeout=5)["archived"] == 0
        finally:
            commit.set()
        writer.result(timeout=5)
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT evidence_state FROM analysis.ticker_decision WHERE id = %s",
                                  [old]).fetchone()["evidence_state"] == "local"


def test_ticker_compact_history_and_current_decision_work_without_nas(storage, monkeypatch):
    now = datetime.now(UTC) - timedelta(minutes=1)
    old_at = now - timedelta(days=40)
    with storage.runtime.transaction() as connection:
        reconcile_instrument(connection, "TCOLD")
    repository = TickerDecisionRepository(storage.runtime)
    old = build_ticker_decision("TCOLD", {"quotes": [{
        "symbol": "TCOLD", "price": 100, "observed_at": old_at - timedelta(minutes=1),
        "available_at": old_at - timedelta(minutes=1), "confirmed": True,
    }]}, as_of=old_at)
    old_id = repository.publish(old)["ticker_decision_id"]
    new = build_ticker_decision("TCOLD", {"quotes": [{
        "symbol": "TCOLD", "price": 101, "observed_at": now - timedelta(minutes=1),
        "available_at": now - timedelta(minutes=1), "confirmed": True,
    }]}, as_of=now)
    new_id = repository.publish(new)["ticker_decision_id"]
    assert new_id != old_id
    _complete_ticker(storage.runtime, old_id)
    with storage.runtime.transaction() as connection:
        connection.execute("""UPDATE analysis.ticker_data_request
            SET status = 'complete', completed_at = %s
            WHERE ticker_decision_id = %s AND status IN ('open', 'running')""", [now, old_id])
    archive = TickerEvidenceArchive(storage)
    result = archive.run(now=now, execute=True, backup_token=_verified_backup(storage))
    with storage.runtime.read() as connection:
        diagnostic = connection.execute("""SELECT status, as_of, evidence_state,
            (SELECT count(*) FROM analysis.ticker_outcome WHERE ticker_decision_id = decision.id AND state = 'observing') AS pending,
            (SELECT count(*) FROM analysis.ticker_data_request WHERE ticker_decision_id = decision.id AND status IN ('open', 'running')) AS requests
            FROM analysis.ticker_decision decision WHERE id = %s""", [old_id]).fetchone()
    assert result["archived"] == 1, diagnostic
    offline = storage.archive_root.with_name(storage.archive_root.name + "-offline")
    storage.archive_root.rename(offline)
    try:
        from fastapi.testclient import TestClient
        from investment_panel.api import dependencies
        from investment_panel.api.main import app

        monkeypatch.setitem(app.dependency_overrides, dependencies.get_ticker_decision_evidence,
                            lambda: TickerEvidenceArchive(storage).full_evidence)
        monkeypatch.setitem(app.dependency_overrides, dependencies.get_authorized_request,
                            lambda: None)
        assert repository.latest("TCOLD").decision_revision == new.decision_revision
        with storage.runtime.read() as connection:
            history = connection.execute("""SELECT decision_revision, resolution, input_manifest,
                   evidence_state, evidence_archive_manifest_id
                FROM analysis.ticker_decision_read WHERE id = %s""", [old_id]).fetchone()
        assert history["decision_revision"] == old.decision_revision
        assert history["evidence_state"] == "archived"
        assert history["evidence_archive_manifest_id"] == result["manifest_id"]
        assert history["resolution"]["action"] == old.resolution.action.value
        assert history["input_manifest"]["trade_plan"] is None
        assert archive.full_evidence(old_id) == {
            "status": "archived", "archive_manifest_id": result["manifest_id"],
        }
        assert TestClient(app).get(f"/api/ticker-decisions/{old_id}/evidence").json() == {
            "status": "archived", "archive_manifest_id": result["manifest_id"],
        }
    finally:
        offline.rename(storage.archive_root)


def test_completed_option_scan_archives_dependencies_and_restores_typed_rows(storage, migrated_postgres_dsn):
    now, decision_id = _completed_option_scan(storage)
    archive = OptionEvidenceArchive(storage)
    assert archive.run(now=now)["eligible"] == 1
    assert not storage.archive_root.exists()
    with pytest.raises(ValueError, match="backup"):
        archive.run(now=now, execute=True)
    with storage.runtime.read() as connection:
        before = connection.execute("SELECT to_jsonb(scan)::text AS row_json FROM analysis.option_decision scan WHERE decision_id = %s", [decision_id]).fetchone()["row_json"]
    token = _verified_backup(storage)
    backup_receipt = storage.archive_root.parent.parent / "postgres-backups" / "fixture.json"
    backup = json.loads(backup_receipt.read_text())
    backup_receipt.write_text(json.dumps({**backup, "created_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()}))
    with pytest.raises(ValueError, match="fresh"):
        archive.run(now=now, execute=True, backup_token=token)
    backup_receipt.write_text(json.dumps(backup))
    result = archive.run(now=now, execute=True, backup_token=token)
    assert result["archived"] == 1
    with pytest.raises(psycopg.errors.RaiseException, match="requires local evidence"):
        with storage.runtime.transaction() as connection:
            connection.execute("""
                INSERT INTO app.paper_order (decision_id, instrument_id, side, quantity, status)
                SELECT decision.id, decision.instrument_id, 'buy', 1, 'open'
                FROM analysis.decision decision WHERE decision.id = %s
            """, [decision_id])
    with pytest.raises(psycopg.errors.RaiseException, match="requires local evidence"):
        with storage.runtime.transaction() as connection:
            connection.execute("""
                INSERT INTO analysis.agent_task (decision_id, task_kind, status, request)
                VALUES (%s, 'review', 'running', '{}'::jsonb)
            """, [decision_id])
    with pytest.raises(psycopg.errors.RaiseException, match="requires local evidence"):
        with storage.runtime.transaction() as connection:
            connection.execute("INSERT INTO analysis.shadow_trade (decision_id, status) VALUES (%s, 'pending')",
                               [decision_id])
    with pytest.raises(psycopg.errors.RaiseException, match="requires local scan evidence"):
        with storage.runtime.transaction() as connection:
            connection.execute("""
                INSERT INTO app.publication (scope, analysis_run_id, status, published_at)
                SELECT 'options-radar', run_id, 'published', now()
                FROM analysis.decision WHERE id = %s
            """, [decision_id])
    with pytest.raises(psycopg.errors.RaiseException, match="requires local scan evidence"):
        with storage.runtime.transaction() as connection:
            connection.execute("""
                INSERT INTO app.publication (scope, analysis_run_id, status, published_at)
                SELECT 'options-decision-system', run_id, 'published', now()
                FROM analysis.decision WHERE id = %s
            """, [decision_id])
    with storage.runtime.transaction() as connection:
        publication_id = connection.execute("""
            INSERT INTO app.publication (scope, analysis_run_id, status, published_at)
            SELECT 'today', run_id, 'published', now()
            FROM analysis.decision WHERE id = %s RETURNING id
        """, [decision_id]).fetchone()["id"]
    with pytest.raises(psycopg.errors.RaiseException, match="requires local scan evidence"):
        with storage.runtime.transaction() as connection:
            connection.execute("UPDATE app.publication SET scope = 'options-decision-system' WHERE id = %s",
                               [publication_id])
    with pytest.raises(psycopg.errors.RaiseException, match="requires local primary scan evidence"):
        with storage.runtime.transaction() as connection:
            successor = connection.execute("""
                SELECT newer.id FROM analysis.decision older
                JOIN analysis.decision newer ON newer.instrument_id = older.instrument_id
                  AND newer.id <> older.id AND newer.as_of > older.as_of
                WHERE older.id = %s LIMIT 1
            """, [decision_id]).fetchone()["id"]
            connection.execute("UPDATE analysis.option_decision SET primary_decision_id = %s WHERE decision_id = %s",
                               [decision_id, successor])
    with storage.runtime.read() as connection:
        row = connection.execute("SELECT details, evidence_state, evidence_archive_manifest_id FROM analysis.option_decision WHERE decision_id = %s", [decision_id]).fetchone()
        assert row["details"]["evidence_state"] == "archived"
        assert row["details"]["archive_manifest_id"] == result["manifest_id"]
        exact = connection.execute(
            "SELECT details #>> '{historical_paths,lower_95_expected_value}' AS value "
            "FROM analysis.option_decision WHERE decision_id = %s", [decision_id],
        ).fetchone()["value"]
        assert exact == "0.12345678901234567890123456789"
        assert row["details"]["historical_paths"]["scenario_count"] == 17
        assert "paths" not in row["details"]["historical_paths"]
        assert row["details"]["calibration"] == {"sample_size": 17}
        assert row["details"]["quote_package"]["liquidity"] == {"spread": 0.1}
        assert row["details"]["thesis"] == {"invalidation": "price"}
        assert "raw" not in row["details"]
        assert row["evidence_state"] == "archived"
        assert row["evidence_archive_manifest_id"] == result["manifest_id"]
        evidence = connection.execute("SELECT detail FROM analysis.decision_evidence WHERE decision_id = %s", [decision_id]).fetchone()
        assert evidence["detail"]["evidence_state"] == "archived"
    with pytest.raises(psycopg.errors.RaiseException, match="archived option evidence is immutable"):
        with storage.runtime.transaction() as connection:
            connection.execute("UPDATE analysis.option_decision SET details = '{}'::jsonb WHERE decision_id = %s",
                               [decision_id])
    with pytest.raises(psycopg.errors.RaiseException, match="archived option evidence is immutable"):
        with storage.runtime.transaction() as connection:
            connection.execute("UPDATE analysis.decision_evidence SET detail = '{}'::jsonb WHERE decision_id = %s",
                               [decision_id])
    offline = storage.archive_root.with_name(storage.archive_root.name + "-offline")
    storage.archive_root.rename(offline)
    try:
        detail = AnalysisRepository(storage.runtime).option_signal_detail(decision_id)
        assert detail["evidence_state"] == "archived"
        assert detail["details"]["archive_manifest_id"] == result["manifest_id"]
    finally:
        offline.rename(storage.archive_root)
    with psycopg.connect(migrated_postgres_dsn) as connection:
        connection.execute("ALTER TABLE analysis.decision_evidence DISABLE TRIGGER archived_decision_evidence_immutable")
        connection.execute("ALTER TABLE analysis.option_decision DISABLE TRIGGER archived_option_decision_immutable")
        connection.execute("DELETE FROM analysis.decision_evidence WHERE decision_id = %s", [decision_id])
        connection.execute("DELETE FROM analysis.option_decision WHERE decision_id = %s", [decision_id])
        connection.execute("DELETE FROM analysis.decision WHERE id = %s", [decision_id])
        connection.execute("ALTER TABLE analysis.decision_evidence ENABLE TRIGGER archived_decision_evidence_immutable")
        connection.execute("ALTER TABLE analysis.option_decision ENABLE TRIGGER archived_option_decision_immutable")
    receipt = archive.restore_scan(decision_id, manifest_id=result["manifest_id"],
                                   destination_dsn=migrated_postgres_dsn)
    assert receipt["typed_rows"]["analysis.option_decision"] == 1
    with psycopg.connect(migrated_postgres_dsn, row_factory=psycopg.rows.dict_row) as connection:
        restored = connection.execute(f"SELECT to_jsonb(scan)::text AS row_json FROM {receipt['staging_schema']}.analysis_option_decision scan").fetchone()["row_json"]
        assert restored == before


def test_option_scan_archive_covers_every_synthetic_leg_quote(storage, migrated_postgres_dsn):
    now, decision_id = _completed_option_scan(storage, count=2)
    with storage.runtime.transaction() as connection:
        leg = connection.execute("""
            SELECT q.contract_id FROM raw.option_quote q
            JOIN analysis.option_decision scan ON scan.snapshot_id = q.snapshot_id
              AND scan.quote_observed_at = q.observed_at
            WHERE scan.decision_id = %s AND q.contract_id <> scan.contract_id
        """, [decision_id]).fetchone()["contract_id"]
        connection.execute(
            "UPDATE analysis.option_decision SET synthetic_legs = %s WHERE decision_id = %s",
            [Jsonb([{"contract_id": leg}]), decision_id],
        )
        scan = connection.execute("""
            SELECT decision.run_id, scan.snapshot_id, scan.quote_observed_at,
                   scan.contract_id FROM analysis.option_decision scan
            JOIN analysis.decision decision ON decision.id = scan.decision_id
            WHERE scan.decision_id = %s
        """, [decision_id]).fetchone()
        for contract_id in (scan["contract_id"], leg):
            connection.execute("""
                INSERT INTO analysis.option_feature
                  (run_id, snapshot_id, contract_id, quote_observed_at, feature_version)
                VALUES (%s, %s, %s, %s, 'archive-test')
            """, [scan["run_id"], scan["snapshot_id"], contract_id,
                  scan["quote_observed_at"]])
    archive = OptionEvidenceArchive(storage)
    result = archive.run(now=now, execute=True, backup_token=_verified_backup(storage))
    restored = archive.restore_scan(decision_id, destination_dsn=migrated_postgres_dsn)
    assert restored["typed_rows"]["raw.option_quote"] == 2
    assert restored["typed_rows"]["catalog.option_contract"] == 2
    assert restored["typed_rows"]["analysis.option_feature"] == 2


def test_hot_retention_pins_local_synthetic_leg_quote(storage):
    now, decision_id = _completed_option_scan(storage, count=2)
    with storage.runtime.transaction() as connection:
        leg = connection.execute("""
            SELECT q.snapshot_id, q.contract_id, q.observed_at FROM raw.option_quote q
            JOIN analysis.option_decision scan ON scan.snapshot_id = q.snapshot_id
              AND scan.quote_observed_at = q.observed_at
            WHERE scan.decision_id = %s AND q.contract_id <> scan.contract_id
        """, [decision_id]).fetchone()
        connection.execute("UPDATE analysis.option_decision SET synthetic_legs = %s WHERE decision_id = %s",
                           [Jsonb([{"contract_id": leg["contract_id"]}]), decision_id])
    HotRetention(storage).run(phase="options", now=now, execute=True, max_batches=4)
    with storage.runtime.read() as connection:
        quote = connection.execute("""
            SELECT provider_payload FROM raw.option_quote
            WHERE snapshot_id = %s AND contract_id = %s AND observed_at = %s
        """, [leg["snapshot_id"], leg["contract_id"], leg["observed_at"]]).fetchone()
        assert quote is not None and quote["provider_payload"]["precise"] == "unchanged"


def test_option_scan_with_primary_link_restores_parent_without_compacting_it(storage, migrated_postgres_dsn):
    now, decision_id = _completed_option_scan(storage)
    with storage.runtime.transaction() as connection:
        successor = connection.execute("""
            SELECT newer.id FROM analysis.decision older
            JOIN analysis.decision newer ON newer.instrument_id = older.instrument_id
              AND newer.id <> older.id WHERE older.id = %s LIMIT 1
        """, [decision_id]).fetchone()["id"]
        connection.execute("UPDATE analysis.option_decision SET primary_decision_id = %s WHERE decision_id = %s",
                           [successor, decision_id])
        original = connection.execute(
            "SELECT to_jsonb(scan)::text AS row_json FROM analysis.option_decision scan WHERE decision_id = %s",
            [successor],
        ).fetchone()["row_json"]
    result = OptionEvidenceArchive(storage).run(now=now, execute=True,
                                                backup_token=_verified_backup(storage))
    assert result["archived"] == 1
    receipt = OptionEvidenceArchive(storage).restore_scan(decision_id, destination_dsn=migrated_postgres_dsn)
    with psycopg.connect(migrated_postgres_dsn) as connection:
        restored = connection.execute(
            f"SELECT to_jsonb(scan)::text FROM {receipt['staging_schema']}.analysis_option_decision scan "
            "WHERE decision_id = %s", [successor],
        ).fetchone()[0]
    assert restored == original
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT evidence_state FROM analysis.option_decision WHERE decision_id = %s",
                                  [successor]).fetchone()["evidence_state"] == "local"


def test_completed_option_scan_with_thesis_archives_and_restores_exact_parent(storage, migrated_postgres_dsn):
    now, decision_id = _completed_option_scan(storage)
    with storage.runtime.transaction() as connection:
        instrument_id = connection.execute(
            "SELECT instrument_id FROM analysis.decision WHERE id = %s", [decision_id]
        ).fetchone()["instrument_id"]
        thesis_id = connection.execute("""
            INSERT INTO app.thesis (instrument_id, revision, status, thesis)
            VALUES (%s, 1, 'superseded', %s::jsonb) RETURNING id
        """, [instrument_id, '{"exact":0.12345678901234567890123456789,"reason":"old"}']).fetchone()["id"]
        connection.execute("UPDATE analysis.option_decision SET thesis_id = %s WHERE decision_id = %s",
                           [thesis_id, decision_id])
        original = connection.execute("SELECT to_jsonb(thesis)::text AS row_json FROM app.thesis thesis WHERE id = %s",
                                      [thesis_id]).fetchone()["row_json"]
    archived = OptionEvidenceArchive(storage).run(now=now, execute=True,
                                                  backup_token=_verified_backup(storage))
    assert archived["archived"] == 1
    receipt = OptionEvidenceArchive(storage).restore_scan(decision_id, destination_dsn=migrated_postgres_dsn)
    with psycopg.connect(migrated_postgres_dsn) as connection:
        restored = connection.execute(
            f"SELECT to_jsonb(thesis)::text FROM {receipt['staging_schema']}.app_thesis thesis WHERE id = %s",
            [thesis_id],
        ).fetchone()[0]
    assert restored == original


def test_option_scan_thesis_revision_lineage_restores_exact_parents(storage, migrated_postgres_dsn):
    now, decision_id = _completed_option_scan(storage)
    with storage.runtime.transaction() as connection:
        instrument_id = connection.execute(
            "SELECT instrument_id FROM analysis.decision WHERE id = %s", [decision_id]
        ).fetchone()["instrument_id"]
        parent_id = connection.execute("""
            INSERT INTO app.thesis (instrument_id, revision, status, thesis)
            VALUES (%s, 1, 'superseded', '{}'::jsonb) RETURNING id
        """, [instrument_id]).fetchone()["id"]
        child_id = connection.execute("""
            INSERT INTO app.thesis (instrument_id, revision, status, thesis, superseded_revision_id)
            VALUES (%s, 2, 'superseded', '{}'::jsonb, %s) RETURNING id
        """, [instrument_id, parent_id]).fetchone()["id"]
        connection.execute("UPDATE analysis.option_decision SET thesis_id = %s WHERE decision_id = %s",
                           [child_id, decision_id])
        original = connection.execute("SELECT to_jsonb(thesis)::text AS row_json FROM app.thesis thesis WHERE id = %s",
                                      [parent_id]).fetchone()["row_json"]
    result = OptionEvidenceArchive(storage).run(now=now, execute=True,
                                                backup_token=_verified_backup(storage))
    assert result["archived"] == 1
    receipt = OptionEvidenceArchive(storage).restore_scan(decision_id, destination_dsn=migrated_postgres_dsn)
    with psycopg.connect(migrated_postgres_dsn) as connection:
        restored = connection.execute(
            f"SELECT to_jsonb(thesis)::text FROM {receipt['staging_schema']}.app_thesis thesis WHERE id = %s",
            [parent_id],
        ).fetchone()[0]
    assert restored == original


def test_option_scan_thesis_automation_restores_exact_parent(storage, migrated_postgres_dsn):
    now, decision_id = _completed_option_scan(storage)
    with storage.runtime.transaction() as connection:
        instrument_id = connection.execute(
            "SELECT instrument_id FROM analysis.decision WHERE id = %s", [decision_id]
        ).fetchone()["instrument_id"]
        automation_id = connection.execute("""
            INSERT INTO app.thesis_automation_run
              (instrument_id, status, evidence_snapshot, cost_usd)
            VALUES (%s, 'succeeded', %s::jsonb, 0.123456) RETURNING id
        """, [instrument_id, '[{"exact":0.12345678901234567890123456789}]']).fetchone()["id"]
        thesis_id = connection.execute("""
            INSERT INTO app.thesis (instrument_id, revision, status, thesis, automation_run_id)
            VALUES (%s, 1, 'superseded', '{}'::jsonb, %s) RETURNING id
        """, [instrument_id, automation_id]).fetchone()["id"]
        connection.execute("UPDATE analysis.option_decision SET thesis_id = %s WHERE decision_id = %s",
                           [thesis_id, decision_id])
        original = connection.execute(
            "SELECT to_jsonb(run)::text AS row_json FROM app.thesis_automation_run run WHERE id = %s",
            [automation_id],
        ).fetchone()["row_json"]
    result = OptionEvidenceArchive(storage).run(now=now, execute=True,
                                                backup_token=_verified_backup(storage))
    assert result["archived"] == 1
    receipt = OptionEvidenceArchive(storage).restore_scan(decision_id, destination_dsn=migrated_postgres_dsn)
    with psycopg.connect(migrated_postgres_dsn) as connection:
        restored = connection.execute(
            f"SELECT to_jsonb(run)::text FROM {receipt['staging_schema']}.app_thesis_automation_run run WHERE id = %s",
            [automation_id],
        ).fetchone()[0]
    assert restored == original


def test_option_scan_thesis_agent_task_restores_run_and_task(storage, migrated_postgres_dsn):
    now, decision_id = _completed_option_scan(storage)
    with storage.runtime.transaction() as connection:
        instrument_id = connection.execute(
            "SELECT instrument_id FROM analysis.decision WHERE id = %s", [decision_id]
        ).fetchone()["instrument_id"]
        run_id = connection.execute("""
            INSERT INTO analysis.agent_run (provider, model, trigger, started_at, finished_at, status, cost_usd)
            VALUES ('fixture', 'fixture', 'test', %s, %s, 'succeeded', 0.123456) RETURNING id
        """, [now - timedelta(days=40), now - timedelta(days=40)]).fetchone()["id"]
        task_id = connection.execute("""
            INSERT INTO analysis.agent_task
              (agent_run_id, decision_id, task_kind, status, request, result)
            VALUES (%s, %s, 'option_thesis', 'completed', %s::jsonb, %s::jsonb) RETURNING id
        """, [run_id, decision_id, '{"exact":0.12345678901234567890123456789}', '{}']).fetchone()["id"]
        thesis_id = connection.execute("""
            INSERT INTO app.thesis (instrument_id, revision, status, thesis, source_agent_task_id)
            VALUES (%s, 1, 'superseded', '{}'::jsonb, %s) RETURNING id
        """, [instrument_id, task_id]).fetchone()["id"]
        connection.execute("UPDATE analysis.option_decision SET thesis_id = %s WHERE decision_id = %s",
                           [thesis_id, decision_id])
        original = connection.execute(
            "SELECT to_jsonb(task)::text AS row_json FROM analysis.agent_task task WHERE id = %s", [task_id]
        ).fetchone()["row_json"]
    result = OptionEvidenceArchive(storage).run(now=now, execute=True,
                                                backup_token=_verified_backup(storage))
    assert result["archived"] == 1
    receipt = OptionEvidenceArchive(storage).restore_scan(decision_id, destination_dsn=migrated_postgres_dsn)
    with psycopg.connect(migrated_postgres_dsn) as connection:
        restored = connection.execute(
            f"SELECT to_jsonb(task)::text FROM {receipt['staging_schema']}.analysis_agent_task task WHERE id = %s",
            [task_id],
        ).fetchone()[0]
        assert connection.execute(
            f"SELECT count(*) FROM {receipt['staging_schema']}.analysis_agent_run WHERE id = %s",
            [run_id],
        ).fetchone()[0] == 1
    assert restored == original


def test_option_scan_referenced_by_newer_local_scan_stays_local(storage):
    now, decision_id = _completed_option_scan(storage)
    with storage.runtime.transaction() as connection:
        successor = connection.execute("""
            SELECT newer.id FROM analysis.decision older
            JOIN analysis.decision newer ON newer.instrument_id = older.instrument_id
              AND newer.id <> older.id AND newer.as_of > older.as_of
            WHERE older.id = %s LIMIT 1
        """, [decision_id]).fetchone()["id"]
        connection.execute("UPDATE analysis.option_decision SET primary_decision_id = %s WHERE decision_id = %s",
                           [decision_id, successor])
    assert OptionEvidenceArchive(storage).run(now=now)["status"] == "pass_complete"
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT evidence_state FROM analysis.option_decision WHERE decision_id = %s",
                                  [decision_id]).fetchone()["evidence_state"] == "local"


def test_option_scan_missing_synthetic_leg_quote_prevents_compaction(storage):
    now, decision_id = _completed_option_scan(storage, count=2)
    with storage.runtime.transaction() as connection:
        leg = connection.execute("""
            SELECT q.snapshot_id, q.contract_id, q.observed_at FROM raw.option_quote q
            JOIN analysis.option_decision scan ON scan.snapshot_id = q.snapshot_id
              AND scan.quote_observed_at = q.observed_at
            WHERE scan.decision_id = %s AND q.contract_id <> scan.contract_id
        """, [decision_id]).fetchone()
        connection.execute(
            "UPDATE analysis.option_decision SET synthetic_legs = %s WHERE decision_id = %s",
            [Jsonb([{"contract_id": leg["contract_id"]}]), decision_id],
        )
        connection.execute(
            "DELETE FROM raw.option_quote WHERE snapshot_id = %s AND contract_id = %s AND observed_at = %s",
            [leg["snapshot_id"], leg["contract_id"], leg["observed_at"]],
        )
    skipped = OptionEvidenceArchive(storage).run(now=now, execute=True,
                                                  backup_token=_verified_backup(storage))
    assert skipped["archived"] == 0 and skipped["skipped"] == [str(decision_id)]
    with storage.runtime.read() as connection:
        assert connection.execute(
            "SELECT evidence_state FROM analysis.option_decision WHERE decision_id = %s", [decision_id]
        ).fetchone()["evidence_state"] == "local"


def test_option_scan_archive_restores_capture_source_parents(storage, migrated_postgres_dsn):
    now, decision_id = _completed_option_scan(storage)
    with storage.runtime.transaction() as connection:
        snapshot = connection.execute("""
            SELECT snapshot.id, snapshot.ingest_run_id FROM raw.option_snapshot snapshot
            JOIN analysis.option_decision scan ON scan.snapshot_id = snapshot.id
            WHERE scan.decision_id = %s
        """, [decision_id]).fetchone()
        payload_id = connection.execute("""
            INSERT INTO ingest.payload (run_id, archive_uri, sha256, encoding, byte_count)
            VALUES (%s, 'fixture://capture', %s, 'json', 1) RETURNING id
        """, [snapshot["ingest_run_id"], "a" * 64]).fetchone()["id"]
        generation_id = connection.execute("""
            INSERT INTO raw.option_capture_generation
              (snapshot_id, ingest_run_id, generation, capture_state)
            VALUES (%s, %s, 1, 'complete') RETURNING id
        """, [snapshot["id"], snapshot["ingest_run_id"]]).fetchone()["id"]
        connection.execute("""
            UPDATE raw.option_snapshot SET payload_id = %s,
              latest_complete_generation_id = %s WHERE id = %s
        """, [payload_id, generation_id, snapshot["id"]])
        connection.execute("""
            UPDATE raw.option_quote SET capture_generation_id = %s WHERE snapshot_id = %s
        """, [generation_id, snapshot["id"]])
    archive = OptionEvidenceArchive(storage)
    archive.run(now=now, execute=True, backup_token=_verified_backup(storage))
    receipt = archive.restore_scan(decision_id, destination_dsn=migrated_postgres_dsn)
    for relation in ("ingest.source", "ingest.run", "ingest.payload",
                     "raw.option_capture_generation"):
        assert receipt["typed_rows"][relation] == 1


def test_option_scan_archive_restores_strategy_revision_chain(storage, migrated_postgres_dsn):
    now, decision_id = _completed_option_scan(storage)
    with storage.runtime.transaction() as connection:
        hypothesis = connection.execute("""
            INSERT INTO analysis.hypothesis
              (hypothesis_key, statement, mechanism_class, falsification, input_hash)
            VALUES ('archive-test', 'test', 'test', 'test', %s) RETURNING id
        """, ["a" * 64]).fetchone()["id"]
        family = connection.execute("""
            INSERT INTO analysis.experiment_family
              (hypothesis_id, family_key, name, input_hash)
            VALUES (%s, 'archive-test', 'test', %s) RETURNING id
        """, [hypothesis, "b" * 64]).fetchone()["id"]
        old_revision = connection.execute("""
            INSERT INTO analysis.strategy_revision
              (strategy_key, revision, name, status, parameters, authority_group)
            VALUES ('archive-test', 1, 'test', 'candidate', '{}'::jsonb, 'archive-test')
            RETURNING id
        """).fetchone()["id"]
        revision = connection.execute("""
            INSERT INTO analysis.strategy_revision
              (strategy_key, revision, name, status, parameters, authority_group,
               supersedes_id, hypothesis_id, experiment_family_id)
            VALUES ('archive-test', 2, 'test', 'candidate', '{}'::jsonb, 'archive-test',
                    %s, %s, %s) RETURNING id
        """, [old_revision, hypothesis, family]).fetchone()["id"]
        connection.execute("""
            UPDATE analysis.run SET strategy_revision_id = %s
            WHERE id = (SELECT run_id FROM analysis.decision WHERE id = %s)
        """, [revision, decision_id])
        connection.execute("UPDATE analysis.decision SET strategy_revision_id = %s WHERE id = %s",
                           [revision, decision_id])
    archive = OptionEvidenceArchive(storage)
    archive.run(now=now, execute=True, backup_token=_verified_backup(storage))
    receipt = archive.restore_scan(decision_id, destination_dsn=migrated_postgres_dsn)
    assert receipt["typed_rows"]["analysis.strategy_revision"] == 2
    assert receipt["typed_rows"]["analysis.hypothesis"] == 1
    assert receipt["typed_rows"]["analysis.experiment_family"] == 1


def test_option_feature_missing_source_quote_prevents_compaction(storage):
    now, decision_id = _completed_option_scan(storage)
    with storage.runtime.transaction() as connection:
        scan = connection.execute("""
            SELECT decision.run_id, scan.snapshot_id, scan.contract_id,
                   scan.quote_observed_at FROM analysis.option_decision scan
            JOIN analysis.decision decision ON decision.id = scan.decision_id
            WHERE scan.decision_id = %s
        """, [decision_id]).fetchone()
        connection.execute("""
            INSERT INTO analysis.option_feature
              (run_id, snapshot_id, contract_id, quote_observed_at, feature_version)
            VALUES (%s, %s, %s, %s, 'archive-test')
        """, [scan["run_id"], scan["snapshot_id"], scan["contract_id"],
              scan["quote_observed_at"] + timedelta(minutes=1)])
    skipped = OptionEvidenceArchive(storage).run(now=now, execute=True,
                                                  backup_token=_verified_backup(storage))
    assert skipped["archived"] == 0 and skipped["skipped"] == [str(decision_id)]
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT evidence_state FROM analysis.option_decision WHERE decision_id = %s",
                                  [decision_id]).fetchone()["evidence_state"] == "local"


def test_incomplete_option_scan_does_not_block_later_completed_scan(storage):
    now, broken_id = _completed_option_scan(storage, count=2)
    with storage.runtime.transaction() as connection:
        broken = connection.execute("""
            SELECT decision.run_id, decision.instrument_id, scan.snapshot_id,
                   scan.contract_id, scan.quote_observed_at
            FROM analysis.decision decision
            JOIN analysis.option_decision scan ON scan.decision_id = decision.id
            WHERE decision.id = %s
        """, [broken_id]).fetchone()
        other = connection.execute("""
            SELECT contract_id FROM raw.option_quote
            WHERE snapshot_id = %s AND observed_at = %s AND contract_id <> %s
        """, [broken["snapshot_id"], broken["quote_observed_at"],
              broken["contract_id"]]).fetchone()["contract_id"]
        healthy_id = connection.execute("""
            INSERT INTO analysis.decision
              (run_id, decision_key, kind, instrument_id, as_of, state, input_hash)
            VALUES (%s, 'old-option-second', 'option', %s, %s, 'REJECT', %s) RETURNING id
        """, [broken["run_id"], broken["instrument_id"], broken["quote_observed_at"],
              "e" * 64]).fetchone()["id"]
        connection.execute("""
            INSERT INTO analysis.option_decision
              (decision_id, contract_id, snapshot_id, quote_observed_at, paper_state, details)
            VALUES (%s, %s, %s, %s, 'REJECT', '{}'::jsonb)
        """, [healthy_id, other, broken["snapshot_id"], broken["quote_observed_at"]])
        another_id = connection.execute("""
            INSERT INTO analysis.decision
              (run_id, decision_key, kind, instrument_id, as_of, state, input_hash)
            VALUES (%s, 'old-option-third', 'option', %s, %s, 'REJECT', %s) RETURNING id
        """, [broken["run_id"], broken["instrument_id"], broken["quote_observed_at"],
              "f" * 64]).fetchone()["id"]
        connection.execute("""
            INSERT INTO analysis.option_decision
              (decision_id, contract_id, snapshot_id, quote_observed_at, paper_state, details)
            VALUES (%s, %s, %s, %s, 'REJECT', '{}'::jsonb)
        """, [another_id, other, broken["snapshot_id"], broken["quote_observed_at"]])
        ingest_run_id = connection.execute(
            "SELECT ingest_run_id FROM raw.option_snapshot WHERE id = %s",
            [broken["snapshot_id"]],
        ).fetchone()["ingest_run_id"]
        generation = connection.execute("""
            INSERT INTO raw.option_capture_generation
              (snapshot_id, ingest_run_id, generation, capture_state)
            VALUES (%s, %s, 1, 'complete') RETURNING id
        """, [broken["snapshot_id"], ingest_run_id]).fetchone()["id"]
        relative_run = connection.execute("""
            INSERT INTO analysis.run
              (run_type, input_cutoff, code_version, input_hash, started_at, status)
            VALUES ('option-relative-archive-fixture', %s, 'fixture', %s, %s, 'succeeded')
            RETURNING id
        """, [broken["quote_observed_at"], "1" * 64,
              broken["quote_observed_at"]]).fetchone()["id"]
        relative = connection.execute("""
            INSERT INTO analysis.option_relative_value
              (analysis_run_id, capture_generation_id, contract_id,
               model_revision, classification, quality_status)
            VALUES (%s, %s, %s, 'fixture', 'rejected', 'available') RETURNING id
        """, [relative_run, generation, other]).fetchone()["id"]
        connection.execute("UPDATE analysis.option_decision SET relative_value_id = %s WHERE decision_id = %s",
                           [relative, healthy_id])
        connection.execute("UPDATE analysis.decision SET as_of = as_of - interval '1 second' WHERE id = %s",
                           [broken_id])
        connection.execute("""
            DELETE FROM raw.option_quote WHERE snapshot_id = %s AND contract_id = %s
              AND observed_at = %s
        """, [broken["snapshot_id"], broken["contract_id"], broken["quote_observed_at"]])
    result = OptionEvidenceArchive(storage).run(
        now=now, batch_size=3, execute=True, backup_token=_verified_backup(storage))
    assert result["archived"] == 2
    assert result["skipped"] == [str(broken_id)]
    with storage.runtime.read() as connection:
        states = {str(row["decision_id"]): row["evidence_state"] for row in connection.execute(
            "SELECT decision_id, evidence_state FROM analysis.option_decision WHERE decision_id = ANY(%s)",
            [[broken_id, healthy_id, another_id]],
        ).fetchall()}
    assert states == {str(broken_id): "local", str(healthy_id): "archived",
                      str(another_id): "archived"}


def test_option_archive_restores_relative_value_verification(storage, migrated_postgres_dsn):
    now, decision_id = _completed_option_scan(storage)
    with storage.runtime.transaction() as connection:
        scan = connection.execute("""
            SELECT decision.run_id, scan.snapshot_id, scan.contract_id,
                   snapshot.ingest_run_id FROM analysis.option_decision scan
            JOIN analysis.decision decision ON decision.id = scan.decision_id
            JOIN raw.option_snapshot snapshot ON snapshot.id = scan.snapshot_id
            WHERE scan.decision_id = %s
        """, [decision_id]).fetchone()
        generation = connection.execute("""
            INSERT INTO raw.option_capture_generation
              (snapshot_id, ingest_run_id, generation, capture_state)
            VALUES (%s, %s, 1, 'complete') RETURNING id
        """, [scan["snapshot_id"], scan["ingest_run_id"]]).fetchone()["id"]
        relative = connection.execute("""
            INSERT INTO analysis.option_relative_value
              (analysis_run_id, capture_generation_id, contract_id,
               model_revision, classification, quality_status)
            VALUES (%s, %s, %s, 'archive-test', 'rejected', 'available') RETURNING id
        """, [scan["run_id"], generation, scan["contract_id"]]).fetchone()["id"]
        connection.execute("""
            INSERT INTO analysis.option_relative_value_verification
              (relative_value_id, status, blockers, evidence)
            VALUES (%s, 'rejected', ARRAY['fixture'], '{"exact":0.12345678901234567890123456789}'::jsonb)
        """, [relative])
        connection.execute("UPDATE analysis.option_decision SET relative_value_id = %s WHERE decision_id = %s",
                           [relative, decision_id])
    archive = OptionEvidenceArchive(storage)
    archive.run(now=now, execute=True, backup_token=_verified_backup(storage))
    receipt = archive.restore_scan(decision_id, destination_dsn=migrated_postgres_dsn)
    assert receipt["typed_rows"]["analysis.option_relative_value_verification"] == 1


def test_option_scan_missing_quote_prevents_compaction(storage):
    now, decision_id = _completed_option_scan(storage)
    with storage.runtime.transaction() as connection:
        connection.execute("DELETE FROM raw.option_quote WHERE observed_at < %s", [now - timedelta(days=30)])
    skipped = OptionEvidenceArchive(storage).run(now=now, execute=True,
                                                  backup_token=_verified_backup(storage))
    assert skipped["archived"] == 0 and skipped["skipped"] == [str(decision_id)]
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT evidence_state FROM analysis.option_decision WHERE decision_id = %s", [decision_id]).fetchone()["evidence_state"] == "local"


def test_option_scan_needs_completed_successor_for_same_instrument(storage):
    now, decision_id = _completed_option_scan(storage)
    with storage.runtime.transaction() as connection:
        other_id = connection.execute("""
            INSERT INTO catalog.instrument (symbol, name, asset_class)
            VALUES ('OTHERSCAN', 'Other scan', 'equity') RETURNING id
        """).fetchone()["id"]
        connection.execute("UPDATE analysis.decision SET instrument_id = %s WHERE decision_key = 'new-option'",
                           [other_id])
    result = OptionEvidenceArchive(storage).run(now=now, execute=True,
                                                 backup_token=_verified_backup(storage))
    assert result["archived"] == 0
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT evidence_state FROM analysis.option_decision WHERE decision_id = %s",
                                  [decision_id]).fetchone()["evidence_state"] == "local"


def test_active_shadow_reference_keeps_option_scan_local(storage):
    now, decision_id = _completed_option_scan(storage)
    with storage.runtime.transaction() as connection:
        connection.execute("INSERT INTO analysis.shadow_trade (decision_id, status) VALUES (%s, 'pending')",
                           [decision_id])
    result = OptionEvidenceArchive(storage).run(now=now, execute=True,
                                                 backup_token=_verified_backup(storage))
    assert result["archived"] == 0
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT evidence_state FROM analysis.option_decision WHERE decision_id = %s",
                                  [decision_id]).fetchone()["evidence_state"] == "local"


def test_active_paper_reference_keeps_old_option_scan_local(storage):
    now, decision_id = _completed_option_scan(storage)
    with storage.runtime.transaction() as connection:
        connection.execute("""
            INSERT INTO app.paper_order (decision_id, instrument_id, side, quantity, status)
            SELECT decision.id, decision.instrument_id, 'buy', 1, 'open'
            FROM analysis.decision decision WHERE decision.id = %s
        """, [decision_id])
    result = OptionEvidenceArchive(storage).run(now=now, execute=True,
                                                 backup_token=_verified_backup(storage))
    assert result["archived"] == 0
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT evidence_state FROM analysis.option_decision WHERE decision_id = %s", [decision_id]).fetchone()["evidence_state"] == "local"


def test_option_archive_interruption_keeps_source(storage, monkeypatch):
    now, decision_id = _completed_option_scan(storage)
    archive = OptionEvidenceArchive(storage)
    monkeypatch.setattr(archive, "_checkpoint", Mock(side_effect=RuntimeError("commit interrupted")))
    with pytest.raises(RuntimeError, match="commit interrupted"):
        archive.run(now=now, execute=True, backup_token=_verified_backup(storage))
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT evidence_state FROM analysis.option_decision WHERE decision_id = %s", [decision_id]).fetchone()["evidence_state"] == "local"


def test_option_archive_corrupt_dependency_keeps_source(storage, monkeypatch):
    now, decision_id = _completed_option_scan(storage)
    archive = OptionEvidenceArchive(storage)
    write = archive._write_dependencies

    def corrupt(connection, rows):
        packs = write(connection, rows)
        manifest = connection.execute("SELECT nas_uri FROM ops.storage_archive_manifest WHERE id = %s",
                                      [packs["analysis.option_decision"][0]]).fetchone()
        Path(manifest["nas_uri"]).write_bytes(b"corrupt")
        return packs

    monkeypatch.setattr(archive, "_write_dependencies", corrupt)
    with pytest.raises(ValueError, match="corrupt"):
        archive.run(now=now, execute=True, backup_token=_verified_backup(storage))
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT evidence_state FROM analysis.option_decision WHERE decision_id = %s", [decision_id]).fetchone()["evidence_state"] == "local"


def test_concurrent_option_paper_reference_prevents_compaction(storage):
    now, decision_id = _completed_option_scan(storage)
    token = _verified_backup(storage)
    inserted, commit = Event(), Event()

    def add_order():
        with storage.runtime.transaction() as connection:
            connection.execute("""
                INSERT INTO app.paper_order (decision_id, instrument_id, side, quantity, status)
                SELECT decision.id, decision.instrument_id, 'buy', 1, 'open'
                FROM analysis.decision decision WHERE decision.id = %s
            """, [decision_id])
            inserted.set()
            assert commit.wait(10)

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(add_order)
        assert inserted.wait(5)
        archiver = executor.submit(OptionEvidenceArchive(storage).run,
                                   now=now, execute=True, backup_token=token)
        try:
            assert archiver.result(timeout=5)["archived"] == 0
        finally:
            commit.set()
        writer.result(timeout=5)
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT evidence_state FROM analysis.option_decision WHERE decision_id = %s", [decision_id]).fetchone()["evidence_state"] == "local"


def test_large_option_detail_restores_from_typed_copy(storage, migrated_postgres_dsn):
    now, decision_id = _completed_option_scan(storage)
    with storage.runtime.transaction() as connection:
        raw = '{"raw":"' + "中" * (3 * 1024**2) + '","exact":0.12345678901234567890123456789}'
        connection.execute("UPDATE analysis.option_decision SET details = %s::jsonb WHERE decision_id = %s",
                           [raw, decision_id])
        before = connection.execute("SELECT to_jsonb(scan)::text AS row_json FROM analysis.option_decision scan WHERE decision_id = %s",
                                    [decision_id]).fetchone()["row_json"]
    result = OptionEvidenceArchive(storage).run(now=now, execute=True,
                                                 backup_token=_verified_backup(storage))
    with storage.runtime.read() as connection:
        pack_id = connection.execute("SELECT (metadata->'dependency_manifests'->'analysis.option_decision'->>0)::bigint AS id FROM ops.storage_archive_manifest WHERE id = %s",
                                     [result["manifest_id"]]).fetchone()["id"]
        assert connection.execute("SELECT format FROM ops.storage_archive_manifest WHERE id = %s", [pack_id]).fetchone()["format"] == "postgres-copy-text-gzip.v1"
    receipt = OptionEvidenceArchive(storage).restore_scan(
        decision_id, destination_dsn=migrated_postgres_dsn)
    with psycopg.connect(migrated_postgres_dsn, row_factory=psycopg.rows.dict_row) as connection:
        restored = connection.execute(f"SELECT to_jsonb(scan)::text AS row_json FROM {receipt['staging_schema']}.analysis_option_decision scan").fetchone()["row_json"]
        assert restored == before


def test_payload_retention_does_not_rebuild_paper_option_marks(storage):
    now = _quotes(storage, old_days=90, profile="history_full")
    with storage.runtime.read() as connection:
        before = connection.execute("""
            SELECT max(mark.projected_at) AS projected_at
            FROM analysis.paper_option_mark_projection mark
            WHERE mark.contract_id IN (
                SELECT quote.contract_id FROM raw.option_quote quote
                JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
                WHERE snapshot.source_id = 'hot-test'
            )
        """).fetchone()["projected_at"]
    assert before is not None

    result = HotRetention(storage).run(
        phase="options", now=now, batch_size=500, max_batches=5, execute=True,
    )

    assert result["option_provider_payloads"] == 3
    with storage.runtime.read() as connection:
        after = connection.execute("""
            SELECT max(mark.projected_at) AS projected_at
            FROM analysis.paper_option_mark_projection mark
            WHERE mark.contract_id IN (
                SELECT quote.contract_id FROM raw.option_quote quote
                JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
                WHERE snapshot.source_id = 'hot-test'
            )
        """).fetchone()["projected_at"]
        assert connection.execute(
            "SELECT count(*) FROM raw.option_quote WHERE provider_payload <> '{}'::jsonb"
        ).fetchone()["count"] == 3
    assert after == before
    with storage.runtime.transaction() as connection:
        quote = connection.execute("""
            SELECT quote.snapshot_id, quote.contract_id, mark.projected_at, mark.mid
            FROM raw.option_quote quote
            JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
            JOIN analysis.paper_option_mark_projection mark ON mark.contract_id = quote.contract_id
            WHERE snapshot.source_id = 'hot-test'
            ORDER BY quote.observed_at DESC
            LIMIT 1
        """).fetchone()
        connection.execute(
            "UPDATE raw.option_quote SET mid = mid + 0.1 WHERE snapshot_id = %s AND contract_id = %s",
            [quote["snapshot_id"], quote["contract_id"]],
        )
        refreshed = connection.execute(
            "SELECT projected_at, mid FROM analysis.paper_option_mark_projection WHERE contract_id = %s",
            [quote["contract_id"]],
        ).fetchone()
        assert refreshed["projected_at"] > quote["projected_at"]
        assert refreshed["mid"] == quote["mid"] + 0.1


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
    Path(path).write_bytes(b"corrupt")
    with pytest.raises(ValueError), storage.runtime.transaction() as connection:
        RowArchive(storage).write(connection, "analysis.ticker_decision", records)


def test_oversized_pack_and_invalid_batch_fail_before_mutation(storage):
    with storage.runtime.transaction() as connection:
        with pytest.raises(ValueError, match="budget"):
            RowArchive(storage).write(connection, "analysis.ticker_decision", [{"row_json": "x" * (MAX_CHUNK_BYTES + 1)}])
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
    assert job_definition("postgres_retention").timeout_seconds == 600
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
    assert health["accounting"]["volume_free_bytes"] == 100 * 1024**3
    assert health["projected_free_space_bytes"] is not None


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
    unarchived_run = analysis.start_run("storage-role-test", input_cutoff=old,
                code_version="test", inputs={"revision": "unarchived"})
    analysis.finish_run(unarchived_run, "succeeded")
    analysis.publish(runs[0], "today", {"daily_brief": [{"stable_key": "brief", "headline": "old"}]})
    analysis.publish(runs[1], "today", {"daily_brief": [{"stable_key": "brief", "headline": "current"}]})
    jobs = JobRepository(storage.runtime)
    job = jobs.start("old-storage-role-test")
    jobs.finish(job["id"], "succeeded")
    with storage.runtime.transaction() as connection:
        assert connection.execute("SELECT current_user AS role").fetchone()["role"] == "market_app"
        connection.execute("UPDATE analysis.run SET started_at = %s WHERE id = ANY(%s)", [old, runs + [unarchived_run]])
        connection.execute("UPDATE app.publication SET created_at = %s, published_at = %s WHERE status = 'superseded'", [old, old])
        connection.execute("UPDATE ops.job_run SET started_at = %s, finished_at = %s WHERE id = %s", [old, old, job["id"]])
        assert not connection.execute("SELECT has_table_privilege('market_app', 'analysis.run', 'DELETE') AS allowed").fetchone()["allowed"]
        with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
            connection.execute("DELETE FROM analysis.run WHERE false")
    with storage.runtime.transaction() as connection:
        with pytest.raises(psycopg.Error, match="verified archive"), connection.transaction():
            connection.execute(
                "SELECT analysis.prune_empty_run_metadata(%s::uuid[], %s)", [[unarchived_run], old],
            )
        with pytest.raises(psycopg.Error, match="30 days"), connection.transaction():
            connection.execute(
                "SELECT analysis.prune_empty_run_metadata(%s::uuid[], %s)",
                [[recent_run], now - timedelta(days=1)],
            )
    counts = RetentionRepository(storage.runtime, archive_root=storage.archive_root).prune(now=now)
    assert counts["publications"] == 1
    assert counts["analysis_runs"] == 3
    assert counts["job_runs"] == 1
    with storage.runtime.transaction() as connection:
        retained = {row["id"] for row in connection.execute("SELECT id FROM analysis.run").fetchall()}
        assert retained == {runs[1], recent_run}
        assert connection.execute("SELECT count(*) AS n FROM analysis.run WHERE id = %s", [recent_run]).fetchone()["n"] == 1
        with pytest.raises(psycopg.Error, match="bounded"), connection.transaction():
            connection.execute("SELECT analysis.prune_empty_run_metadata(%s::uuid[], %s)", [[runs[1]] * 101, old])
    assert storage.verify()["failed"] == 0


def test_run_metadata_retention_rejects_a_window_below_its_database_guard(storage):
    from investment_panel.infrastructure.postgres.retention import RetentionRepository

    with pytest.raises(ValueError, match="30 days"):
        RetentionRepository(storage.runtime, archive_root=storage.archive_root).prune(
            now=datetime.now(UTC), analysis_days=29, dry_run=True,
        )


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


def test_oversized_row_uses_verified_typed_copy_and_retries_idempotently(storage, tmp_path):
    # UTF-8, embedded COPY delimiters and precision must survive without a
    # Python numeric parse or a higher budget for ordinary JSON row packs.
    raw = '{"blob":"' + ('中' * (3 * 1024**2)) + '\\n\\t\\\\","exact":0.12345678901234567890123456789}'
    decision_id = _decision(storage.runtime, "oversized", raw)
    with storage.runtime.transaction() as connection:
        records = connection.execute("SELECT to_jsonb(d)::text AS row_json FROM analysis.ticker_decision d WHERE id = %s", [decision_id]).fetchall()
        assert len(records[0]["row_json"].encode()) > MAX_PACK_BYTES
        ids = RowArchive(storage).write(connection, "analysis.ticker_decision", records)
    assert len(ids) == 1
    assert storage.verify(manifest_id=ids[0])["verified"] == 1
    with storage.runtime.read() as connection:
        manifest = connection.execute("SELECT * FROM ops.storage_archive_manifest WHERE id = %s", [ids[0]]).fetchone()
    assert manifest["format"] == "postgres-copy-text-gzip.v1"
    assert manifest["metadata"]["archive_contract"] == "postgres-row-json-copy.v1"
    assert manifest["metadata"]["typed_restore_verified"] is True
    with storage.runtime.transaction() as connection:
        connection.execute("CREATE TEMP TABLE recovered_row (relation text, source_columns jsonb, source_database jsonb, row_json text)")
        with connection.cursor().copy("COPY recovered_row FROM STDIN WITH (FORMAT text)") as copy:
            with gzip.open(manifest["nas_uri"], "rb") as source:
                while chunk := source.read(1024**2):
                    copy.write(chunk)
        assert connection.execute("SELECT row_json::jsonb = %s::jsonb AS same FROM recovered_row", [records[0]["row_json"]]).fetchone()["same"]
        assert connection.execute("SELECT to_jsonb(jsonb_populate_record(NULL::analysis.ticker_decision, row_json::jsonb)) = row_json::jsonb AS same FROM recovered_row").fetchone()["same"]
        assert RowArchive(storage).write(connection, "analysis.ticker_decision", records) == ids
    # Read-back verification must reject corruption, not replace it and declare
    # success. The caller's source transaction is still safe to roll back.
    with open(manifest["nas_uri"], "wb") as target:
        target.write(b"corrupt")
    with pytest.raises(ValueError), storage.runtime.transaction() as connection:
        RowArchive(storage).write(connection, "analysis.ticker_decision", records)
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) AS n FROM analysis.ticker_decision WHERE id = %s", [decision_id]).fetchone()["n"] == 1


@pytest.mark.parametrize("failure", [None, "nas_unavailable", "typed_restore"])
def test_orphaned_large_publication_payload_is_deleted_only_after_verifiable_archive(storage, monkeypatch, failure):
    from investment_panel.infrastructure.postgres.retention import RetentionRepository
    from investment_panel.infrastructure.postgres import row_copy_archive

    raw = '{"data":"' + 'x' * (9 * 1024**2) + '","exact":0.12345678901234567890123456789}'
    with storage.runtime.transaction() as connection:
        connection.execute("INSERT INTO app.publication_payload (content_hash, payload) VALUES (%s, %s::jsonb)", ["c" * 64, raw])
    retention = RetentionRepository(storage.runtime, archive_root=storage.archive_root)
    if failure:
        def fail(*args, **kwargs):
            raise ValueError(failure)
        monkeypatch.setattr(row_copy_archive, "ensure_mounted_archive_root" if failure == "nas_unavailable" else "_typed_restore", fail)
        with pytest.raises(ValueError, match=failure):
            retention.prune_publications()
    else:
        assert retention.prune_publications()["publication_payloads"] == 1
        assert storage.verify()["failed"] == 0
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) AS n FROM app.publication_payload WHERE content_hash = %s", ["c" * 64]).fetchone()["n"] == (1 if failure else 0)
        manifests = connection.execute("SELECT * FROM ops.storage_archive_manifest WHERE source_relation = 'app.publication_payload'").fetchall()
    assert len(manifests) == (0 if failure else 1)
    if not failure:
        with storage.runtime.transaction() as connection:
            connection.execute("CREATE TEMP TABLE original_payload (relation text, source_columns jsonb, source_database jsonb, row_json text)")
            with connection.cursor().copy("COPY original_payload FROM STDIN WITH (FORMAT text)") as copy, gzip.open(manifests[0]["nas_uri"], "rb") as handle:
                for chunk in iter(lambda: handle.read(1024**2), b""):
                    copy.write(chunk)
            connection.execute("INSERT INTO app.publication_payload SELECT restored.* FROM original_payload, LATERAL jsonb_populate_record(NULL::app.publication_payload, row_json::jsonb) restored")
            assert connection.execute("SELECT payload = %s::jsonb AS same FROM app.publication_payload WHERE content_hash = %s", [raw, "c" * 64]).fetchone()["same"]
