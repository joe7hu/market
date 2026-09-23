"""Bounded, lossless evacuation of the retired duplicate input-manifest table.

COPY text preserves every column (including legacy row IDs and original/revised
values). Files and sidecars are written directly to the NAS. A cutover compares
EVERY source byte against verified chunks, restores each chunk through COPY in
a bounded temporary table, and drops only the obsolete table, without CASCADE.
No decision, outcome, trade, or publication row is deleted.
"""

from __future__ import annotations

from hashlib import sha256
import gzip
import json
import os
from pathlib import Path
import tempfile
from typing import Any, TYPE_CHECKING

from psycopg import sql
from psycopg.types.json import Jsonb

from investment_panel.infrastructure.postgres.migrations import HEAD_REVISION
from investment_panel.infrastructure.postgres.decision_storage import require_maintenance_headroom
from investment_panel.infrastructure.postgres.runtime import JOB_PROFILE

if TYPE_CHECKING:
    from investment_panel.infrastructure.postgres.storage_archive import StorageArchiveService

RELATION = "analysis.ticker_input_manifest_legacy"
KIND = "decision-manifests"
FORMAT = "postgres-copy-text-gzip.v1"
CHECKPOINT = "decision-manifests-copy-v1"
MAX_CHUNK_BYTES = 64 * 1024**2
COPY_SETTINGS = """SET LOCAL TIME ZONE 'UTC'; SET LOCAL DateStyle = 'ISO, YMD';
SET LOCAL IntervalStyle = 'postgres'; SET LOCAL extra_float_digits = 3;
SET LOCAL bytea_output = 'hex'; SET LOCAL client_encoding = 'UTF8'"""


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sync_archive_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_copy_file(path: Path, expected_sha256: str, *, row_count: int, metadata: dict[str, Any]) -> tuple[bool, str]:
    """Verify with bounded memory; no JSON materialization or local temp file."""
    try:
        if _file_hash(path) != expected_sha256:
            return False, "sha256_mismatch"
        digest = sha256()
        rows = size = 0
        with gzip.open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(block)
                if size > MAX_CHUNK_BYTES:
                    return False, "chunk_exceeds_restore_budget"
                digest.update(block)
                # COPY text escapes embedded newlines, unlike CSV.
                rows += block.count(b"\n")
        if digest.hexdigest() != metadata.get("content_sha256"):
            return False, "content_sha256_mismatch"
        if rows != row_count or size != metadata.get("uncompressed_bytes"):
            return False, "copy_size_or_row_count_mismatch"
        if not metadata.get("columns"):
            return False, "copy_schema_missing"
        return True, "copy_bytes_verified"
    except (OSError, EOFError) as exc:
        return False, f"copy_archive_unreadable:{type(exc).__name__}"


def _schema(connection: Any) -> tuple[int | None, list[dict[str, Any]]]:
    row = connection.execute("SELECT to_regclass(%s)::oid AS oid", [RELATION]).fetchone()
    if row["oid"] is None:
        return None, []
    columns = connection.execute(
        """SELECT attname AS name, format_type(atttypid, atttypmod) AS type,
                  attnotnull AS not_null
           FROM pg_attribute WHERE attrelid = %s AND attnum > 0 AND NOT attisdropped
           ORDER BY attnum""", [row["oid"]],
    ).fetchall()
    return int(row["oid"]), [dict(column) for column in columns]


def _copy_query(columns: list[dict[str, Any]], lower: int | None, upper: int) -> Any:
    names = sql.SQL(", ").join(sql.Identifier(column["name"]) for column in columns)
    return sql.SQL(
        "COPY (SELECT {} FROM analysis.ticker_input_manifest_legacy "
        "WHERE ({}::bigint IS NULL OR id > {}) AND id <= {} ORDER BY id) TO STDOUT WITH (FORMAT text)"
    ).format(names, sql.Literal(lower), sql.Literal(lower), sql.Literal(upper))


def _stream_source(connection: Any, columns: list[dict[str, Any]], lower: int | None, upper: int, output: Any = None) -> dict[str, Any]:
    digest = sha256()
    rows = size = 0
    connection.execute(COPY_SETTINGS)
    with connection.cursor() as cursor, cursor.copy(_copy_query(columns, lower, upper)) as copy:
        for chunk in copy:
            block = bytes(chunk)
            size += len(block)
            if size > MAX_CHUNK_BYTES:
                raise ValueError("manifest chunk exceeds 64 MiB; retry with a smaller --batch-size")
            digest.update(block)
            rows += block.count(b"\n")
            if output is not None:
                output.write(block)
    return {"content_sha256": digest.hexdigest(), "uncompressed_bytes": size, "row_count": rows}


def _restore_chunk(connection: Any, path: Path, row_count: int, expected_hash: str) -> None:
    connection.execute("TRUNCATE pg_temp.market_manifest_verify")
    with gzip.open(path, "rb") as handle, connection.cursor() as cursor:
        with cursor.copy("COPY pg_temp.market_manifest_verify FROM STDIN WITH (FORMAT text)") as copy:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                copy.write(block)
    restored = connection.execute("SELECT count(*) AS count FROM pg_temp.market_manifest_verify").fetchone()["count"]
    if restored != row_count:
        raise ValueError("manifest scratch restore row count mismatch")
    digest = sha256()
    with connection.cursor() as cursor, cursor.copy(
        "COPY (SELECT * FROM pg_temp.market_manifest_verify ORDER BY id) TO STDOUT WITH (FORMAT text)"
    ) as copy:
        for block in copy:
            digest.update(block)
    if digest.hexdigest() != expected_hash:
        raise ValueError("manifest typed restore changed original values")


class ManifestArchive:
    def __init__(self, service: StorageArchiveService) -> None:
        self.service = service
        self.runtime = service.runtime

    def run(self, *, state: str = "plan", batch_size: int = 500, max_batches: int = 1,
            execute: bool = False, backup_token: str | None = None) -> dict[str, Any]:
        if not 1 <= batch_size <= 10_000 or not 1 <= max_batches <= 100_000:
            raise ValueError("batch_size must be 1..10000 and max_batches must be 1..100000")
        if state == "plan" or (not execute and state in {"backfill", "cutover"}):
            return self.plan()
        if state not in {"backfill", "verify", "cutover"}:
            raise ValueError("unknown decision-manifests phase state")
        if state == "cutover":
            self.service._require_verified_backup(backup_token)
        with self.runtime.job_lock(CHECKPOINT) as acquired:
            if not acquired:
                raise RuntimeError("another decision-manifests archive operation is running")
            if state == "backfill":
                totals = {"chunks": 0, "rows": 0}
                for _ in range(max_batches):
                    result = self._export_batch(batch_size)
                    totals["chunks"] += int(result.get("chunks", 0))
                    totals["rows"] += int(result.get("rows", 0))
                    if result["status"] != "batch_complete":
                        break
                return {**result, **totals}
            return self._verify_or_cutover(cutover=state == "cutover")

    def plan(self) -> dict[str, Any]:
        with self.runtime.read(JOB_PROFILE) as connection:
            oid, _ = _schema(connection)
            if oid is None:
                return {"phase": KIND, "status": "already_compacted", "dry_run": True, "bytes": 0}
            row = connection.execute(
                "SELECT pg_total_relation_size(%s::oid) AS bytes, reltuples::bigint AS estimated_rows FROM pg_class WHERE oid = %s",
                [oid, oid],
            ).fetchone()
        return {"phase": KIND, "status": "legacy_archive_required", "dry_run": True, **dict(row),
                "archive_root": str(self.service.archive_root), "maximum_chunk_bytes": MAX_CHUNK_BYTES,
                "reclaim_method": "verified DROP of retired duplicate table; no decision deletion"}

    def _export_batch(self, batch_size: int) -> dict[str, Any]:
        from investment_panel.infrastructure.postgres.storage_archive import ensure_mounted_archive_root

        ensure_mounted_archive_root(self.service.archive_root)
        root = self.service.archive_root / KIND
        root.mkdir(parents=True, exist_ok=True)
        cursor = self.service._checkpoint_cursor(CHECKPOINT)
        lower = cursor.get("last_id")
        with self.runtime.snapshot(JOB_PROFILE) as connection:
            oid, columns = _schema(connection)
            if oid is None:
                return {"phase": KIND, "status": "already_compacted"}
            if cursor.get("source_oid") not in (None, oid):
                raise ValueError("legacy table identity changed; review the existing archive before resuming")
            ids = connection.execute(
                "SELECT id FROM analysis.ticker_input_manifest_legacy "
                "WHERE (%s::bigint IS NULL OR id > %s) ORDER BY id LIMIT %s", [lower, lower, batch_size],
            ).fetchall()
            if not ids:
                self.service._set_checkpoint(CHECKPOINT, KIND, RELATION, "succeeded", {**cursor, "source_oid": oid})
                return {"phase": KIND, "status": "export_complete"}
            upper = int(ids[-1]["id"])
            self.service._require_archive_capacity(MAX_CHUNK_BYTES)
            fd, temporary = tempfile.mkstemp(prefix=".copy-", suffix=".part", dir=root)
            try:
                with os.fdopen(fd, "wb") as handle:
                    with gzip.GzipFile(filename="", fileobj=handle, mode="wb", mtime=0) as compressed:
                        measured = _stream_source(connection, columns, lower, upper, compressed)
                    handle.flush()
                    os.fsync(handle.fileno())
                artifact_hash = _file_hash(Path(temporary))
                target = root / f"{artifact_hash}.copy.gz"
                metadata = {**measured, "columns": columns, "source_oid": oid,
                            "lower_id_exclusive": lower, "upper_id_inclusive": upper}
                ok, detail = verify_copy_file(Path(temporary), artifact_hash, row_count=measured["row_count"], metadata=metadata)
                if not ok:
                    raise ValueError(f"manifest export read-back failed: {detail}")
                if target.exists():
                    if _file_hash(target) != artifact_hash:
                        raise ValueError("existing content-addressed manifest archive is corrupt")
                    Path(temporary).unlink()
                else:
                    os.replace(temporary, target)
                sidecar = {"archive_kind": KIND, "format": FORMAT, "source_relation": RELATION,
                           "sha256": artifact_hash, "row_count": measured["row_count"],
                           "schema_revision": HEAD_REVISION, "metadata": metadata}
                sidecar_path = target.with_suffix(".json")
                sidecar_fd, sidecar_tmp = tempfile.mkstemp(prefix=".receipt-", dir=root)
                try:
                    with os.fdopen(sidecar_fd, "w") as handle:
                        json.dump(sidecar, handle, sort_keys=True)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(sidecar_tmp, sidecar_path)
                    sync_archive_directory(root)
                finally:
                    Path(sidecar_tmp).unlink(missing_ok=True)
            finally:
                Path(temporary).unlink(missing_ok=True)
        # A crash before the checkpoint can only repeat a content-addressed
        # write. Never advance the cursor on a failed filesystem/DB operation.
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """INSERT INTO ops.storage_archive_manifest
                   (archive_kind, source_relation, nas_uri, sha256, format, row_count,
                    schema_revision, verification_status, verified_at, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'verified', now(), %s)
                   ON CONFLICT (archive_kind, sha256) DO NOTHING""",
                [KIND, RELATION, str(target), artifact_hash, FORMAT, measured["row_count"], HEAD_REVISION, Jsonb(metadata)],
            )
        self.service._set_checkpoint(
            CHECKPOINT, KIND, RELATION, "paused", {"source_oid": oid, "last_id": upper},
            counts={"last_chunk_rows": measured["row_count"]},
        )
        return {"phase": KIND, "status": "batch_complete", "chunks": 1,
                "rows": measured["row_count"], "last_id": upper}

    def _verify_or_cutover(self, *, cutover: bool) -> dict[str, Any]:
        from investment_panel.infrastructure.postgres.storage_archive import ensure_mounted_archive_root
        ensure_mounted_archive_root(self.service.archive_root)
        require_maintenance_headroom(minimum_bytes=256 * 1024**2)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            oid, columns = _schema(connection)
            if oid is None:
                return {"phase": KIND, "status": "already_compacted", "dropped": False}
            # Blocks concurrent owner corrections too. No check-then-delete
            # race and no CASCADE. Ordinary app writes were revoked at upgrade.
            connection.execute("LOCK TABLE analysis.ticker_input_manifest_legacy IN ACCESS EXCLUSIVE MODE")
            oid, columns = _schema(connection)  # Recheck after acquiring the table lock.
            connection.execute(COPY_SETTINGS)
            manifests = connection.execute(
                """SELECT * FROM ops.storage_archive_manifest
                   WHERE archive_kind = %s AND source_relation = %s
                   ORDER BY (metadata->>'upper_id_inclusive')::bigint""", [KIND, RELATION],
            ).fetchall()
            connection.execute(
                "CREATE TEMP TABLE market_manifest_verify (LIKE analysis.ticker_input_manifest_legacy) ON COMMIT DROP"
            )
            previous = None
            rows = 0
            for manifest in manifests:
                metadata = dict(manifest["metadata"])
                if metadata.get("source_oid") != oid or metadata.get("columns") != columns:
                    raise ValueError("manifest source schema or identity mismatch")
                if metadata.get("lower_id_exclusive") != previous:
                    raise ValueError("manifest coverage gap or overlapping chunks")
                upper = metadata["upper_id_inclusive"]
                if previous is not None and upper <= previous:
                    raise ValueError("manifest chunk order is invalid")
                path = Path(manifest["nas_uri"])
                ok, detail = verify_copy_file(path, str(manifest["sha256"]), row_count=int(manifest["row_count"]), metadata=metadata)
                if not ok:
                    raise ValueError(f"manifest {manifest['id']} verification failed: {detail}")
                measured = _stream_source(connection, columns, previous, upper)
                if any(measured[key] != metadata[key] for key in ("content_sha256", "uncompressed_bytes", "row_count")):
                    raise ValueError("legacy rows changed or were omitted after export; no data was removed")
                _restore_chunk(connection, path, int(manifest["row_count"]), str(metadata["content_sha256"]))
                previous = upper
                rows += int(manifest["row_count"])
            tail = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM analysis.ticker_input_manifest_legacy "
                "WHERE (%s::bigint IS NULL OR id > %s)) AS present", [previous, previous],
            ).fetchone()["present"]
            if tail:
                raise ValueError("manifest export is incomplete; continue backfill before cutover")
            size = connection.execute("SELECT pg_total_relation_size(%s::oid) AS bytes", [oid]).fetchone()["bytes"]
            if cutover:
                connection.execute("DROP TABLE analysis.ticker_input_manifest_legacy RESTRICT")
                connection.execute(
                    """UPDATE ops.storage_archive_manifest
                       SET metadata = metadata || %s, verified_at = now(), verification_status = 'verified'
                       WHERE archive_kind = %s AND source_relation = %s""",
                    [Jsonb({"source_dropped": True, "copy_restore_verified": True}), KIND, RELATION],
                )
            result = {"phase": KIND, "status": "compacted" if cutover else "verified", "rows": rows,
                      "chunks": len(manifests), "dropped": cutover,
                      "relation_bytes_released_on_commit": int(size) if cutover else 0}
        self.service._set_checkpoint(CHECKPOINT, KIND, RELATION, "succeeded",
                                     {"source_oid": oid, "last_id": previous, "dropped": cutover}, counts=result)
        return result
