from __future__ import annotations

from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.types.json import Jsonb

from investment_panel.database.migrations import downgrade_database, upgrade_database
from investment_panel.database.instruments import reconcile_instrument
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.storage_archive import (
    ARCHIVE_FREE_RESERVE_BYTES,
    ArchiveCapacityError,
    StorageArchiveService,
    _ensure_mounted_archive_root,
)
from investment_panel.core.config import config_to_dict, load_config


@pytest.fixture
def storage_postgres_dsn(postgresql) -> str:
    info = postgresql.info
    credentials = info.user if not info.password else f"{info.user}:{info.password}"
    dsn = f"postgresql://{credentials}@{info.host}:{info.port}/{info.dbname}"
    upgrade_database(dsn)
    try:
        yield dsn
    finally:
        downgrade_database(dsn)


def test_storage_migration_adds_metadata_and_compatible_policy_columns(storage_postgres_dsn: str) -> None:
    with closing(psycopg.connect(storage_postgres_dsn)) as connection:
        tables = connection.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'ops' AND table_name LIKE 'storage_archive_%'
            ORDER BY table_name
            """
        ).fetchall()
        columns = connection.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'app' AND table_name = 'option_history_policy'
              AND column_name = ANY(%s)
            """,
            [["hot_retention_days", "archive_retention_days", "normalized_retention_days"]],
        ).fetchall()
    assert [row[0] for row in tables] == [
        "storage_archive_checkpoint",
        "storage_archive_manifest",
        "storage_archive_manifest_reference",
    ]
    assert {row[0] for row in columns} == {"hot_retention_days", "archive_retention_days", "normalized_retention_days"}


def test_storage_archive_dir_round_trips_through_config(tmp_path: Path) -> None:
    configured_archive = tmp_path / "archive-root"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"nas:\n  storage_archive_dir: {configured_archive}\n")

    config = load_config(config_path)

    assert config.nas.storage_archive_dir == configured_archive
    assert config_to_dict(config)["nas"]["storage_archive_dir"] == str(configured_archive)


def test_archive_is_content_addressed_verified_and_restorable(storage_postgres_dsn: str, tmp_path: Path) -> None:
    runtime = DatabaseRuntime(storage_postgres_dsn)
    runtime.open()
    try:
        archive_root = tmp_path / "nas" / "storage-archive" / "v1"
        archive_root.parent.mkdir(parents=True)
        service = StorageArchiveService(runtime, archive_root)
        first = service._write_json_gzip(
            "fundamental-history", [{"date": "2026-01-01", "value": 10}],
            source_relation="raw.fundamental_observation", row_count=1, metadata={"test": True},
        )
        second = service._write_json_gzip(
            "fundamental-history", [{"value": 10, "date": "2026-01-01"}],
            source_relation="raw.fundamental_observation", row_count=1, metadata={"test": True},
        )
        assert first["created"] is True
        assert second["created"] is False
        assert service.verify() == {"checked": 1, "verified": 1, "failed": 0}
        destination = tmp_path / "restore" / "history.json"
        restored = service.restore_to_file(1, destination)
        assert restored["destination"] == str(destination)
        assert destination.read_text() == '[{"date":"2026-01-01","value":10}]'
    finally:
        runtime.close()


def test_fundamental_archive_records_each_deduplicated_source_row(
    storage_postgres_dsn: str, tmp_path: Path,
) -> None:
    runtime = DatabaseRuntime(storage_postgres_dsn)
    runtime.open()
    try:
        archive_root = tmp_path / "nas" / "storage-archive" / "v1"
        archive_root.parent.mkdir(parents=True)
        service = StorageArchiveService(runtime, archive_root)
        first = service._write_json_gzip(
            "fundamental-history", [{"date": "2026-01-01", "value": 10}],
            source_relation="raw.fundamental_observation", row_count=1, metadata={},
        )
        second = service._write_json_gzip(
            "fundamental-history", [{"value": 10, "date": "2026-01-01"}],
            source_relation="raw.fundamental_observation", row_count=1, metadata={},
        )
        service._record_source_reference(
            manifest_id=int(first["manifest_id"]), source_relation="raw.fundamental_observation", source_row_id=11,
            source_ingest_run_id="00000000-0000-0000-0000-000000000011",
        )
        service._record_source_reference(
            manifest_id=int(second["manifest_id"]), source_relation="raw.fundamental_observation", source_row_id=12,
            source_ingest_run_id="00000000-0000-0000-0000-000000000012",
        )
        with closing(psycopg.connect(storage_postgres_dsn)) as connection:
            references = connection.execute(
                "SELECT manifest_id, source_row_id FROM ops.storage_archive_manifest_reference ORDER BY source_row_id"
            ).fetchall()
        assert first["created"] is True
        assert second["created"] is False
        assert references == [(int(first["manifest_id"]), 11), (int(first["manifest_id"]), 12)]
    finally:
        runtime.close()


def test_verification_fails_closed_for_corrupt_artifact(storage_postgres_dsn: str, tmp_path: Path) -> None:
    runtime = DatabaseRuntime(storage_postgres_dsn)
    runtime.open()
    try:
        archive_root = tmp_path / "nas" / "storage-archive" / "v1"
        archive_root.parent.mkdir(parents=True)
        service = StorageArchiveService(runtime, archive_root)
        result = service._write_json_gzip(
            "derived", [{"row": 1}, {"row": 2}], source_relation="analysis.option_relative_value", row_count=2, metadata={},
        )
        Path(result["path"]).write_bytes(b"not gzip")
        assert service.verify() == {"checked": 1, "verified": 0, "failed": 1}
    finally:
        runtime.close()


def test_market_nas_archive_rejects_a_stale_unmounted_mountpoint(monkeypatch) -> None:
    monkeypatch.setattr("investment_panel.database.storage_archive.os.path.ismount", lambda _path: False)

    with pytest.raises(FileNotFoundError, match="archive mount is unavailable"):
        _ensure_mounted_archive_root(Path("/Volumes/agent/data-sources/market-mini/storage-archive/v1"))


def test_archive_writer_rejects_writes_below_nas_reserve(storage_postgres_dsn: str, tmp_path: Path, monkeypatch) -> None:
    runtime = DatabaseRuntime(storage_postgres_dsn)
    runtime.open()
    try:
        archive_root = tmp_path / "nas" / "storage-archive" / "v1"
        archive_root.parent.mkdir(parents=True)
        service = StorageArchiveService(runtime, archive_root)
        monkeypatch.setattr(
            "investment_panel.database.storage_archive._disk_usage",
            lambda _path: SimpleNamespace(free=ARCHIVE_FREE_RESERVE_BYTES),
        )

        with pytest.raises(ArchiveCapacityError, match="below reserve"):
            service._write_json_gzip(
                "fundamental-history", [{"date": "2026-01-01", "value": 10}],
                source_relation="raw.fundamental_observation", row_count=1, metadata={},
            )
    finally:
        runtime.close()


def test_fundamental_archive_audit_reexports_a_corrected_source_row(
    storage_postgres_dsn: str, tmp_path: Path,
) -> None:
    runtime = DatabaseRuntime(storage_postgres_dsn)
    runtime.open()
    try:
        ingestion = IngestionRepository(runtime)
        ingestion.register_source("archive-test", name="Archive test", family="test", kind="fundamentals")
        first_run = ingestion.start_run("archive-test", "fundamentals")
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, "ARCH", asset_class="equity", category="test")
            observation_id = connection.execute(
                """
                INSERT INTO raw.fundamental_observation
                    (instrument_id, source_id, ingest_run_id, metric_set, period_end, observed_at, values)
                VALUES (%s, 'archive-test', %s, 'history', '2026-01-01', '2026-01-02T00:00:00Z', %s)
                RETURNING id
                """,
                [instrument_id, first_run, Jsonb({"history": [{"date": "2026-01-01", "value": 1}]})],
            ).fetchone()["id"]
        ingestion.finish_run(first_run, "succeeded")
        archive_root = tmp_path / "nas" / "storage-archive" / "v1"
        archive_root.parent.mkdir(parents=True)
        service = StorageArchiveService(runtime, archive_root)
        assert service.archive_fundamental_history(batch_size=10)["status"] == "paused"
        assert service.archive_fundamental_history(batch_size=10)["status"] == "succeeded"

        second_run = ingestion.start_run("archive-test", "fundamentals")
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE raw.fundamental_observation SET ingest_run_id = %s, values = %s WHERE id = %s",
                [second_run, Jsonb({"history": [{"date": "2026-01-01", "value": 2}]}), observation_id],
            )
        ingestion.finish_run(second_run, "succeeded")
        assert service.archive_fundamental_history(batch_size=10)["status"] == "succeeded"
        with closing(psycopg.connect(storage_postgres_dsn)) as connection:
            manifests = connection.execute(
                "SELECT count(*) AS count FROM ops.storage_archive_manifest"
            ).fetchone()[0]
            reference = connection.execute(
                "SELECT source_ingest_run_id FROM ops.storage_archive_manifest_reference "
                "WHERE source_relation = 'raw.fundamental_observation' AND source_row_id = %s",
                [observation_id],
            ).fetchone()[0]
        assert manifests == 2
        assert str(reference) == str(second_run)
    finally:
        runtime.close()
