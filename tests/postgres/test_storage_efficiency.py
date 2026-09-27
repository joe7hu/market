"""Storage regressions through real restricted-role PostgreSQL workflows."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from investment_panel.infrastructure.postgres.analysis import AnalysisRepository
from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime


@pytest.fixture
def runtime(application_postgres_dsn):
    value = DatabaseRuntime(application_postgres_dsn)
    value.open()
    try:
        yield value
    finally:
        value.close()


def _capture(runtime):
    repo = IngestionRepository(runtime)
    repo.register_source("storage-audit", name="Audit", family="broker", kind="option_chain")
    at = datetime(2026, 9, 25, 15, tzinfo=UTC)
    run_id = repo.start_run("storage-audit", "option_quotes", started_at=at)
    row = {"symbol": "QQQ", "expiration": "2026-10-16", "strike": 500,
           "option_type": "call", "provider_symbol": "audit-contract", "bid": 1.125,
           "ask": 1.375, "style": "american", "settlement": "physical",
           "deliverable_key": "audit-standard", "standard_contract_verified": True,
           "provider_payload": {"instrument": {"id": "audit-contract"},
                                "untouched": {"original": "evidence"}}}
    kwargs = {"source_id": "storage-audit", "observed_at": at,
              "market_session": "regular", "universe": "audit", "rows": [row]}
    return repo, run_id, kwargs


def _versions(runtime):
    with runtime.read() as connection:
        return {
            relation: connection.execute(
                f"SELECT tableoid::oid AS relation, ctid::text, xmin::text FROM {relation} ORDER BY 1, 2"
            ).fetchall()
            for relation in ("raw.option_snapshot", "raw.option_quote", "catalog.option_contract", "ingest.run")
        }


def test_identical_option_retry_does_not_rewrite_any_persistent_row(runtime):
    repo, run_id, kwargs = _capture(runtime)
    first = repo.store_option_snapshot(run_id, **kwargs)
    before = _versions(runtime)
    assert repo.store_option_snapshot(run_id, **kwargs) == first
    assert _versions(runtime) == before


def test_real_quote_correction_and_payload_change_are_not_suppressed(runtime):
    repo, run_id, kwargs = _capture(runtime)
    first = repo.store_option_snapshot(run_id, **kwargs)
    before = _versions(runtime)
    kwargs["rows"][0].update(bid=1.25, provider_payload={"instrument": {"id": "audit-contract"}, "revision": 2})
    assert repo.store_option_snapshot(run_id, **kwargs) == first
    after = _versions(runtime)
    assert after["raw.option_quote"] != before["raw.option_quote"]
    assert after["catalog.option_contract"] == before["catalog.option_contract"]
    with runtime.read() as connection:
        quote = connection.execute("SELECT bid, provider_payload FROM raw.option_quote").fetchone()
    assert float(quote["bid"]) == 1.25
    assert quote["provider_payload"]["revision"] == 2
    assert repo.store_option_snapshot(run_id, **kwargs) == first
    assert _versions(runtime) == after


def test_new_capture_reuses_contract_but_preserves_new_observation(runtime):
    repo, run_id, kwargs = _capture(runtime)
    first = repo.store_option_snapshot(run_id, **kwargs)
    before = _versions(runtime)
    kwargs["observed_at"] += timedelta(minutes=5)
    second = repo.store_option_snapshot(run_id, **kwargs)
    assert first["snapshot_id"] != second["snapshot_id"]
    after = _versions(runtime)
    assert len(after["raw.option_quote"]) == 2
    assert after["catalog.option_contract"] == before["catalog.option_contract"]


def test_source_registration_retry_does_not_rewrite_metadata(runtime):
    repo, _, _ = _capture(runtime)
    with runtime.read() as connection:
        before = connection.execute("SELECT xmin::text, updated_at FROM ingest.source WHERE id = 'storage-audit'").fetchone()
    repo.register_source("storage-audit", name="Audit", family="broker", kind="option_chain")
    with runtime.read() as connection:
        after = connection.execute("SELECT xmin::text, updated_at FROM ingest.source WHERE id = 'storage-audit'").fetchone()
    assert before == after
    repo.register_source("storage-audit", name="New descriptive name", family="broker", kind="option_chain")
    with runtime.read() as connection:
        assert connection.execute("SELECT name FROM ingest.source WHERE id = 'storage-audit'").fetchone()["name"] == "New descriptive name"


def test_publication_payload_reference_indexes_cover_gc_and_fk_checks(runtime):
    with runtime.read() as connection:
        for relation in ("app.publication_bundle_item", "app.current_publication_item"):
            for column in ("decision_payload_hash", "content_hash"):
                assert connection.execute("""SELECT 1 FROM pg_index idx
                    JOIN pg_attribute col ON col.attrelid = idx.indrelid AND col.attnum = idx.indkey[0]
                    JOIN pg_class index_relation ON index_relation.oid = idx.indexrelid
                    JOIN pg_am access ON access.oid = index_relation.relam
                    WHERE idx.indrelid = %s::regclass AND col.attname = %s
                      AND access.amname = 'btree' AND idx.indisvalid AND idx.indisready""",
                    [relation, column]).fetchone(), (relation, column)


def _walk(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _walk(child)


def test_current_option_projection_does_not_expand_historical_candidates(runtime):
    analysis = AnalysisRepository(runtime)
    snapshot_keys = ("snapshot_time", "ticker", "underlying_price", "expiration", "strike",
                     "option_type", "bid", "ask", "mid", "volume", "open_interest", "iv",
                     "delta", "dte", "spread_pct", "data_source", "contract_id", "raw")
    feature_keys = ("snapshot_time", "contract_id", "ticker", "required_2x_price",
                   "required_5x_price", "required_10x_price", "required_move_pct",
                   "liquidity_score", "convexity_score", "raw")
    for generation in range(8):
        candidates = []
        for number in range(12):
            candidate = dict.fromkeys((*snapshot_keys, *feature_keys))
            candidate.update(candidate_event_id=str(uuid4()), contract_id=str(number), ticker="QQQ",
                             mid=1.125, raw={"generation": generation})
            candidates.append(candidate)
        run_id = analysis.start_run("options-radar", input_cutoff=datetime.now(UTC),
                                    code_version="storage-audit", inputs={"generation": generation})
        latest = analysis.publish(run_id, "options-radar", {
            "candidate_event": candidates,
            "option_snapshot": [{key: row[key] for key in snapshot_keys} for row in candidates],
            "option_features": [{key: row[key] for key in feature_keys} for row in candidates],
        })
    with runtime.read() as connection:
        plan = connection.execute("""EXPLAIN (ANALYZE, FORMAT JSON)
            SELECT payload FROM app.current_publication_item_read
            WHERE scope = 'options-radar' AND model_name = 'option_snapshot'""").fetchone()["QUERY PLAN"][0]["Plan"]
        rows = connection.execute("""SELECT payload FROM app.publication_content_item
            WHERE publication_id = %s AND model_name = 'option_snapshot' ORDER BY rank""", [latest]).fetchall()
    assert len(rows) == 12 and all(row["payload"]["raw"] == {"generation": 7} for row in rows)
    for node in _walk(plan):
        if node["Node Type"] == "WindowAgg":
            assert node["Actual Rows"] <= 12, node
        if node.get("Function Name") == "option_bundle_projection":
            assert node["Actual Loops"] == 1, node


def test_storage_audit_covers_catalog_without_reading_application_values(runtime):
    from investment_panel.infrastructure.postgres.storage_audit import audit_storage

    report = audit_storage(runtime)
    assert report["format"] == "market-storage-audit.v1"
    assert report["read_only"] is True
    assert report["filesystem_bytes_recovered"] is None
    tables = {row["relation"]: row for row in report["tables"]}
    assert "catalog.option_contract" in tables
    assert "analysis.ticker_decision" in tables
    assert all(row["allocated_bytes"] >= row["heap_bytes"] for row in tables.values())
    columns = {(row["relation"], row["column"]): row for row in report["columns"]}
    assert ("raw.option_quote", "provider_payload") in columns
    assert "most_common_vals" not in str(report)
    assert "phase4-test-allocation-signing-key" not in str(report)
    assert report["indexes"] and report["constraints"]
    assert report["coverage"]["table_count"] == len(tables)
    with runtime.read() as connection:
        assert connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute("CREATE TABLE public.audit_must_not_write(id integer)")
