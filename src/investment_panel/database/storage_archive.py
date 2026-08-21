"""Verified, resumable NAS archive control plane.

This module owns archive files and their PostgreSQL manifest records.  It does
not delete or attach live rows.  Destructive table rebuilds stay in separate,
explicit compaction phases after a verified backup and preflight approval.
"""

from __future__ import annotations

from datetime import datetime
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.database.migrations import HEAD_REVISION
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


ARCHIVE_KINDS = frozenset({"fundamental-history", "publications", "options", "derived"})
ARCHIVE_FREE_RESERVE_BYTES = 10 * 1024**3
_ARCHIVE_DIRS = {
    "fundamental-history": "fundamental-history",
    "publications": "publications",
    "options": "options",
    "derived": "derived",
}


class StorageArchiveService:
    """Own bounded archive write, verify, plan, and file restore operations."""

    def __init__(self, runtime: DatabaseRuntime, archive_root: Path) -> None:
        self.runtime = runtime
        self.archive_root = archive_root

    def plan(self) -> dict[str, Any]:
        """Return read-only capacity and candidate measurements."""

        with self.runtime.read(JOB_PROFILE) as connection:
            sizes = connection.execute(
                """
                SELECT relation, pg_total_relation_size(relation::regclass) AS bytes
                FROM unnest(ARRAY[
                    'raw.fundamental_observation', 'raw.quote_confirmation',
                    'raw.price_bar_confirmation', 'app.publication_item',
                    'raw.option_quote', 'analysis.option_relative_value'
                ]) AS relation
                WHERE to_regclass(relation) IS NOT NULL
                ORDER BY bytes DESC
                """
            ).fetchall()
            fundamental = connection.execute(
                """
                SELECT count(*) AS rows, coalesce(sum(pg_column_size(values)), 0) AS value_bytes
                FROM raw.fundamental_observation WHERE values ? 'history'
                """
            ).fetchone()
            manifests = connection.execute(
                """
                SELECT verification_status, count(*) AS count
                FROM ops.storage_archive_manifest GROUP BY verification_status
                """
            ).fetchall()
            checkpoints = connection.execute(
                """
                SELECT checkpoint_key, archive_kind, run_status, counts, error_detail, updated_at
                FROM ops.storage_archive_checkpoint ORDER BY updated_at DESC
                """
            ).fetchall()
        local = shutil.disk_usage(Path.cwd())
        nas = _disk_usage(self.archive_root)
        history_bytes = int(fundamental["value_bytes"] or 0)
        wal_allowance = max(1024**3, int(history_bytes * 0.20))
        retained_copy_estimate = max(1024**3, int(history_bytes * 0.15))
        return {
            "write": False,
            "archive_root": str(self.archive_root),
            "local_free_bytes": local.free,
            "nas_free_bytes": nas.free if nas else None,
            "required_staging_reserve_bytes": 10 * 1024**3,
            "wal_allowance_bytes": wal_allowance,
            "retained_copy_estimate_bytes": retained_copy_estimate,
            "required_local_staging_bytes": retained_copy_estimate + wal_allowance + 10 * 1024**3,
            "reclaim_estimate_bytes": {"fundamental_history_values_upper_bound": history_bytes},
            "tables": [dict(row) for row in sizes],
            "fundamental_history_candidates": dict(fundamental),
            "manifests": {str(row["verification_status"]): int(row["count"]) for row in manifests},
            "checkpoints": [dict(row) for row in checkpoints],
        }

    def archive_fundamental_history(self, *, batch_size: int = 500) -> dict[str, Any]:
        """Export a bounded batch of history arrays without changing live rows.

        The compressed object is content-addressed.  The phase deliberately
        stops before payload linking/table replacement; those need row-level
        validation and a separate preflight-approved compaction operation.
        """

        if batch_size < 1 or batch_size > 10_000:
            raise ValueError("batch_size must be between 1 and 10000")
        checkpoint_key = "fundamental-history-export-v1"
        self._repair_fundamental_references()
        cursor = self._checkpoint_cursor(checkpoint_key)
        mode = str(cursor.get("mode") or "append")
        cursor_key = "last_id" if mode == "append" else "audit_last_id"
        last_id = int(cursor.get(cursor_key) or 0)
        self._set_checkpoint(checkpoint_key, "fundamental-history", "raw.fundamental_observation", "running", cursor)
        written = 0
        deduplicated = 0
        rows: list[Any] = []
        try:
            with self.runtime.read(JOB_PROFILE) as connection:
                rows = connection.execute(
                    """
                    SELECT observation.id, observation.instrument_id, observation.source_id,
                           observation.ingest_run_id, observation.metric_set, observation.period_end,
                           observation.observed_at, observation.values -> 'history' AS history,
                           reference.manifest_id AS reference_manifest_id,
                           reference.source_ingest_run_id
                    FROM raw.fundamental_observation observation
                    LEFT JOIN ops.storage_archive_manifest_reference reference
                      ON reference.source_relation = 'raw.fundamental_observation'
                     AND reference.source_row_id = observation.id
                    WHERE observation.id > %s
                      AND (%s <> 'audit'
                           OR (reference.manifest_id IS NOT NULL
                               AND reference.source_ingest_run_id IS DISTINCT FROM observation.ingest_run_id))
                    ORDER BY observation.id LIMIT %s
                    """,
                    [last_id, mode, batch_size],
                ).fetchall()
            # First group source rows by their canonical JSON.  Large history
            # arrays are often identical across many observations.  A group
            # therefore needs one NAS/manifest operation and one batched
            # reference transaction, rather than two database transactions per
            # source row.  The cursor remains durable after the whole batch;
            # a process failure can only repeat idempotent writes.
            grouped: dict[str, list[Any]] = {}
            empty_reference_ids: list[int] = []
            for row in rows:
                # Scan by the primary key, rather than filtering on the large
                # JSONB value before ordering.  On a nearly full database the
                # latter can choose a full sort and spill several GiB to local
                # pg_temp before the first archive object is written.
                last_id = int(row["id"])
                history = row["history"]
                if history is None:
                    if row["reference_manifest_id"] is not None:
                        empty_reference_ids.append(int(row["id"]))
                    continue
                if mode == "audit" and row["source_ingest_run_id"] == row["ingest_run_id"]:
                    continue
                raw = json.dumps(history, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
                grouped.setdefault(sha256(raw).hexdigest(), []).append(row)

            references: list[tuple[int, int, Any]] = []
            for group in grouped.values():
                row = group[0]
                artifact = self._write_json_gzip(
                    "fundamental-history",
                    row["history"],
                    source_relation="raw.fundamental_observation",
                    row_count=len(row["history"]) if isinstance(row["history"], list) else 1,
                    metadata={
                        "observation_id": int(row["id"]),
                        "instrument_id": int(row["instrument_id"]),
                        "source_id": str(row["source_id"]),
                        "metric_set": str(row["metric_set"]),
                        "period_end": str(row["period_end"] or ""),
                        "observed_at": row["observed_at"].isoformat(),
                        "archive_phase": "export_only",
                    },
                    range_start=row["observed_at"],
                    range_end=row["observed_at"],
                )
                if artifact["created"]:
                    written += 1
                deduplicated += len(group) - int(artifact["created"])
                references.extend(
                    (int(artifact["manifest_id"]), int(group_row["id"]), group_row["ingest_run_id"])
                    for group_row in group
                )
            self._apply_fundamental_reference_batch(references, empty_reference_ids)
        except ArchiveCapacityError as exc:
            self._set_checkpoint(
                checkpoint_key,
                "fundamental-history",
                "raw.fundamental_observation",
                "paused",
                {**cursor, "mode": mode, cursor_key: last_id},
                counts={"batch_rows": len(rows), "artifacts_written": written, "deduplicated": deduplicated},
                error_detail=str(exc),
            )
            return {
                "phase": "fundamental-history",
                "status": "paused",
                "reason": "nas_free_space_below_reserve",
                "rows": len(rows),
                "written": written,
                "deduplicated": deduplicated,
            }
        except Exception as exc:
            self._set_checkpoint(
                checkpoint_key,
                "fundamental-history",
                "raw.fundamental_observation",
                "failed",
                {**cursor, "mode": mode, cursor_key: last_id},
                counts={"artifacts_written": written, "deduplicated": deduplicated},
                error_detail=f"{type(exc).__name__}: {exc}",
            )
            raise
        if mode == "append" and len(rows) < batch_size:
            status = "paused"
            next_cursor = {**cursor, "mode": "audit", "last_id": last_id, "audit_last_id": 0}
        elif mode == "audit" and len(rows) < batch_size:
            status = "succeeded"
            next_cursor = {**cursor, "mode": "audit", "last_id": cursor.get("last_id", last_id), "audit_last_id": 0}
        else:
            status = "paused"
            next_cursor = {**cursor, "mode": mode, cursor_key: last_id}
        self._set_checkpoint(
            checkpoint_key,
            "fundamental-history",
            "raw.fundamental_observation",
            status,
            next_cursor,
            counts={"batch_rows": len(rows), "artifacts_written": written, "deduplicated": deduplicated},
        )
        return {"phase": "fundamental-history", "status": status, "rows": len(rows), "written": written, "deduplicated": deduplicated}

    def verify(self, *, manifest_id: int | None = None) -> dict[str, Any]:
        with self.runtime.read(JOB_PROFILE) as connection:
            if manifest_id is None:
                rows = connection.execute("SELECT * FROM ops.storage_archive_manifest ORDER BY id").fetchall()
            else:
                rows = connection.execute("SELECT * FROM ops.storage_archive_manifest WHERE id = %s", [manifest_id]).fetchall()
        checked = verified = failed = 0
        for row in rows:
            checked += 1
            path = Path(str(row["nas_uri"]))
            ok, detail = self._verify_file(
                path,
                str(row["sha256"]),
                expected_row_count=int(row["row_count"]),
                schema_revision=str(row["schema_revision"]),
                metadata=dict(row["metadata"] or {}),
            )
            with self.runtime.transaction(JOB_PROFILE) as connection:
                connection.execute(
                    """
                    UPDATE ops.storage_archive_manifest
                    SET verification_status = %s, verified_at = CASE WHEN %s THEN now() ELSE NULL END,
                        metadata = metadata || %s, updated_at = now()
                    WHERE id = %s
                    """,
                    ["verified" if ok else "failed", ok, Jsonb({"verification_detail": detail}), row["id"]],
                )
            verified += int(ok)
            failed += int(not ok)
        return {"checked": checked, "verified": verified, "failed": failed}

    def restore_to_file(self, manifest_id: int, destination: Path) -> dict[str, Any]:
        """Restore one artifact to an empty staging file, never a live table."""

        if destination.exists():
            raise FileExistsError(f"restore destination already exists: {destination}")
        with self.runtime.read(JOB_PROFILE) as connection:
            row = connection.execute("SELECT * FROM ops.storage_archive_manifest WHERE id = %s", [manifest_id]).fetchone()
        if row is None:
            raise ValueError(f"unknown archive manifest {manifest_id}")
        if row["verification_status"] != "verified":
            raise ValueError("archive must verify before restore")
        source = Path(str(row["nas_uri"]))
        ok, detail = self._verify_file(
            source,
            str(row["sha256"]),
            expected_row_count=int(row["row_count"]),
            schema_revision=str(row["schema_revision"]),
            metadata=dict(row["metadata"] or {}),
        )
        if not ok:
            raise ValueError(f"archive verification failed: {detail}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(source, "rb") as input_file, destination.open("xb") as output_file:
            shutil.copyfileobj(input_file, output_file)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                "UPDATE ops.storage_archive_manifest SET verification_status = 'restored', updated_at = now() WHERE id = %s",
                [manifest_id],
            )
        return {"manifest_id": manifest_id, "destination": str(destination), "bytes": destination.stat().st_size}

    def health(self) -> dict[str, Any]:
        with self.runtime.read() as connection:
            table_rows = connection.execute(
                """
                SELECT relation, pg_total_relation_size(relation::regclass) AS bytes
                FROM unnest(ARRAY[
                    'raw.fundamental_observation', 'app.publication_item', 'raw.option_quote',
                    'analysis.option_relative_value'
                ]) AS relation
                WHERE to_regclass(relation) IS NOT NULL ORDER BY bytes DESC
                """
            ).fetchall()
            failures = connection.execute(
                "SELECT count(*) AS count FROM ops.storage_archive_manifest WHERE verification_status = 'failed'"
            ).fetchone()
            active = connection.execute(
                """
                SELECT checkpoint_key, archive_kind, source_relation, run_status, counts, error_detail, updated_at
                FROM ops.storage_archive_checkpoint WHERE run_status IN ('running', 'paused', 'failed')
                ORDER BY updated_at DESC
                """
            ).fetchall()
        local = shutil.disk_usage(Path.cwd())
        nas = _disk_usage(self.archive_root)
        # A seven-day linear estimate is intentionally withheld until daily
        # accounting samples exist; reporting null is safer than a fiction.
        return {
            "local": {"path": str(Path.cwd()), "free_bytes": local.free, "total_bytes": local.total},
            "nas": None if nas is None else {"path": str(self.archive_root), "free_bytes": nas.free, "total_bytes": nas.total},
            "table_sizes": [dict(row) for row in table_rows],
            "forecast_30d_bytes": None,
            "forecast_status": "pending_daily_accounting",
            "archive_verification_failures": int(failures["count"]),
            "active_reclamation": [dict(row) for row in active],
            "full_history_collection_allowed": local.free >= 30 * 1024**3,
        }

    def _write_json_gzip(
        self,
        archive_kind: str,
        payload: Any,
        *,
        source_relation: str,
        row_count: int,
        metadata: dict[str, Any],
        range_start: datetime | None = None,
        range_end: datetime | None = None,
    ) -> dict[str, Any]:
        if archive_kind not in ARCHIVE_KINDS:
            raise ValueError(f"unsupported archive kind: {archive_kind}")
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        content_hash = sha256(raw).hexdigest()
        root = self.archive_root / _ARCHIVE_DIRS[archive_kind] / content_hash[:2]
        _ensure_mounted_archive_root(self.archive_root)
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{content_hash}.json.gz"
        if not target.exists():
            self._require_archive_capacity(len(raw))
            fd, temp_name = tempfile.mkstemp(prefix=".archive-", suffix=".tmp", dir=root)
            try:
                with os.fdopen(fd, "wb") as file_handle:
                    with gzip.GzipFile(fileobj=file_handle, mode="wb", mtime=0) as compressed:
                        compressed.write(raw)
                    file_handle.flush()
                    os.fsync(file_handle.fileno())
                os.replace(temp_name, target)
            except BaseException:
                Path(temp_name).unlink(missing_ok=True)
                raise
        artifact_hash = _sha256_file(target)
        manifest_id, created = self._record_manifest(
            archive_kind=archive_kind,
            source_relation=source_relation,
            path=target,
            artifact_hash=artifact_hash,
            row_count=row_count,
            range_start=range_start,
            range_end=range_end,
            metadata={**metadata, "content_sha256": content_hash, "uncompressed_bytes": len(raw)},
        )
        return {
            "path": str(target),
            "sha256": artifact_hash,
            "manifest_id": manifest_id,
            "created": created,
        }

    def _record_manifest(
        self, *, archive_kind: str, source_relation: str, path: Path, artifact_hash: str,
        row_count: int, range_start: datetime | None, range_end: datetime | None, metadata: dict[str, Any],
    ) -> tuple[int, bool]:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                INSERT INTO ops.storage_archive_manifest
                    (archive_kind, source_relation, nas_uri, sha256, format, row_count,
                     range_start, range_end, schema_revision, verification_status, metadata)
                VALUES (%s, %s, %s, %s, 'json.gz', %s, %s, %s, %s, 'written', %s)
                ON CONFLICT (archive_kind, sha256) DO NOTHING
                RETURNING id
                """,
                [archive_kind, source_relation, str(path), artifact_hash, row_count,
                 range_start, range_end, HEAD_REVISION, Jsonb(metadata)],
            ).fetchone()
            created = row is not None
            if row is None:
                row = connection.execute(
                    "SELECT id FROM ops.storage_archive_manifest "
                    "WHERE archive_kind = %s AND sha256 = %s",
                    [archive_kind, artifact_hash],
                ).fetchone()
        if row is None:
            raise RuntimeError("archive manifest could not be resolved")
        return int(row["id"]), created

    def _record_source_reference(
        self,
        *,
        manifest_id: int,
        source_relation: str,
        source_row_id: int,
        source_ingest_run_id: Any,
    ) -> None:
        """Record every source row that shares a content-addressed artifact."""

        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """
                INSERT INTO ops.storage_archive_manifest_reference
                    (manifest_id, source_relation, source_row_id, source_ingest_run_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (source_relation, source_row_id) DO UPDATE
                SET manifest_id = EXCLUDED.manifest_id,
                    source_ingest_run_id = EXCLUDED.source_ingest_run_id,
                    created_at = now()
                """,
                [manifest_id, source_relation, source_row_id, source_ingest_run_id],
            )

    def _apply_fundamental_reference_batch(
        self, references: list[tuple[int, int, Any]], empty_reference_ids: list[int],
    ) -> None:
        """Atomically apply one archive batch's source-row lineage changes."""

        with self.runtime.transaction(JOB_PROFILE) as connection:
            if empty_reference_ids:
                connection.execute(
                    "DELETE FROM ops.storage_archive_manifest_reference "
                    "WHERE source_relation = 'raw.fundamental_observation' "
                    "AND source_row_id = ANY(%s)",
                    [empty_reference_ids],
                )
            if references:
                connection.cursor().executemany(
                    """
                    INSERT INTO ops.storage_archive_manifest_reference
                        (manifest_id, source_relation, source_row_id, source_ingest_run_id)
                    VALUES (%s, 'raw.fundamental_observation', %s, %s)
                    ON CONFLICT (source_relation, source_row_id) DO UPDATE
                    SET manifest_id = EXCLUDED.manifest_id,
                        source_ingest_run_id = EXCLUDED.source_ingest_run_id,
                        created_at = now()
                    """,
                    references,
                )

    def _remove_source_reference(self, *, source_relation: str, source_row_id: int) -> None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                "DELETE FROM ops.storage_archive_manifest_reference "
                "WHERE source_relation = %s AND source_row_id = %s",
                [source_relation, source_row_id],
            )

    def _repair_fundamental_references(self) -> None:
        """Add lineage for early artifacts written before reference tracking."""

        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """
                INSERT INTO ops.storage_archive_manifest_reference
                    (manifest_id, source_relation, source_row_id)
                SELECT id, source_relation, (metadata->>'observation_id')::bigint
                FROM ops.storage_archive_manifest
                WHERE archive_kind = 'fundamental-history'
                  AND source_relation = 'raw.fundamental_observation'
                  AND metadata ? 'observation_id'
                ON CONFLICT (source_relation, source_row_id) DO NOTHING
                """
            )

    def _require_archive_capacity(self, payload_bytes: int) -> None:
        usage = _disk_usage(self.archive_root)
        if usage is None:
            raise ArchiveCapacityError("archive filesystem capacity is unavailable")
        required = ARCHIVE_FREE_RESERVE_BYTES + payload_bytes
        if usage.free < required:
            raise ArchiveCapacityError(
                f"archive free space is below reserve: free={usage.free}, required={required}"
            )

    def _checkpoint_cursor(self, checkpoint_key: str) -> dict[str, Any]:
        with self.runtime.read(JOB_PROFILE) as connection:
            row = connection.execute(
                "SELECT cursor FROM ops.storage_archive_checkpoint WHERE checkpoint_key = %s", [checkpoint_key]
            ).fetchone()
        return dict(row["cursor"]) if row and row["cursor"] else {}

    def _set_checkpoint(
        self, checkpoint_key: str, archive_kind: str, source_relation: str, status: str,
        cursor: dict[str, Any], *, counts: dict[str, Any] | None = None, error_detail: str | None = None,
    ) -> None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """
                INSERT INTO ops.storage_archive_checkpoint
                    (checkpoint_key, archive_kind, source_relation, cursor, run_status, counts, error_detail, started_at, finished_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now(), CASE WHEN %s IN ('succeeded', 'failed') THEN now() END)
                ON CONFLICT (checkpoint_key) DO UPDATE SET
                    cursor = EXCLUDED.cursor, run_status = EXCLUDED.run_status, counts = EXCLUDED.counts,
                    error_detail = EXCLUDED.error_detail, updated_at = now(),
                    finished_at = CASE WHEN EXCLUDED.run_status IN ('succeeded', 'failed') THEN now() ELSE NULL END
                """,
                [checkpoint_key, archive_kind, source_relation, Jsonb(cursor), status, Jsonb(counts or {}), error_detail, status],
            )

    @staticmethod
    def _verify_file(
        path: Path, expected_sha256: str, *, expected_row_count: int,
        schema_revision: str, metadata: dict[str, Any],
    ) -> tuple[bool, str]:
        if not path.is_file():
            return False, "missing"
        try:
            with gzip.open(path, "rb") as handle:
                raw = handle.read()
            payload = json.loads(raw)
        except OSError as exc:
            return False, f"gzip_corrupt:{type(exc).__name__}"
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return False, f"json_corrupt:{type(exc).__name__}"
        actual = _sha256_file(path)
        if actual != expected_sha256:
            return False, "sha256_mismatch"
        if str(metadata.get("content_sha256") or "") != sha256(raw).hexdigest():
            return False, "content_sha256_mismatch"
        if (len(payload) if isinstance(payload, list) else 1) != expected_row_count:
            return False, "row_count_mismatch"
        if not schema_revision:
            return False, "schema_revision_missing"
        return True, "ok"


def _ensure_mounted_archive_root(root: Path) -> None:
    # Do not silently create a top-level path that normally represents an
    # unavailable NAS mount.  Once the known NAS mount is present, create only
    # the configured Market archive directory below it.
    nas_mount = Path("/Volumes/agent")
    try:
        on_market_nas = root.is_relative_to(nas_mount)
    except AttributeError:  # pragma: no cover - Python 3.11 is required.
        on_market_nas = str(root).startswith(f"{nas_mount}{os.sep}")
    if on_market_nas:
        if not nas_mount.is_dir() or not os.path.ismount(nas_mount):
            raise FileNotFoundError(f"archive mount is unavailable: {nas_mount}")
        root.mkdir(parents=True, exist_ok=True)
        return
    if not root.parent.exists():
        raise FileNotFoundError(f"archive parent is unavailable: {root.parent}")


ensure_mounted_archive_root = _ensure_mounted_archive_root


class ArchiveCapacityError(RuntimeError):
    """Archive writes must pause before they consume the NAS reserve."""


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _disk_usage(path: Path) -> Any | None:
    try:
        return shutil.disk_usage(path)
    except OSError:
        return None
