"""Verified, resumable NAS archive control plane.

This module owns archive files and their PostgreSQL manifest records.  It does
not delete or attach live rows.  Destructive table rebuilds stay in separate,
explicit compaction phases after a verified backup and preflight approval.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from investment_panel.core.decision import MARKET_TZ, is_us_market_day
from investment_panel.database.migrations import HEAD_REVISION
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


# Only these phases have a production archive writer.  Publication and
# derived detail is retained or recomputed locally; keeping them out of this
# set prevents the CLI from advertising a writer that does not exist.
ARCHIVE_KINDS = frozenset({"fundamental-history", "options"})
_JSON_ARCHIVE_KINDS = frozenset({"fundamental-history", "publications", "derived"})
ARCHIVE_FREE_RESERVE_BYTES = 10 * 1024**3
_ARCHIVE_DIRS = {
    "fundamental-history": "fundamental-history",
    "publications": "publications",
    "options": "options",
    "derived": "derived",
}
_OPTION_PARTITION_RE = re.compile(r"^option_quote_(\d{4})(\d{2})(\d{2})?$")
_BACKUP_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


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

    def archive_options(
        self,
        *,
        now: datetime | None = None,
        execute: bool = False,
        backup_token: str | None = None,
    ) -> dict[str, Any]:
        """Archive immutable option partitions and optionally detach them.

        The default is a read-only plan plus archive verification.  A
        partition is detached only after a custom dump, checksum, listing,
        row-count check, and scratch-database restore all pass.
        """

        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("archive reference time must be timezone-aware")
        cutoff = _complete_trading_day_cutoff(reference, 7)
        with self.runtime.read(JOB_PROFILE) as connection:
            partitions = connection.execute(
                """
                SELECT child.relname AS name,
                       pg_get_expr(child.relpartbound, child.oid) AS bounds,
                       pg_total_relation_size(child.oid) AS bytes
                FROM pg_partition_tree('raw.option_quote'::regclass) tree
                JOIN pg_class child ON child.oid = tree.relid
                JOIN pg_namespace namespace ON namespace.oid = child.relnamespace
                WHERE tree.isleaf
                  AND namespace.nspname = 'raw'
                  AND child.relname <> 'option_quote_default'
                ORDER BY child.relname
                """
            ).fetchall()

        candidates: list[dict[str, Any]] = []
        for row in partitions:
            name = str(row["name"])
            parsed = _option_partition_bounds(str(row["bounds"] or ""))
            if parsed is None or parsed[1] > cutoff.astimezone(UTC):
                continue
            candidates.append({
                "partition": name,
                "bounds": str(row["bounds"]),
                "range_start": parsed[0],
                "range_end": parsed[1],
                "bytes": int(row["bytes"] or 0),
            })
        if not candidates:
            return {
                "phase": "options",
                "status": "nothing_due",
                "cutoff": cutoff,
                "candidates": [],
                "detached": 0,
            }
        if execute:
            self._require_verified_backup(backup_token)
            self._assert_no_conflicting_activity()

        archived: list[dict[str, Any]] = []
        detached = 0
        for candidate in candidates:
            result = self._archive_option_partition(candidate)
            archived.append(result)
            if execute and result.get("verification_status") == "verified":
                with self.runtime.transaction(JOB_PROFILE) as connection:
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended('raw.option_quote.archive', 0))"
                    )
                    partition_name = str(candidate["partition"])
                    connection.execute(
                        f"ALTER TABLE raw.option_quote DETACH PARTITION raw.{_quote_ident(partition_name)}"
                    )
                    connection.execute(f"DROP TABLE raw.{_quote_ident(partition_name)}")
                self._update_manifest_metadata(
                    int(result["manifest_id"]), {"detached_at": datetime.now(UTC).isoformat()}
                )
                detached += 1
        return {
            "phase": "options",
            "status": "succeeded" if all(item.get("verification_status") == "verified" for item in archived) else "partial",
            "cutoff": cutoff,
            "candidates": archived,
            "detached": detached,
            "dry_run": not execute,
        }

    def compact_price_confirmations(
        self,
        *,
        state: str,
        batch_size: int = 10_000,
        backup_token: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Run one explicit resumable price-confirmation lifecycle state."""

        if state not in {"plan", "backfill", "verify", "cutover"}:
            raise ValueError("price-confirmations state must be plan, backfill, verify, or cutover")
        if batch_size < 1 or batch_size > 10_000:
            raise ValueError("batch_size must be between 1 and 10000")
        from investment_panel.database.price_confirmation_retention import PriceConfirmationRetentionRepository

        repository = PriceConfirmationRetentionRepository(self.runtime)
        if state in {"plan", "verify"}:
            coverage = repository.coverage()
            return {
                "phase": "price-confirmations",
                "state": state,
                "status": "ready" if coverage["complete"] else "backfill_required",
                "coverage": coverage,
            }
        if state == "backfill":
            results: dict[str, Any] = {}
            for table in ("price_bar", "quote"):
                key = f"storage.compact.price-confirmations/{table}"
                cursor = self._setting_json(key)
                result = repository.project_availability_batch(
                    table=table,
                    after_fact_id=int(cursor.get("after_fact_id") or 0),
                    after_available_at=cursor.get("after_available_at"),
                    fact_batch_size=batch_size,
                    dry_run=dry_run,
                )
                complete = int(result["fact_versions"] or 0) < batch_size
                next_cursor = {
                    "after_fact_id": result.get("next_after_fact_id") or cursor.get("after_fact_id", 0),
                    "after_available_at": _json_value(result.get("next_after_available_at")) or cursor.get("after_available_at"),
                    "complete": complete,
                }
                if not dry_run:
                    self._write_setting(key, next_cursor)
                results[table] = {**result, "complete": complete, "cursor": next_cursor}
            return {"phase": "price-confirmations", "state": state, "status": "partial", "tables": results}
        # Cutover is the only state that can drop and recreate staging tables.
        coverage = repository.coverage()
        if not coverage["complete"]:
            raise RuntimeError("price-confirmations cutover requires 100% eligible projection coverage")
        if dry_run:
            return {"phase": "price-confirmations", "state": state, "status": "dry_run", "coverage": coverage}
        self._require_verified_backup(backup_token)
        self._assert_no_conflicting_activity()
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('raw.price-confirmations.cutover', 0))")
            for table in ("price_bar", "quote"):
                relation = f"raw.{table}_confirmation"
                connection.execute(f"DROP TABLE IF EXISTS {relation}")
                connection.execute(
                    f"""
                    CREATE TABLE {relation} (
                        fact_id BIGINT NOT NULL,
                        fact_available_at TIMESTAMPTZ NOT NULL,
                        ingest_run_id UUID NOT NULL REFERENCES ingest.run(id),
                        confirmed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                        PRIMARY KEY (fact_id, fact_available_at, ingest_run_id)
                    )
                    """
                )
                trigger_name = f"{table}_confirmation_projection"
                connection.execute(
                    f"""
                    CREATE TRIGGER {trigger_name}
                    AFTER INSERT ON {relation}
                    FOR EACH ROW EXECUTE FUNCTION raw.project_confirmation_staging()
                    """
                )
            connection.execute(
                """
                INSERT INTO app.setting (key, value, updated_at)
                VALUES ('storage.price-confirmations.authoritative', '{"enabled": true}'::jsonb, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """
            )
        return {
            "phase": "price-confirmations",
            "state": state,
            "status": "succeeded",
            "coverage": coverage,
            "staging": "recreated_empty",
        }

    def expire_option_archives(
        self, *, now: datetime | None = None, execute: bool = False
    ) -> dict[str, Any]:
        """Report, then explicitly remove option objects beyond 730 days."""

        reference = now or datetime.now(UTC)
        cutoff = reference - timedelta(days=730)
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT id, nas_uri, range_end, verification_status
                FROM ops.storage_archive_manifest
                WHERE archive_kind = 'options'
                  AND verification_status IN ('verified', 'restored')
                  AND range_end < %s
                ORDER BY range_end, id
                """,
                [cutoff],
            ).fetchall()
        candidates = [
            {"manifest_id": int(row["id"]), "path": str(row["nas_uri"]), "range_end": row["range_end"]}
            for row in rows
        ]
        if not execute:
            return {"phase": "options", "status": "dry_run", "eligible": candidates, "cutoff": cutoff}
        removed = 0
        for candidate in candidates:
            path = Path(candidate["path"])
            if path.exists():
                path.unlink()
            with self.runtime.transaction(JOB_PROFILE) as connection:
                connection.execute(
                    "UPDATE ops.storage_archive_manifest SET verification_status = 'expired', metadata = metadata || %s, updated_at = now() WHERE id = %s",
                    [Jsonb({"expired_at": reference.isoformat(), "retention_cutoff": cutoff.isoformat()}), candidate["manifest_id"]],
                )
            removed += 1
        return {"phase": "options", "status": "succeeded", "removed": removed, "cutoff": cutoff}

    def _archive_option_partition(self, candidate: dict[str, Any]) -> dict[str, Any]:
        name = str(candidate["partition"])
        with self.runtime.read(JOB_PROFILE) as connection:
            row = connection.execute(
                f"""
                SELECT count(*) AS row_count,
                       min(observed_at) AS min_observed_at,
                       max(observed_at) AS max_observed_at
                FROM raw.{_quote_ident(name)}
                """
            ).fetchone()
            existing = connection.execute(
                """
                SELECT id, nas_uri, sha256, verification_status, metadata
                FROM ops.storage_archive_manifest
                WHERE archive_kind = 'options' AND source_relation = %s
                ORDER BY id DESC LIMIT 1
                """,
                [f"raw.{name}"],
            ).fetchone()
        if existing and str(existing["verification_status"]) == "verified" and Path(str(existing["nas_uri"])).is_file():
            return {
                "partition": name,
                "manifest_id": int(existing["id"]),
                "path": str(existing["nas_uri"]),
                "verification_status": "verified",
                "row_count": int(row["row_count"] or 0),
                "reused": True,
            }
        root = self.archive_root / "options"
        _ensure_mounted_archive_root(self.archive_root)
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{name}-{HEAD_REVISION}.dump"
        self._require_archive_capacity(max(int(candidate["bytes"]), 1))
        if not path.exists():
            subprocess.run(
                [_binary("pg_dump"), "--format=custom", "--compress=9", "--no-owner", "--no-acl",
                 "--file", str(path), "--dbname", self.runtime.dsn, "--table", f"raw.{name}"],
                check=True, capture_output=True, text=True,
            )
        listing = subprocess.run(
            [_binary("pg_restore"), "--list", str(path)], check=True, capture_output=True, text=True
        ).stdout
        listing_hash = sha256(listing.encode("utf-8")).hexdigest()
        dump_hash = _sha256_file(path)
        metadata = {
            "partition": name,
            "bounds": candidate["bounds"],
            "dump_listing_sha256": listing_hash,
            "byte_count": path.stat().st_size,
            "min_observed_at": _json_value(row["min_observed_at"]),
            "max_observed_at": _json_value(row["max_observed_at"]),
            "restore_verified": False,
        }
        manifest_id, _ = self._record_native_manifest(
            source_relation=f"raw.{name}", path=path, artifact_hash=dump_hash,
            row_count=int(row["row_count"] or 0), range_start=candidate["range_start"],
            range_end=candidate["range_end"], metadata=metadata,
        )
        ok, detail = self._verify_native_dump(
            path, dump_hash, int(row["row_count"] or 0), listing_hash, scratch=True,
            relation_name=name,
        )
        self._update_manifest_metadata(
            manifest_id, {"restore_verified": ok, "verification_detail": detail}
        )
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """
                UPDATE ops.storage_archive_manifest
                SET verification_status = %s, verified_at = CASE WHEN %s THEN now() ELSE NULL END,
                    updated_at = now()
                WHERE id = %s
                """,
                ["verified" if ok else "failed", ok, manifest_id],
            )
        return {
            "partition": name,
            "manifest_id": manifest_id,
            "path": str(path),
            "verification_status": "verified" if ok else "failed",
            "verification_detail": detail,
            "row_count": int(row["row_count"] or 0),
            "sha256": dump_hash,
        }

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
            metadata = dict(row["metadata"] or {})
            if str(row["format"]) == "custom" and str(row["archive_kind"]) == "options":
                listing_hash = str(metadata.get("dump_listing_sha256") or "")
                ok, detail = self._verify_native_dump(
                    path, str(row["sha256"]), int(row["row_count"]), listing_hash, scratch=True,
                    relation_name=str(metadata.get("partition") or "") or None,
                )
            else:
                ok, detail = self._verify_file(
                    path,
                    str(row["sha256"]),
                    expected_row_count=int(row["row_count"]),
                    schema_revision=str(row["schema_revision"]),
                    metadata=metadata,
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
        metadata = dict(row["metadata"] or {})
        if str(row["format"]) == "custom":
            ok, detail = self._verify_native_dump(
                source, str(row["sha256"]), int(row["row_count"]),
                str(metadata.get("dump_listing_sha256") or ""), scratch=False,
                relation_name=str(metadata.get("partition") or "") or None,
            )
        else:
            ok, detail = self._verify_file(
                source,
                str(row["sha256"]),
                expected_row_count=int(row["row_count"]),
                schema_revision=str(row["schema_revision"]),
                metadata=metadata,
            )
        if not ok:
            raise ValueError(f"archive verification failed: {detail}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if str(row["format"]) == "custom":
            shutil.copyfile(source, destination)
        else:
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
            retention = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM analysis.run WHERE started_at < now() - interval '30 days') AS analysis_runs,
                    (SELECT count(*) FROM ops.job_run
                     WHERE (status IN ('succeeded', 'skipped') AND started_at < now() - interval '7 days')
                        OR (status IN ('partial', 'failed') AND started_at < now() - interval '30 days')) AS job_runs,
                    (SELECT count(*) FROM raw.price_bar_confirmation) AS price_confirmation_staging,
                    (SELECT count(*) FROM raw.quote_confirmation) AS quote_confirmation_staging
                """
            ).fetchone()
            partitions = connection.execute(
                """
                SELECT child.relname AS name,
                       pg_get_expr(child.relpartbound, child.oid) AS bounds
                FROM pg_partition_tree('raw.option_quote'::regclass) tree
                JOIN pg_class child ON child.oid = tree.relid
                JOIN pg_namespace namespace ON namespace.oid = child.relnamespace
                WHERE tree.isleaf
                  AND namespace.nspname = 'raw'
                  AND child.relname <> 'option_quote_default'
                """
            ).fetchall()
        local = shutil.disk_usage(Path.cwd())
        nas = _disk_usage(self.archive_root)
        reference = datetime.now(UTC)
        archive_cutoff = _complete_trading_day_cutoff(reference, 7).astimezone(UTC)
        parsed_partitions = [
            parsed
            for row in partitions
            if (parsed := _option_partition_bounds(str(row["bounds"] or ""))) is not None
        ]
        hot_partitions = [parsed for parsed in parsed_partitions if parsed[1] > archive_cutoff]
        archive_candidates = [parsed for parsed in parsed_partitions if parsed[1] <= archive_cutoff]
        hot_age_days = (
            max(0.0, (reference - min(start for start, _ in hot_partitions)).total_seconds() / 86400)
            if hot_partitions else None
        )
        archive_lag_seconds = (
            int(max(0.0, (reference - min(end for _, end in archive_candidates)).total_seconds()))
            if archive_candidates else 0
        )
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
            "archive_lag_seconds": archive_lag_seconds,
            "hot_partition_age_days": hot_age_days,
            "retention_backlog": {**dict(retention), "option_archive_candidates": len(archive_candidates)},
            "projected_free_space_bytes": local.free,
        }

    def _setting_json(self, key: str) -> dict[str, Any]:
        with self.runtime.read(JOB_PROFILE) as connection:
            row = connection.execute("SELECT value FROM app.setting WHERE key = %s", [key]).fetchone()
        return dict(row["value"] or {}) if row else {}

    def _write_setting(self, key: str, value: dict[str, Any]) -> None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """
                INSERT INTO app.setting (key, value, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                [key, Jsonb(value)],
            )

    def _require_verified_backup(self, token: str | None) -> dict[str, Any]:
        if not token or not _BACKUP_SHA_RE.fullmatch(token.lower()):
            raise ValueError("a verified PostgreSQL backup SHA-256 token is required")
        backup_root = self.archive_root.parent.parent / "postgres-backups"
        for manifest_path in sorted(backup_root.glob("*.json"), reverse=True):
            try:
                manifest = json.loads(manifest_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if (
                str(manifest.get("status")) == "verified"
                and str(manifest.get("sha256", "")).lower() == token.lower()
                and Path(str(manifest.get("dump_path", ""))).is_file()
            ):
                return manifest
        raise ValueError("backup token does not identify a verified NAS PostgreSQL backup")

    def _assert_no_conflicting_activity(self) -> None:
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT pid, usename, application_name, state, query
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  AND state <> 'idle'
                  AND query NOT ILIKE '%pg_stat_activity%'
                """
            ).fetchall()
        if rows:
            descriptions = ", ".join(f"{row['pid']}:{row['application_name'] or row['usename']}" for row in rows[:5])
            raise RuntimeError(f"destructive storage cutover requires no active PostgreSQL activity: {descriptions}")

    def _record_native_manifest(
        self,
        *,
        source_relation: str,
        path: Path,
        artifact_hash: str,
        row_count: int,
        range_start: datetime,
        range_end: datetime,
        metadata: dict[str, Any],
    ) -> tuple[int, bool]:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                INSERT INTO ops.storage_archive_manifest
                    (archive_kind, source_relation, nas_uri, sha256, format, row_count,
                     range_start, range_end, schema_revision, verification_status, metadata)
                VALUES ('options', %s, %s, %s, 'custom', %s, %s, %s, %s, 'written', %s)
                ON CONFLICT (archive_kind, sha256) DO NOTHING
                RETURNING id
                """,
                [source_relation, str(path), artifact_hash, row_count, range_start,
                 range_end, HEAD_REVISION, Jsonb(metadata)],
            ).fetchone()
            created = row is not None
            if row is None:
                row = connection.execute(
                    "SELECT id FROM ops.storage_archive_manifest WHERE archive_kind = 'options' AND sha256 = %s",
                    [artifact_hash],
                ).fetchone()
        if row is None:
            raise RuntimeError("native archive manifest could not be resolved")
        return int(row["id"]), created

    def _update_manifest_metadata(self, manifest_id: int, values: dict[str, Any]) -> None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                "UPDATE ops.storage_archive_manifest SET metadata = metadata || %s, updated_at = now() WHERE id = %s",
                [Jsonb(values), manifest_id],
            )

    def _verify_native_dump(
        self,
        path: Path,
        expected_sha256: str,
        expected_row_count: int,
        expected_listing_sha256: str,
        *,
        scratch: bool,
        relation_name: str | None = None,
    ) -> tuple[bool, str]:
        if not path.is_file():
            return False, "missing"
        if _sha256_file(path) != expected_sha256:
            return False, "sha256_mismatch"
        try:
            listing = subprocess.run(
                [_binary("pg_restore"), "--list", str(path)],
                check=True, capture_output=True, text=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            return False, f"pg_restore_list_failed:{type(exc).__name__}"
        if sha256(listing.encode("utf-8")).hexdigest() != expected_listing_sha256:
            return False, "dump_listing_sha256_mismatch"
        if relation_name:
            with self.runtime.read(JOB_PROFILE) as connection:
                relation = connection.execute("SELECT to_regclass(%s) AS relation", [f"raw.{relation_name}"]).fetchone()
                if relation and relation["relation"] is not None:
                    row = connection.execute(
                        f"SELECT count(*) AS count FROM raw.{_quote_ident(relation_name)}"
                    ).fetchone()
                    if int(row["count"] or 0) != expected_row_count:
                        return False, "row_count_mismatch"
        if not scratch:
            return True, "ok"
        database_name = f"market_archive_verify_{uuid4().hex[:16]}"
        try:
            subprocess.run(
                [_binary("createdb"), "--maintenance-db", self.runtime.dsn, database_name],
                check=True, capture_output=True, text=True,
            )
            if relation_name and relation_name.startswith("option_quote_"):
                subprocess.run(
                    [_binary("psql"), "--dbname", database_name, "-v", "ON_ERROR_STOP=1",
                     "-c", "CREATE SCHEMA IF NOT EXISTS raw"],
                    check=True, capture_output=True, text=True,
                )
                parent_schema = subprocess.run(
                    [_binary("pg_dump"), "--schema-only", "--section=pre-data",
                     "--no-owner", "--no-acl", "--table", "raw.option_quote",
                     "--dbname", self.runtime.dsn],
                    check=True, capture_output=True, text=True,
                )
                subprocess.run(
                    [_binary("psql"), "--dbname", database_name, "-v", "ON_ERROR_STOP=1"],
                    input=parent_schema.stdout, check=True, capture_output=True, text=True,
                )
            subprocess.run(
                [_binary("pg_restore"), "--dbname", database_name, "--section=pre-data",
                 "--section=data", "--no-owner", "--no-acl",
                 "--exit-on-error", str(path)],
                check=True, capture_output=True, text=True,
            )
            if relation_name:
                counted = subprocess.run(
                    [_binary("psql"), "--dbname", database_name, "-Atc",
                     f"SELECT count(*) FROM raw.{_quote_ident(relation_name)}"],
                    check=True, capture_output=True, text=True,
                )
                if int(counted.stdout.strip() or "-1") != expected_row_count:
                    return False, "scratch_row_count_mismatch"
            return True, f"scratch_restore_ok:{expected_row_count}"
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = str(exc)
            if isinstance(exc, subprocess.CalledProcessError):
                detail = (exc.stderr or exc.stdout or detail).strip().splitlines()[-1]
            return False, f"scratch_restore_failed:{detail[:240]}"
        finally:
            subprocess.run(
                [_binary("dropdb"), "--if-exists", "--maintenance-db", self.runtime.dsn, database_name],
                check=False, capture_output=True, text=True,
            )

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
        if archive_kind not in _JSON_ARCHIVE_KINDS:
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


def _binary(name: str) -> str:
    return shutil.which(name) or f"/opt/homebrew/opt/postgresql@18/bin/{name}"


def _quote_ident(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", value):
        raise ValueError(f"unsafe PostgreSQL identifier: {value}")
    return value


def _option_partition_bounds(bounds: str) -> tuple[datetime, datetime] | None:
    match = re.search(r"FROM \('([^']+)'\) TO \('([^']+)'\)", bounds)
    if match is None:
        # PostgreSQL may omit the parenthesis around a single range literal in
        # a future display format.  Keep the parser strict and fail closed.
        match = re.search(r"FROM '([^']+)' TO '([^']+)'", bounds)
    if match is None:
        return None
    values: list[datetime] = []
    for raw in match.groups():
        try:
            value = datetime.fromisoformat(raw.replace(" ", "T"))
        except ValueError:
            return None
        values.append(value if value.tzinfo else value.replace(tzinfo=UTC))
    return values[0], values[1]


def _complete_trading_day_cutoff(reference: datetime, trading_days: int) -> datetime:
    local = reference.astimezone(MARKET_TZ)
    cursor = local.date()
    remaining = trading_days
    if is_us_market_day(cursor) and local.hour >= 16:
        remaining -= 1
    while remaining > 0:
        cursor -= timedelta(days=1)
        if is_us_market_day(cursor):
            remaining -= 1
    return datetime.combine(cursor, datetime.min.time(), tzinfo=MARKET_TZ)


def _json_value(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value
