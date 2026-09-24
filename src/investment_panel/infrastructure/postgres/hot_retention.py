"""Bounded NAS-first lifecycle for raw option envelopes and disposable RV rows.

A checkpoint and its source mutations share a transaction. Archive objects may
outlive a rolled-back transaction, and are reverified on reuse. Cursor scans
advance past protected records, so a protected prefix cannot starve cleanup.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.infrastructure.postgres.row_archive import MAX_PACK_BYTES, RowArchive
from investment_panel.infrastructure.postgres.runtime import RuntimeProfile
from investment_panel.infrastructure.postgres.storage_archive import StorageArchiveService

MAINTENANCE_PROFILE = RuntimeProfile(statement_timeout_ms=30_000, lock_timeout_ms=2_000, jit=False)
# Every exact option feature/decision stays hot. Candidate verifications also
# consume provider-specific instrument IDs, so preserve their original envelope.
QUOTE_PIN = """
    EXISTS (SELECT 1 FROM analysis.option_feature f WHERE f.snapshot_id = q.snapshot_id
            AND f.contract_id = q.contract_id AND f.quote_observed_at = q.observed_at)
    OR EXISTS (SELECT 1 FROM analysis.option_decision d WHERE d.snapshot_id = q.snapshot_id
            AND d.contract_id = q.contract_id AND d.quote_observed_at = q.observed_at)
    OR EXISTS (SELECT 1 FROM analysis.option_relative_value r
            WHERE r.capture_generation_id = q.capture_generation_id AND r.contract_id = q.contract_id
              AND r.classification IN ('historical_static_arbitrage_candidate', 'verified_static_arbitrage_candidate'))
"""
RV_PIN = """
    r.classification IN ('historical_static_arbitrage_candidate', 'verified_static_arbitrage_candidate')
    OR EXISTS (SELECT 1 FROM analysis.option_decision d WHERE d.relative_value_id = r.id)
    OR EXISTS (SELECT 1 FROM analysis.option_relative_value_verification v WHERE v.relative_value_id = r.id)
    OR EXISTS (SELECT 1 FROM app.publication p WHERE p.analysis_run_id = r.analysis_run_id)
    OR EXISTS (SELECT 1 FROM analysis.strategy_evaluation e WHERE e.run_id = r.analysis_run_id)
"""


class HotRetention:
    def __init__(self, service: StorageArchiveService) -> None:
        self.service = service
        self.archive = RowArchive(service)

    def run(self, *, phase: str = "all", now: datetime | None = None,
            batch_size: int = 500, max_batches: int = 1, execute: bool = False,
            option_days: int = 7, analysis_days: int = 30, time_budget_seconds: int = 60) -> dict[str, Any]:
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("hot retention time must be timezone-aware")
        if phase not in {"all", "options", "relative-values"}:
            raise ValueError("unknown hot retention phase")
        if not 1 <= batch_size <= 1000 or not 1 <= max_batches <= 10000:
            raise ValueError("batch_size must be 1..1000 and max_batches 1..10000")
        if option_days < 1 or analysis_days < 1 or not 1 <= time_budget_seconds <= 3600:
            raise ValueError("retention windows and time budget must be positive and bounded")
        phases = ("options", "relative-values") if phase == "all" else (phase,)
        result: dict[str, Any] = {"dry_run": not execute, "option_quotes": 0,
            "option_provider_payloads": 0, "relative_values": 0, "scanned": 0, "batches": 0}
        if not execute:
            result["checkpoints"] = {p: self.service._checkpoint_cursor(f"hot-{p}-v1") for p in phases}
            result["policy"] = {"raw_envelope_days": option_days, "relative_value_days": analysis_days,
                                "history_typed_days": 730, "event_typed_days": 365,
                                "latest_capture": "always_retained", "max_batches_per_phase": max_batches,
                                "source_batch_bytes": MAX_PACK_BYTES // 2}
            return result
        started = monotonic()
        with self.service.runtime.job_lock("hot-storage-lifecycle") as acquired:
            if not acquired:
                return {**result, "status": "already_running"}
            active = set(phases)
            for _ in range(max_batches):
                # Alternate phases so a quote backlog cannot monopolize every
                # scheduled pass and starve derived-history cleanup.
                for part in phases:
                    if part not in active:
                        continue
                    if monotonic() - started >= time_budget_seconds:
                        return {**result, "status": "time_budget_reached"}
                    key = f"hot-{part}-v1"
                    try:
                        with self.service.runtime.transaction(MAINTENANCE_PROFILE) as connection:
                            saved = connection.execute("SELECT cursor FROM ops.storage_archive_checkpoint WHERE checkpoint_key = %s", [key]).fetchone()
                            cursor = dict(saved["cursor"]) if saved else {}
                            if part == "options":
                                counts, next_cursor, done = self._options(connection, cursor, reference, batch_size, option_days)
                            else:
                                counts, next_cursor, done = self._relative_values(connection, cursor, reference, batch_size, analysis_days)
                            self._checkpoint(connection, key, part, next_cursor, counts, done)
                    except Exception as exc:
                        # The source transaction has rolled back. Mark failure
                        # using ONLY its previously committed cursor.
                        try:
                            self.service._set_checkpoint(key, "derived",
                                "raw.option_quote" if part == "options" else "analysis.option_relative_value",
                                "failed", self.service._checkpoint_cursor(key), error_detail=f"{type(exc).__name__}: {exc}"[:2000])
                        except Exception:
                            pass  # Preserve the original error if the database is unavailable too.
                        raise
                    for name, count in counts.items():
                        result[name] += count
                    result["batches"] += 1
                    if done:
                        active.remove(part)
                if not active:
                    break
        return {**result, "status": "batch_complete"}

    def _checkpoint(self, connection: Any, key: str, phase: str, cursor: dict[str, Any], counts: dict[str, int], done: bool) -> None:
        connection.execute("""
            INSERT INTO ops.storage_archive_checkpoint
                (checkpoint_key, archive_kind, source_relation, cursor, run_status, counts, error_detail, updated_at)
            VALUES (%s, 'derived', %s, %s, %s, %s, NULL, now())
            ON CONFLICT (checkpoint_key) DO UPDATE SET cursor = EXCLUDED.cursor,
                run_status = EXCLUDED.run_status, counts = EXCLUDED.counts, error_detail = NULL, updated_at = now()
        """, [key, "raw.option_quote" if phase == "options" else "analysis.option_relative_value",
              Jsonb(cursor), "succeeded" if done else "paused", Jsonb(counts)])

    def _options(self, connection: Any, cursor: dict[str, Any], now: datetime, limit: int, days: int):
        cutoff = now - timedelta(days=days)
        snapshot_id = int(cursor.get("snapshot_id") or 0)
        complete = bool(cursor.get("complete"))
        snapshot = connection.execute("""
            SELECT s.id, s.collection_profile, s.observed_at,
                EXISTS (SELECT 1 FROM raw.option_snapshot newer
                        WHERE newer.source_id = s.source_id AND newer.collection_profile = s.collection_profile
                          AND newer.history_symbol IS NOT DISTINCT FROM s.history_symbol
                          AND newer.universe = s.universe
                          AND (newer.observed_at, newer.id) > (s.observed_at, s.id)
                          AND newer.capture_state = 'complete') AS superseded
            FROM raw.option_snapshot s
            WHERE s.id >= %s AND (s.id > %s OR NOT %s)
              AND s.observed_at < %s
            ORDER BY s.id LIMIT 1 FOR UPDATE OF s
        """, [snapshot_id, snapshot_id, complete, cutoff]).fetchone()
        empty = {"option_quotes": 0, "option_provider_payloads": 0, "scanned": 0}
        if not snapshot:
            return empty, {}, True
        sid = snapshot["id"]
        end_cursor = {"snapshot_id": sid, "complete": True}
        if not snapshot["superseded"]:
            return empty, end_cursor, False
        same = sid == snapshot_id and not complete
        last_contract = int(cursor.get("contract_id") or 0) if same else 0
        last_at = datetime.fromisoformat(cursor["observed_at"]) if same and cursor.get("observed_at") else datetime.min.replace(tzinfo=UTC)
        candidates = connection.execute(f"""
            SELECT q.contract_id, q.observed_at, q.capture_generation_id, ({QUOTE_PIN}) AS pinned,
                   octet_length(to_jsonb(q)::text) AS bytes,
                   q.provider_payload <> '{{}}'::jsonb AS has_payload
            FROM raw.option_quote q WHERE q.snapshot_id = %s
                AND q.observed_at < %s AND (q.contract_id, q.observed_at) > (%s, %s)
            ORDER BY q.contract_id, q.observed_at LIMIT %s FOR UPDATE OF q
        """, [sid, cutoff, last_contract, last_at, limit]).fetchall()
        if not candidates:
            return empty, end_cursor, False
        generation_ids = sorted({row["capture_generation_id"] for row in candidates if row["capture_generation_id"] is not None})
        if generation_ids:
            connection.execute("""
                SELECT id FROM raw.option_capture_generation
                WHERE id = ANY(%s) ORDER BY id FOR UPDATE
            """, [generation_ids]).fetchall()
        counts = dict(empty)
        keys, delete_keys, trim_keys = [], [], []
        budget = 0
        horizon = 730 if snapshot["collection_profile"] == "history_full" else 365 if snapshot["collection_profile"] == "event_strip" else days
        next_cursor = end_cursor
        for row in candidates:
            eligible = not row["pinned"] and (row["observed_at"] < now - timedelta(days=horizon) or row["has_payload"])
            if eligible and budget + int(row["bytes"]) > MAX_PACK_BYTES // 2:
                if not keys:
                    raise ValueError("individual quote exceeds the bounded archive budget")
                break
            counts["scanned"] += 1
            next_cursor = {"snapshot_id": sid, "contract_id": row["contract_id"], "observed_at": row["observed_at"].isoformat(), "complete": False}
            if not eligible:
                continue
            keys.append((row["contract_id"], row["observed_at"]))
            budget += int(row["bytes"])
            (delete_keys if row["observed_at"] < now - timedelta(days=horizon) else trim_keys).append((row["contract_id"], row["observed_at"]))
        if keys:
            records = connection.execute("""SELECT to_jsonb(q)::text AS row_json FROM raw.option_quote q
                WHERE snapshot_id = %s AND (contract_id, observed_at) IN (SELECT * FROM unnest(%s::bigint[], %s::timestamptz[])) ORDER BY contract_id, observed_at""",
                [sid, [key[0] for key in keys], [key[1] for key in keys]]).fetchall()
            self.archive.write(connection, "raw.option_quote", records)
            # Recheck references after potentially slow NAS I/O. Foreign-key
            # references through snapshot are serialized by its FOR UPDATE lock.
            if delete_keys:
                counts["option_quotes"] = connection.execute(f"""DELETE FROM raw.option_quote q
                    WHERE snapshot_id = %s AND (contract_id, observed_at) IN (SELECT * FROM unnest(%s::bigint[], %s::timestamptz[]))
                      AND NOT ({QUOTE_PIN})""", [sid, [key[0] for key in delete_keys], [key[1] for key in delete_keys]]).rowcount
            if trim_keys:
                counts["option_provider_payloads"] = connection.execute(f"""UPDATE raw.option_quote q
                    SET provider_payload = '{{}}'::jsonb
                    WHERE snapshot_id = %s AND (contract_id, observed_at) IN (SELECT * FROM unnest(%s::bigint[], %s::timestamptz[]))
                      AND NOT ({QUOTE_PIN})""", [sid, [key[0] for key in trim_keys], [key[1] for key in trim_keys]]).rowcount
        return counts, next_cursor, False

    def _relative_values(self, connection: Any, cursor: dict[str, Any], now: datetime, limit: int, days: int):
        rows = connection.execute(f"""
            SELECT r.id, r.analysis_run_id, r.created_at, ({RV_PIN}) AS pinned,
                   octet_length(to_jsonb(r)::text) AS bytes
            FROM analysis.option_relative_value r WHERE r.id > %s
            ORDER BY r.id LIMIT %s FOR UPDATE OF r
        """, [int(cursor.get("last_id") or 0), limit]).fetchall()
        counts = {"relative_values": 0, "scanned": 0}
        if not rows:
            return counts, {}, True
        ids, run_ids, budget, last_id = [], set(), 0, int(cursor.get("last_id") or 0)
        for row in rows:
            eligible = not row["pinned"] and row["created_at"] < now - timedelta(days=days)
            if eligible and budget + int(row["bytes"]) > MAX_PACK_BYTES // 2:
                if not ids:
                    raise ValueError("individual relative-value row exceeds the archive budget")
                break
            last_id = row["id"]
            counts["scanned"] += 1
            if eligible:
                ids.append(last_id)
                run_ids.add(row["analysis_run_id"])
                budget += int(row["bytes"])
        if ids:
            records = connection.execute("SELECT to_jsonb(r)::text AS row_json FROM analysis.option_relative_value r WHERE id = ANY(%s) ORDER BY id", [ids]).fetchall()
            self.archive.write(connection, "analysis.option_relative_value", records)
            connection.execute("""
                SELECT id FROM analysis.run WHERE id = ANY(%s)
                ORDER BY id FOR UPDATE
            """, [sorted(run_ids)]).fetchall()
            counts["relative_values"] = connection.execute(f"DELETE FROM analysis.option_relative_value r WHERE id = ANY(%s) AND NOT ({RV_PIN})", [ids]).rowcount
        return counts, {"last_id": last_id}, False
