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

from investment_panel.infrastructure.postgres import decision_storage
from investment_panel.infrastructure.postgres.decision_storage import (
    backfill_ranking_publication_refs, compact_context_batch, compact_decision_evidence_batch, context_digest,
    require_maintenance_headroom, store_context,
)
from investment_panel.infrastructure.postgres.instruments import reconcile_instrument
from investment_panel.infrastructure.postgres.manifest_archive import (
    CHECKPOINT, FORMAT, KIND, MAX_CHUNK_BYTES, ManifestArchive, verify_copy_file,
)
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.infrastructure.postgres.storage_archive import StorageArchiveService
from investment_panel.infrastructure.postgres.retention import RetentionRepository
from investment_panel.infrastructure.postgres.analysis import AnalysisRepository
from investment_panel.domain.decision import build_ticker_decision
from investment_panel.infrastructure.postgres.ticker_decisions import TickerDecisionRepository


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


@pytest.mark.parametrize("location", ["trade_plan", "opportunity_rank"])
def test_ranking_publication_reference_backfill_preserves_exact_legacy_read(storage, location):
    decision_id = _decision(storage.runtime)
    with storage.runtime.transaction() as connection:
        run_id = connection.execute("""INSERT INTO analysis.run
            (run_type, input_cutoff, code_version, input_hash, started_at, finished_at, status)
            VALUES ('ranking-ref-test', now(), 'test', %s, now(), now(), 'succeeded') RETURNING id""",
            ["f" * 64]).fetchone()["id"]
        publication_id = connection.execute("""INSERT INTO app.publication
            (scope, analysis_run_id, status) VALUES ('ticker-opportunity-ranking', %s, 'superseded')
            RETURNING id""", [run_id]).fetchone()["id"]
        payload = ({"publication_id": str(publication_id), "action": "HOLD"}
                   if location == "trade_plan" else {"ranking_publication_id": str(publication_id)})
        connection.execute("""UPDATE analysis.ticker_decision SET
            input_manifest = jsonb_set(input_manifest, %s::text[], %s::jsonb)
            WHERE id = %s""", [[location], Jsonb(payload), decision_id])
    with storage.runtime.read() as connection:
        before = connection.execute("SELECT input_manifest::text FROM analysis.ticker_decision_read WHERE id = %s",
                                    [decision_id]).fetchone()
    assert backfill_ranking_publication_refs(storage.runtime, batch_size=1, execute=True)["checked"] == 1
    with storage.runtime.read() as connection:
        after = connection.execute("SELECT input_manifest::text FROM analysis.ticker_decision_read WHERE id = %s",
                                   [decision_id]).fetchone()
        ref = connection.execute("SELECT ranking_publication_id, ranking_ref_checked FROM analysis.ticker_decision WHERE id = %s",
                                 [decision_id]).fetchone()
    assert after == before
    assert ref == {"ranking_publication_id": publication_id, "ranking_ref_checked": True}
    assert backfill_ranking_publication_refs(storage.runtime, batch_size=1, execute=True)["checked"] == 0


def test_ranking_backfill_rejects_conflicting_plan_and_rank_refs(storage):
    decision_id = _decision(storage.runtime)
    with storage.runtime.transaction() as connection:
        connection.execute("""UPDATE analysis.ticker_decision SET input_manifest = input_manifest || %s::jsonb
            WHERE id = %s""", [Jsonb({"trade_plan": {"publication_id": "00000000-0000-4000-8000-000000000001"},
                                     "opportunity_rank": {"ranking_publication_id": "00000000-0000-4000-8000-000000000002"}}),
                              decision_id])
    with pytest.raises(ValueError, match="conflict"):
        backfill_ranking_publication_refs(storage.runtime, execute=True)
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT ranking_ref_checked FROM analysis.ticker_decision WHERE id = %s",
                                  [decision_id]).fetchone()["ranking_ref_checked"] is False


def test_decision_evidence_normalization_keeps_exact_reads_and_one_owner(storage):
    exact = '{"impact_id":"one","price":0.12345678901234567890123456789}'
    episode = {"expressions": {"STOCK": {"kind": "STOCK"}},
               "selected_expression": {"kind": "STOCK"}}
    raw = ('{"trade_plan":{"trade_plan_id":"plan","action":"HOLD","portfolio_impact":' + exact
           + ',"selected_expression":{"kind":"STOCK"}},'
           + '"instrument_state_snapshot":{"revision":"r1","exact":' + exact + '}}')
    decision_id = _decision(storage.runtime)
    with storage.runtime.transaction() as connection:
        connection.execute("""UPDATE analysis.ticker_decision
            SET input_manifest = %s::jsonb,
                resolution = %s::jsonb,
                capital_action = %s::jsonb,
                expressions = %s::jsonb,
                selected_expression = %s::jsonb,
                opportunity_episode = %s::jsonb,
                portfolio_impacts = %s::jsonb
            WHERE id = %s""", [raw, '{"action":"HOLD","portfolio_context":' + exact + '}',
                                  '{"action":"HOLD"}',
                                  json.dumps(episode["expressions"]),
                                  json.dumps(episode["selected_expression"]),
                                  json.dumps(episode), '{"STOCK":' + exact + '}', decision_id])
    with storage.runtime.read() as connection:
        before = connection.execute("""SELECT input_manifest, resolution, capital_action, expressions,
            selected_expression, opportunity_episode, portfolio_impacts
            FROM analysis.ticker_decision_read WHERE id = %s""", [decision_id]).fetchone()
    assert compact_decision_evidence_batch(storage.runtime, execute=True)["compacted"] == 1
    with storage.runtime.read() as connection:
        after = connection.execute("""SELECT input_manifest, resolution, capital_action, expressions,
            selected_expression, opportunity_episode, portfolio_impacts
            FROM analysis.ticker_decision_read WHERE id = %s""", [decision_id]).fetchone()
        base = connection.execute("""SELECT input_manifest, resolution, capital_action, expressions,
            selected_expression, portfolio_impacts, evidence_refs
            FROM analysis.ticker_decision WHERE id = %s""", [decision_id]).fetchone()
        assert after == before
        assert connection.execute("""SELECT input_manifest->'trade_plan'->'portfolio_impact'->>'price' AS price
            FROM analysis.ticker_decision_read WHERE id = %s""", [decision_id]).fetchone()["price"] == "0.12345678901234567890123456789"
        assert "trade_plan" not in base["input_manifest"]
        assert "portfolio_context" not in base["resolution"]
        assert "action" not in base["resolution"] and "action" not in base["capital_action"]
        assert base["expressions"] == {}
        assert base["selected_expression"] is None
        assert base["portfolio_impacts"] == {}
        refs = base["evidence_refs"]
        assert refs["plan_impact"] == refs["resolution_impact"] == refs["portfolio_impacts"]["STOCK"]
        assert connection.execute("SELECT count(*) FROM analysis.decision_input_payload").fetchone()["count"] == 3
    assert compact_decision_evidence_batch(storage.runtime, execute=True)["compacted"] == 0


def test_repeated_evaluation_reuses_decision_and_checkpoint(storage):
    from datetime import timedelta

    with storage.runtime.transaction() as connection:
        reconcile_instrument(connection, "STOR")
    repository = TickerDecisionRepository(storage.runtime)
    cutoff = datetime(2026, 9, 25, 14, tzinfo=UTC)
    quote = {"symbol": "STOR", "price": 100, "observed_at": "2026-09-25T13:59:00Z",
             "available_at": "2026-09-25T13:59:00Z", "confirmed": True}

    def decision(check, row):
        return build_ticker_decision("STOR", {"quotes": [row]}, as_of=check)

    first = repository.publish(decision(cutoff, quote))
    second = repository.publish(decision(cutoff + timedelta(minutes=1), quote))
    retry = repository.publish(decision(cutoff + timedelta(minutes=1), quote))
    assert first["ticker_decision_id"] == second["ticker_decision_id"] == retry["ticker_decision_id"]
    assert second["status"] == retry["status"] == "unchanged"
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) FROM analysis.ticker_decision").fetchone()["count"] == 1
        checkpoint = connection.execute("SELECT * FROM analysis.ticker_decision_checkpoint").fetchone()
        assert checkpoint["evaluation_count"] == 2
        assert checkpoint["first_checked_at"] == cutoff
        assert checkpoint["last_checked_at"] == cutoff + timedelta(minutes=1)
        row = connection.execute("SELECT as_of, last_evaluated_at FROM analysis.ticker_decision").fetchone()
        assert row["as_of"] == cutoff and row["last_evaluated_at"] == cutoff + timedelta(minutes=1)
    changed = repository.publish(decision(cutoff + timedelta(minutes=2), {**quote, "price": 101}))
    assert changed["ticker_decision_id"] != first["ticker_decision_id"]
    revised = repository.publish(decision(cutoff + timedelta(minutes=3),
        {**quote, "price": 101, "revision": "corrected"}))
    assert revised["ticker_decision_id"] != changed["ticker_decision_id"]
    fresh = repository.publish(decision(cutoff + timedelta(minutes=4),
        {**quote, "price": 101, "revision": "corrected",
         "observed_at": "2026-09-25T14:04:00Z", "available_at": "2026-09-25T14:04:00Z"}))
    assert fresh["ticker_decision_id"] != revised["ticker_decision_id"]
    same_input = decision(cutoff + timedelta(minutes=4),
        {**quote, "price": 101, "revision": "corrected",
         "observed_at": "2026-09-25T14:04:00Z", "available_at": "2026-09-25T14:04:00Z"})
    risk = same_input.risk_policy_snapshot.model_copy(update={
        "ticker_loss_budget_pct": same_input.risk_policy_snapshot.ticker_loss_budget_pct + 0.001,
    })
    same_input = same_input.model_copy(update={
        "risk_policy_snapshot": risk,
    })
    risk_change = repository.publish(same_input)
    assert risk_change["ticker_decision_id"] != fresh["ticker_decision_id"]
    assert risk_change["decision_revision"] == fresh["decision_revision"]
    restored_terms = repository.publish(decision(cutoff + timedelta(minutes=4),
        {**quote, "price": 101, "revision": "corrected",
         "observed_at": "2026-09-25T14:04:00Z", "available_at": "2026-09-25T14:04:00Z"}))
    assert restored_terms["ticker_decision_id"] not in {
        risk_change["ticker_decision_id"], fresh["ticker_decision_id"]}
    stale = repository.publish(decision(cutoff + timedelta(days=1),
        {**quote, "price": 101, "revision": "corrected",
         "observed_at": "2026-09-25T14:04:00Z", "available_at": "2026-09-25T14:04:00Z"}))
    assert stale["ticker_decision_id"] != restored_terms["ticker_decision_id"]
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) FROM analysis.ticker_decision").fetchone()["count"] == 7
        assert connection.execute("SELECT status FROM analysis.ticker_decision WHERE id = %s", [fresh["ticker_decision_id"]]).fetchone()["status"] == "superseded"
        assert connection.execute("SELECT status FROM analysis.ticker_decision WHERE id = %s", [risk_change["ticker_decision_id"]]).fetchone()["status"] == "superseded"
        assert connection.execute("SELECT status FROM analysis.ticker_decision WHERE id = %s", [restored_terms["ticker_decision_id"]]).fetchone()["status"] == "superseded"
        assert connection.execute("SELECT status FROM analysis.ticker_decision WHERE id = %s", [stale["ticker_decision_id"]]).fetchone()["status"] == "published"


def test_malformed_prior_decision_is_replaced_instead_of_reused(storage):
    with storage.runtime.transaction() as connection:
        reconcile_instrument(connection, "MALFORMED")
    repository = TickerDecisionRepository(storage.runtime)
    decision = build_ticker_decision("MALFORMED", {"quotes": [{
        "symbol": "MALFORMED", "price": 100, "available_at": "2026-09-25T13:59:00Z",
        "confirmed": True,
    }]}, as_of=datetime(2026, 9, 25, 14, tzinfo=UTC))
    first = repository.publish(decision)
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET tactical = '{}'::jsonb WHERE id = %s",
                           [first["ticker_decision_id"]])
    second = repository.publish(decision)
    assert second["status"] == "published"
    assert second["ticker_decision_id"] != first["ticker_decision_id"]
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT status FROM analysis.ticker_decision WHERE id = %s", [first["ticker_decision_id"]]).fetchone()["status"] == "superseded"


def test_backdated_retry_cannot_reuse_superseded_decision(storage):
    with storage.runtime.transaction() as connection:
        reconcile_instrument(connection, "BACKDATE")
    repository = TickerDecisionRepository(storage.runtime)
    cutoff = datetime(2026, 9, 25, 14, tzinfo=UTC)
    old = build_ticker_decision("BACKDATE", {}, as_of=cutoff)
    old_id = repository.publish(old)["ticker_decision_id"]
    current_id = repository.publish(build_ticker_decision(
        "BACKDATE", {}, as_of=cutoff + timedelta(minutes=1)
    ))["ticker_decision_id"]
    assert old_id != current_id
    with pytest.raises(ValueError, match="backdated evaluation"):
        repository.publish(old, reuse_only=True)
    assert repository.publish(old)["ticker_decision_id"] == old_id
    assert repository.latest("BACKDATE").as_of == cutoff + timedelta(minutes=1)
    with storage.runtime.read() as connection:
        assert connection.execute(
            "SELECT count(*) FROM analysis.ticker_decision WHERE instrument_id = "
            "(SELECT id FROM catalog.instrument WHERE symbol = 'BACKDATE')"
        ).fetchone()["count"] == 2


def test_backdated_retry_reuses_exact_legacy_row_without_fingerprint(storage):
    with storage.runtime.transaction() as connection:
        reconcile_instrument(connection, "LEGACYLATE")
    repository = TickerDecisionRepository(storage.runtime)
    cutoff = datetime(2026, 9, 25, 14, tzinfo=UTC)
    old = build_ticker_decision("LEGACYLATE", {}, as_of=cutoff)
    old_id = repository.publish(old)["ticker_decision_id"]
    repository.publish(build_ticker_decision("LEGACYLATE", {}, as_of=cutoff + timedelta(minutes=1)))
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET semantic_fingerprint = NULL WHERE id = %s", [old_id])
    assert repository.publish(old)["ticker_decision_id"] == old_id
    with storage.runtime.read() as connection:
        row = connection.execute("SELECT semantic_fingerprint FROM analysis.ticker_decision WHERE id = %s", [old_id]).fetchone()
        assert row["semantic_fingerprint"] is not None
        assert connection.execute("SELECT count(*) FROM analysis.ticker_decision").fetchone()["count"] == 2


def test_backdated_retry_does_not_reuse_malformed_or_quarantined_history(storage):
    with storage.runtime.transaction() as connection:
        reconcile_instrument(connection, "BADLATE")
    repository = TickerDecisionRepository(storage.runtime)
    cutoff = datetime(2026, 9, 25, 14, tzinfo=UTC)
    old = build_ticker_decision("BADLATE", {}, as_of=cutoff)
    old_id = repository.publish(old)["ticker_decision_id"]
    repository.publish(build_ticker_decision("BADLATE", {}, as_of=cutoff + timedelta(minutes=1)))
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET tactical = '{}'::jsonb WHERE id = %s", [old_id])
    replacement_id = repository.publish(old)["ticker_decision_id"]
    assert replacement_id != old_id
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET status = 'quarantined' WHERE id = %s", [replacement_id])
    assert repository.publish(old)["ticker_decision_id"] not in {old_id, replacement_id}


def test_evidence_batch_budget_includes_expression_bytes(storage, monkeypatch):
    decision_id = _decision(storage.runtime)
    with storage.runtime.transaction() as connection:
        connection.execute("UPDATE analysis.ticker_decision SET expressions = %s WHERE id = %s",
                           [Jsonb({"STOCK": {"raw": "x" * 1000}}), decision_id])
        without_expressions = connection.execute("""
            SELECT octet_length(input_manifest::text) + octet_length(resolution::text)
                 + octet_length(capital_action::text) + octet_length(opportunity_episode::text)
                 + octet_length(portfolio_impacts::text) AS bytes
            FROM analysis.ticker_decision WHERE id = %s
        """, [decision_id]).fetchone()["bytes"]
    monkeypatch.setattr(decision_storage, "MAX_EVIDENCE_BATCH_BYTES", without_expressions + 1)
    with pytest.raises(ValueError, match="evidence budget"):
        compact_decision_evidence_batch(storage.runtime, execute=True)
    with storage.runtime.read() as connection:
        assert not connection.execute("SELECT evidence_normalized FROM analysis.ticker_decision WHERE id = %s", [decision_id]).fetchone()["evidence_normalized"]


def test_daily_accounting_uses_postgres_volume_and_three_distinct_days(storage):
    day = datetime.now(UTC).date()
    with storage.runtime.transaction() as connection:
        for offset, free in ((2, 110), (1, 105)):
            connection.execute("""
                INSERT INTO ops.storage_daily_accounting
                  (sample_day, sampled_at, database_bytes, volume_free_bytes,
                   logical_evidence_bytes, archived_bytes, archive_rows)
                VALUES (%s, now(), 1000000, %s, 500000, 0, 0)
            """, [day - timedelta(days=offset), free * 1024**3])
    report = storage.account(record=True)
    assert report["sample_count"] == 3
    assert report["forecast_confidence"] == "measured"
    assert report["volume_free_bytes"] == 100 * 1024**3
    assert report["measured_growth_bytes_per_day"] == 5 * 1024**3
    assert report["forecast_30d_free_bytes"] == 0
    assert report["status"] == "degraded"


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
    monkeypatch.setattr("investment_panel.infrastructure.postgres.archive_io.MAX_CHUNK_BYTES", 1)
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


def test_ranking_publication_references_immutable_decision_payload_once(storage):
    analysis = AnalysisRepository(storage.runtime)
    cutoff = datetime.now(UTC)
    run_id = analysis.start_run("ticker-opportunity-ranking", input_cutoff=cutoff,
                                code_version="canonical-test", inputs={"version": 1})
    analysis.finish_run(run_id, "succeeded")
    rank = {"ticker": "CANON", "rank_id": "rank-canon", "trade_rank": 1,
            "eligibility": "ACTIONABLE", "exact": 0.12345678901234568}
    publication_id = analysis.publish(run_id, "ticker-opportunity-ranking",
                                      {"opportunity_rank": [rank]})
    with storage.runtime.read() as connection:
        item = connection.execute("""
            SELECT item.content_hash, item.decision_payload_hash, evidence.payload
            FROM app.publication publication
            JOIN app.publication_bundle_item item ON item.bundle_id = publication.bundle_id
            JOIN analysis.decision_input_payload evidence
              ON evidence.content_hash = item.decision_payload_hash
            WHERE publication.id = %s
        """, [publication_id]).fetchone()
        assert item["content_hash"] is None
        assert item["decision_payload_hash"]
        assert item["payload"] == rank
        assert connection.execute(
            "SELECT count(*) AS n FROM app.publication_payload"
        ).fetchone()["n"] == 0
    assert analysis.publication_rows("ticker-opportunity-ranking", "opportunity_rank")[0] == rank
    current_id, models = analysis.current_ranking_rows()
    assert current_id == str(publication_id)
    assert models["opportunity_rank"][0]["rank_id"] == rank["rank_id"]


def test_superseded_ranking_archive_includes_referenced_evidence(storage):
    analysis = AnalysisRepository(storage.runtime)
    cutoff = datetime.now(UTC)
    ids = []
    for version in (1, 2):
        run_id = analysis.start_run("ticker-opportunity-ranking", input_cutoff=cutoff,
                                    code_version=f"canonical-v{version}", inputs={"version": version})
        analysis.finish_run(run_id, "succeeded")
        ids.append(analysis.publish(run_id, "ticker-opportunity-ranking", {
            "opportunity_rank": [{"ticker": "CANON", "rank_id": f"rank-{version}"}],
        }))
    with storage.runtime.transaction() as connection:
        old = cutoff - timedelta(days=100)
        connection.execute("UPDATE app.publication SET created_at = %s, published_at = %s WHERE id = %s",
                           [old, old, ids[0]])
        digest = connection.execute("""
            SELECT item.decision_payload_hash FROM app.publication publication
            JOIN app.publication_bundle_item item ON item.bundle_id = publication.bundle_id
            WHERE publication.id = %s
        """, [ids[0]]).fetchone()["decision_payload_hash"]
    result = RetentionRepository(storage.runtime, archive_root=storage.archive_root).prune_publications(now=cutoff)
    assert result["publications"] == 1
    manifests = [json.loads(gzip.decompress(path.read_bytes()))
                 for path in storage.archive_root.rglob("*.json.gz")]
    rows = [row for manifest in manifests for row in manifest if isinstance(manifest, list)]
    assert any(row["relation"] == "analysis.decision_input_payload"
               and json.loads(row["row_json"])["content_hash"] == digest for row in rows)
    with storage.runtime.read() as connection:
        assert connection.execute(
            "SELECT count(*) AS n FROM analysis.decision_input_payload WHERE content_hash = %s", [digest]
        ).fetchone()["n"] == 0
    assert analysis.publication_rows("ticker-opportunity-ranking", "opportunity_rank")[0]["rank_id"] == "rank-2"


def test_superseded_outcome_publication_archives_without_affecting_current(storage):
    analysis = AnalysisRepository(storage.runtime)
    cutoff = datetime.now(UTC)
    ids = []
    for version in (1, 2):
        run_id = analysis.start_run("ticker_outcome_attribution", input_cutoff=cutoff,
                                    code_version=f"outcome-v{version}", inputs={"version": version})
        analysis.finish_run(run_id, "succeeded")
        ids.append(analysis.publish(run_id, "ticker-outcome-attribution", {
            "outcome_attribution": [{"stable_unit_key": "plan:TACTICAL:1", "version": version}],
        }))
    with storage.runtime.transaction() as connection:
        old = cutoff - timedelta(days=100)
        connection.execute("UPDATE app.publication SET created_at = %s, published_at = %s WHERE id = %s",
                           [old, old, ids[0]])
    result = RetentionRepository(storage.runtime, archive_root=storage.archive_root).prune_publications(now=cutoff)
    assert result["publications"] == 1
    assert analysis.publication_rows("ticker-outcome-attribution", "outcome_attribution")[0]["version"] == 2
    manifests = [json.loads(gzip.decompress(path.read_bytes()))
                 for path in storage.archive_root.rglob("*.json.gz")]
    rows = [row for manifest in manifests for row in manifest if isinstance(manifest, list)]
    assert any(row["relation"] == "app.publication_payload"
               and json.loads(row["row_json"])["payload"]["version"] == 1 for row in rows)


def test_option_plan_does_not_write_and_scratch_uses_configured_dsn(storage, monkeypatch, tmp_path):
    archive_call = Mock(side_effect=AssertionError("dry run must not archive"))
    monkeypatch.setattr(storage, "_archive_option_partition", archive_call)
    with storage.runtime.transaction() as connection:
        connection.execute("SELECT raw.ensure_option_quote_partition('option_quote_202001', '2020-01-01', '2020-02-01')")
    plan = storage.archive_options(now=datetime(2028, 1, 1, tzinfo=UTC))
    assert plan["status"] == "dry_run" and plan["candidates"]
    assert archive_call.call_count == 0
    monkeypatch.setattr(storage, "_archive_option_partition", lambda _: {"verification_status": "verified", "manifest_id": 1})
    exported = storage.archive_options(now=datetime(2028, 1, 1, tzinfo=UTC), export=True)
    assert exported["status"] == "succeeded"
    assert exported["dry_run"] is False
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
    objects = [row for obj in objects for row in (obj if isinstance(obj, list) else [obj])]
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
    objects = [row for obj in objects for row in (obj if isinstance(obj, list) else [obj])]
    assert any(item["relation"] == "analysis.run" and "9007199254740993.1" in item["row_json"] for item in objects)


def test_publication_archive_covers_payload_hash_batch_boundary(storage):
    reference = datetime.now(UTC)
    analysis = AnalysisRepository(storage.runtime)
    old_run = analysis.start_run("storage-test", input_cutoff=reference, code_version="old", inputs={})
    analysis.finish_run(old_run, "succeeded")
    rows = [{"stable_key": f"item-{i}", "value": i} for i in range(501)]
    rows[-1]["blob"] = "x" * (8 * 1024**2)
    old_id = analysis.publish(old_run, "research", {"brief": rows})
    new_run = analysis.start_run("storage-test", input_cutoff=reference, code_version="new", inputs={})
    analysis.finish_run(new_run, "succeeded")
    new_id = analysis.publish(new_run, "research", {"brief": [rows[0]]})
    with storage.runtime.transaction() as connection:
        connection.execute(
            "UPDATE app.publication SET created_at = %s, published_at = %s WHERE id = %s",
            [reference - timedelta(days=100), reference - timedelta(days=100), old_id],
        )
    result = RetentionRepository(storage.runtime, archive_root=storage.archive_root).prune_publications(now=reference)
    assert result["publications"] == 1
    archived = [
        row for path in storage.archive_root.rglob("*.json.gz")
        for row in json.loads(gzip.decompress(path.read_bytes()))
        if row["relation"] == "app.publication_payload"
    ]
    assert len({json.loads(row["row_json"])["content_hash"] for row in archived}) == 500
    with storage.runtime.read() as connection:
        assert connection.execute("""
            SELECT count(*) FROM ops.storage_archive_manifest
            WHERE source_relation = 'app.publication_payload'
              AND format = 'postgres-copy-text-gzip.v1'
              AND verification_status = 'verified'
        """).fetchone()["count"] == 1
        assert connection.execute("SELECT count(*) FROM app.publication WHERE id = %s", [new_id]).fetchone()["count"] == 1
        assert connection.execute("SELECT count(*) FROM app.publication_payload").fetchone()["count"] == 1


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


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_manifest_cutover_requires_offline_restore_sidecar(storage, damage):
    _legacy(storage.runtime, _decision(storage.runtime), rows=1)
    worker = ManifestArchive(storage)
    worker.run(state="backfill", execute=True)
    token = _backup(storage)
    sidecar = next((storage.archive_root / KIND).glob("*.json"))
    if damage == "missing":
        sidecar.unlink()
    else:
        sidecar.write_text('{"wrong": "schema"}')
    with pytest.raises(ValueError, match="sidecar"):
        worker.run(state="cutover", execute=True, backup_token=token)
    with storage.runtime.read() as connection:
        assert connection.execute("SELECT count(*) AS count FROM analysis.ticker_input_manifest_legacy").fetchone()["count"] == 1


def test_manifest_scratch_restore_reuses_physical_storage_within_transaction(storage, monkeypatch):
    _legacy(storage.runtime, _decision(storage.runtime), rows=3)
    worker = ManifestArchive(storage)
    worker.run(state="backfill", batch_size=1, max_batches=4, execute=True)
    from investment_panel.infrastructure.postgres import manifest_archive
    original = manifest_archive._restore_chunk
    checked = []

    def checked_restore(connection, *args):
        before = connection.execute("SELECT pg_relation_filenode('pg_temp.market_manifest_verify') AS node").fetchone()["node"]
        original(connection, *args)
        after = connection.execute("SELECT pg_relation_filenode('pg_temp.market_manifest_verify') AS node").fetchone()["node"]
        assert before == after  # No deferred per-chunk replacement files accumulating until commit.
        checked.append(after)

    monkeypatch.setattr(manifest_archive, "_restore_chunk", checked_restore)
    assert worker.run(state="verify")["rows"] == 3
    assert len(checked) == 3 and len(set(checked)) == 1
