"""Lossless, bounded COPY envelope for a row too large for normal JSON packs.

The envelope retains the original PostgreSQL-rendered JSON *text*, schema and
source identity. It is restored into a scratch table and typed back to the source
relation before a manifest is recorded. This module never deletes source rows.
"""
from __future__ import annotations

import gzip
from hashlib import sha256
import os
from pathlib import Path
import tempfile
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from investment_panel.infrastructure.postgres.archive_io import (
    MAX_CHUNK_BYTES, archive_file_hash, sync_archive_directory, verify_copy_file,
)
from investment_panel.infrastructure.postgres.storage_archive import ensure_mounted_archive_root

if TYPE_CHECKING:
    from investment_panel.infrastructure.postgres.storage_archive import StorageArchiveService

FORMAT = "postgres-copy-text-gzip.v1"
COLUMNS = [{"name": name, "type": kind} for name, kind in (
    ("relation", "text"), ("source_columns", "jsonb"),
    ("source_database", "jsonb"), ("row_json", "text"),
)]


def _typed_restore(connection: Any, path: Path, relation: str, raw: str) -> None:
    table = sql.Identifier("row_archive_verify_" + uuid4().hex)
    # A savepoint preserves the original error and removes the scratch table
    # on failure; issuing DROP in an aborted transaction would mask that error.
    with connection.transaction():
        connection.execute(sql.SQL(
            "CREATE TEMP TABLE {} (relation text, source_columns jsonb, "
            "source_database jsonb, row_json text) ON COMMIT DROP"
        ).format(table))
        with gzip.open(path, "rb") as handle, connection.cursor() as cursor:
            with cursor.copy(sql.SQL("COPY {} FROM STDIN WITH (FORMAT text)").format(table)) as copy:
                for block in iter(lambda: handle.read(1024**2), b""):
                    copy.write(block)
        restored = connection.execute(sql.SQL(
            "SELECT count(*) = 1 AND bool_and(relation = %s AND row_json = %s "
            "AND to_jsonb(jsonb_populate_record(NULL::{}, row_json::jsonb)) = row_json::jsonb) AS same "
            "FROM {}"
        ).format(sql.Identifier(*relation.split(".")), table), [relation, raw]).fetchone()
        if not restored or restored["same"] is not True:
            raise ValueError("large row typed restore changed original values; source retained")
        connection.execute(sql.SQL("DROP TABLE {}").format(table))


def write_large_row(service: StorageArchiveService, connection: Any, kind: str,
                    relation: str, raw: str, columns: list[dict[str, Any]],
                    identity: dict[str, Any]) -> int:
    # Ordinary pack size remains 8 MiB. Large rows use the existing independently
    # enforced 64 MiB COPY restore budget, never an unbounded JSON loader.
    if kind not in {"derived", "publications"}:
        raise ValueError("unsupported large row archive kind")
    raw_bytes = len(raw.encode("utf-8"))
    if raw_bytes > MAX_CHUNK_BYTES:
        raise ValueError("source row exceeds the 64 MiB COPY restore budget; source retained")
    ensure_mounted_archive_root(service.archive_root)
    root = service.archive_root / kind / "large-rows"
    root.mkdir(parents=True, exist_ok=True)
    service._require_archive_capacity(MAX_CHUNK_BYTES)
    fd, name = tempfile.mkstemp(prefix=".row-archive-", suffix=".tmp", dir=root)
    temporary = Path(name)
    digest = sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            with gzip.GzipFile(filename="", fileobj=handle, mode="wb", mtime=0) as compressed:
                with connection.cursor() as cursor, cursor.copy(
                    "COPY (SELECT %s::text AS relation, %s::jsonb AS source_columns, "
                    "%s::jsonb AS source_database, %s::text AS row_json) TO STDOUT WITH (FORMAT text)",
                    [relation, Jsonb(columns), Jsonb(identity), raw],
                ) as copy:
                    for chunk in copy:
                        block = bytes(chunk)
                        size += len(block)
                        if size > MAX_CHUNK_BYTES:
                            raise ValueError("escaped row exceeds the 64 MiB COPY restore budget; source retained")
                        digest.update(block)
                        compressed.write(block)
            handle.flush()
            os.fsync(handle.fileno())
        content_hash = digest.hexdigest()
        target = root / f"{content_hash}.copy.gz"
        artifact_hash = archive_file_hash(temporary)
        metadata = {
            "archive_contract": "postgres-row-json-copy.v1", "columns": COLUMNS,
            "source_columns": columns, "source_database": identity,
            "content_sha256": content_hash, "uncompressed_bytes": size,
        }
        # Never overwrite an existing corrupted content-addressed artifact.
        candidate = target if target.exists() else temporary
        ok, reason = verify_copy_file(candidate, artifact_hash, row_count=1, metadata=metadata)
        if not ok:
            raise ValueError(f"large row read-back failed: {reason}; source retained")
        _typed_restore(connection, candidate, relation, raw)
        metadata["typed_restore_verified"] = True
        if relation == "analysis.run":
            metadata["source_row_ids"] = [connection.execute(
                "SELECT (%s::jsonb)->>'id' AS id", [raw],
            ).fetchone()["id"]]
        if candidate == temporary:
            os.replace(temporary, target)
        sync_archive_directory(root)
        manifest_id, _ = service._record_manifest(
            archive_kind=kind, source_relation=relation, path=target,
            artifact_hash=artifact_hash, row_count=1, range_start=None,
            range_end=None, metadata=metadata, archive_format=FORMAT,
        )
        if service.verify(manifest_id=manifest_id)["verified"] != 1:
            raise ValueError("large row manifest verification failed; source retained")
        return manifest_id
    finally:
        temporary.unlink(missing_ok=True)
