"""Adversarial catalog, index, and capacity-accounting PostgreSQL scenarios."""
from math import ceil
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.infrastructure.postgres.storage_archive import StorageArchiveService
from investment_panel.infrastructure.postgres.storage_audit import audit_storage


@pytest.fixture
def runtime(application_postgres_dsn):
    value = DatabaseRuntime(application_postgres_dsn)
    value.open()
    try:
        yield value
    finally:
        value.close()


def test_inventory_finds_unmanaged_relations_without_reading_private_values(runtime, migrated_postgres_dsn):
    with psycopg.connect(migrated_postgres_dsn) as owner:
        owner.execute("CREATE SCHEMA audit_unmanaged")
        owner.execute("""CREATE TABLE audit_unmanaged.probe (
            a integer, b integer, secret text DEFAULT 'PRIVATE_AUDIT_SENTINEL')""")
        owner.execute("INSERT INTO audit_unmanaged.probe(a, b) VALUES (1, 2)")
        owner.execute("CREATE INDEX audit_probe_a ON audit_unmanaged.probe(a)")
        owner.execute("CREATE INDEX audit_probe_a_copy ON audit_unmanaged.probe(a)")
        owner.execute("CREATE INDEX audit_probe_include ON audit_unmanaged.probe(a) INCLUDE (b)")
        owner.execute("CREATE UNIQUE INDEX audit_probe_unique ON audit_unmanaged.probe(a)")
        owner.execute("CREATE INDEX audit_probe_partial ON audit_unmanaged.probe(a) WHERE a IS NOT NULL")
    report = audit_storage(runtime)
    tables = {row["relation"]: row for row in report["tables"]}
    assert "audit_unmanaged.probe" in tables
    assert tables["audit_unmanaged.probe"]["can_read"] is False
    assert "audit_unmanaged.probe" in report["coverage"]["unmanaged_relations"]
    assert "audit_unmanaged.probe" in report["coverage"]["unreadable_tables"]
    assert "PRIVATE_AUDIT_SENTINEL" not in str(report)
    groups = [row for row in report["equivalent_index_candidates"] if row["relation"] == "audit_unmanaged.probe"]
    assert len(groups) == 1
    assert set(groups[0]["indexes"]) == {"audit_probe_a", "audit_probe_a_copy"}
    overlaps = [row for row in report["overlapping_nonnull_index_candidates"]
                if row["relation"] == "audit_unmanaged.probe"]
    assert {row["partial_index"] for row in overlaps} == {"audit_probe_partial"}
    assert all(row["action"] == "review_only" for row in overlaps)


def test_reference_indexes_are_complete_without_duplicate_one_column_copies(runtime):
    targets = [
        ("app.publication_bundle_item", "content_hash"),
        ("app.publication_bundle_item", "decision_payload_hash"),
        ("app.current_publication_item", "content_hash"),
        ("app.current_publication_item", "decision_payload_hash"),
        ("analysis.ticker_decision", "market_state_context_hash"),
        ("analysis.ticker_decision", "risk_policy_context_hash"),
    ]
    with runtime.read() as connection:
        for relation, column in targets:
            found = connection.execute("""SELECT index_relation.relname
                FROM pg_index idx JOIN pg_class index_relation ON index_relation.oid = idx.indexrelid
                JOIN pg_attribute col ON col.attrelid = idx.indrelid AND col.attnum = idx.indkey[0]
                JOIN pg_am access ON access.oid = index_relation.relam
                WHERE idx.indrelid = %s::regclass AND col.attname = %s
                  AND idx.indnkeyatts = 1 AND idx.indnatts = 1
                  AND access.amname = 'btree' AND idx.indisvalid AND idx.indisready""",
                [relation, column]).fetchall()
            assert len(found) == 1, (relation, column, found)
        connection.execute("SET LOCAL enable_seqscan = off")
        for column in ("market_state_context_hash", "risk_policy_context_hash"):
            plan = connection.execute(f"""EXPLAIN (FORMAT JSON) SELECT id
                FROM analysis.ticker_decision WHERE {column} = ANY(%s)""", [["0" * 64]]).fetchone()
            assert "Index Name" in str(plan), plan


def test_capacity_accounting_never_labels_fallback_or_file_allocation_as_measured_logical_growth(
    runtime, tmp_path, monkeypatch,
):
    monkeypatch.setenv("MARKET_STORAGE_DATABASE_PATH", str(tmp_path))
    monkeypatch.setattr("shutil.disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3, total=200 * 1024**3))
    service = StorageArchiveService(runtime, Path(tmp_path) / "nas")
    report = service.account()
    assert report["sample_count"] == 0
    assert report["forecast_confidence"] == "provisional"
    assert report["measured_growth_bytes_per_day"] is None
    assert report["forecast_growth_bytes_per_day"] == ceil(0.7 * 1024**3)
    assert report["tracked_evidence_allocated_bytes"] > 0
    assert "logical_evidence_bytes" not in report
    assert "logical_evidence_growth_bytes_per_day" not in report
    assert report["archive_backlog_basis"] == "old_local_rows_not_eligibility_count"
    assert report["filesystem_bytes_recovered"] is None
    assert report["reusable_bytes"] is None
