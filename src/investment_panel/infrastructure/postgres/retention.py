"""Retention policies for bounded PostgreSQL operational and option storage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import re
from typing import Any

import psycopg

from investment_panel.domain.decision import is_us_market_day
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.infrastructure.postgres.publication_archive import PublicationArchive
from investment_panel.infrastructure.postgres.hot_retention import HotRetention, MAINTENANCE_PROFILE
from investment_panel.infrastructure.postgres.row_archive import MAX_PACK_BYTES, RowArchive


OPTION_PARTITION_RE = re.compile(r"^option_quote_(\d{4})(\d{2})(\d{2})?$")
ROLLING_PUBLICATION_SCOPES = ("today", "options-radar", "options-decision-system")
MARKET_PUBLICATION_SUPERSEDED_LIMIT = 48
ROLLING_PUBLICATION_TRADING_DAYS = 7
PUBLICATION_PAYLOAD_CLEANUP_BATCH_SIZE = 500


class RetentionRepository:
    def __init__(self, runtime: DatabaseRuntime, *, archive_root: Path | None = None) -> None:
        self.runtime = runtime
        configured = archive_root or os.environ.get("MARKET_STORAGE_ARCHIVE_DIR")
        self.archive = PublicationArchive(runtime, Path(configured)) if configured else None

    def archive_publications(self, *, batch_size: int = 10, execute: bool = False) -> dict[str, Any]:
        """Plan or export eligible publications without deleting their rows."""
        if not 1 <= batch_size <= 1000:
            raise ValueError("publication archive batch_size must be between 1 and 1000")
        reference = datetime.now(UTC)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            ids = _publication_candidates(connection, standard_cutoff=reference - timedelta(days=7),
                rolling_cutoff=_trading_day_cutoff(reference, ROLLING_PUBLICATION_TRADING_DAYS), limit=batch_size)
            if execute and ids:
                if self.archive is None:
                    raise ValueError("publication archival requires a configured NAS archive root")
                ids = self.archive.publications(connection, ids)
            return {"phase": "publications", "candidates": len(ids), "deleted": 0, "dry_run": not execute}

    def prune(
        self, *, now: datetime | None = None, option_days: int = 7,
        analysis_days: int = 30, publication_days: int = 7, job_days: int = 30,
        publication_batch_size: int = 1, dry_run: bool = False,
        vacuum_analyze: bool = False,
    ) -> dict[str, int]:
        """Small independently committed batches; no derived-history CASCADE.

        Missing NAS never authorizes source deletion. A partial pass can be
        rerun safely; the scheduler records failures instead of hiding them.
        """
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None or min(option_days, analysis_days, publication_days, job_days) < 1:
            raise ValueError("retention requires an aware time and positive windows")
        counts = self.prune_publications(now=reference, batch_size=publication_batch_size,
            dry_run=dry_run, vacuum_analyze=vacuum_analyze, publication_days=publication_days)
        if dry_run:
            return counts
        counts.update({"analysis_runs": 0, "option_quotes": 0, "option_snapshots": 0})
        if self.archive is not None:
            hot = HotRetention(self.archive.service).run(now=reference, execute=True,
                max_batches=20, option_days=option_days, analysis_days=analysis_days)
            for name in ("option_quotes", "option_provider_payloads", "relative_values"):
                if hot[name]:
                    counts[name] = hot[name]
            counts["analysis_runs"] = self._prune_empty_runs(reference - timedelta(days=analysis_days))
        else:
            counts["hot_archive_required"] = 1
        with self.runtime.transaction(MAINTENANCE_PROFILE) as connection:
            counts["job_runs"] = connection.execute("""
                WITH expired AS (SELECT id FROM ops.job_run
                    WHERE (status IN ('succeeded', 'skipped') AND started_at < %s)
                       OR (status IN ('partial', 'failed') AND started_at < %s)
                    ORDER BY started_at, id LIMIT 1000 FOR UPDATE SKIP LOCKED)
                DELETE FROM ops.job_run job USING expired WHERE job.id = expired.id
            """, [reference - timedelta(days=min(7, job_days)), reference - timedelta(days=job_days)]).rowcount
        counts["option_partitions"] = self.drop_empty_option_partitions(before=reference - timedelta(days=option_days))
        return counts

    def _prune_empty_runs(self, before: datetime) -> int:
        """Archive empty metadata only. Discover *all* incoming FKs, not a list
        of selected protections that accidentally permits CASCADE elsewhere.
        """
        with self.runtime.transaction(MAINTENANCE_PROFILE) as connection:
            # Some child evidence is deliberately unreadable by market_app.
            # The fixed-search-path helper exposes only empty parent IDs/sizes.
            rows = connection.execute(
                "SELECT * FROM analysis.empty_run_metadata_candidates(%s)", [before],
            ).fetchall()
            if not rows:
                return 0
            selected, consumed = [], 0
            for row in rows:
                if consumed + int(row["bytes"]) > MAX_PACK_BYTES // 2:
                    if not selected:
                        raise ValueError("empty run metadata exceeds bounded archive budget")
                    break
                selected.append(row["id"])
                consumed += int(row["bytes"])
            records = connection.execute("SELECT to_jsonb(run)::text AS row_json FROM analysis.run run WHERE id = ANY(%s) ORDER BY id", [selected]).fetchall()
            RowArchive(self.archive.service).write(connection, "analysis.run", records)
            # Row locks exclude concurrent insertion of FK references. Still
            # recheck on a fresh statement after waiting for these locks.
            return int(connection.execute(
                "SELECT analysis.prune_empty_run_metadata(%s::uuid[], %s) AS deleted", [selected, before],
            ).fetchone()["deleted"])

    def prune_publications(
        self,
        *,
        now: datetime | None = None,
        batch_size: int = 25,
        publication_days: int = 7,
        dry_run: bool = False,
        vacuum_analyze: bool = False,
    ) -> dict[str, int]:
        """Prune only superseded publications in a small, restart-safe batch.

        This operational entrypoint separates the large historical publication
        repair from unrelated source-retention rules.  It is safe to repeat;
        each call selects at most ``batch_size`` current candidates.
        """

        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("retention reference time must be timezone-aware")
        if not 1 <= batch_size <= 100 or publication_days < 1:
            raise ValueError("publication batch size must be 1..100 and days positive")
        rolling_cutoff = _trading_day_cutoff(reference, ROLLING_PUBLICATION_TRADING_DAYS)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            candidates = _publication_candidates(
                connection,
                standard_cutoff=reference - timedelta(days=publication_days),
                rolling_cutoff=rolling_cutoff,
                limit=None if dry_run else batch_size,
            )
            count = len(candidates)
            compact_counts = {}
            if not dry_run and candidates:
                if self.archive is None:
                    compact_counts["publication_archive_required"] = count
                    count = 0
                else:
                    candidates = self.archive.publications(connection, candidates)
                    count = len(candidates)
                    compact_counts = _delete_publications_and_orphaned_content(connection, candidates)
            if not dry_run:
                orphan_payloads = _delete_orphaned_payload_batch(connection, self.archive)
                if orphan_payloads:
                    compact_counts["publication_payloads"] = compact_counts.get("publication_payloads", 0) + orphan_payloads
        result = {"publications": count, **compact_counts}
        if dry_run:
            result["publication_dry_run"] = count
        elif vacuum_analyze and count:
            result["publication_vacuum_tables"] = self.vacuum_analyze_publications()
        return result

    def vacuum_analyze_publications(self) -> int:
        """Reclaim planner statistics after a batched publication delete.

        This intentionally uses normal VACUUM ANALYZE, never VACUUM FULL.  The
        latter would take an exclusive lock and is not valid for this runtime.
        """

        with psycopg.connect(self.runtime.dsn, autocommit=True) as connection:
            connection.execute("VACUUM (ANALYZE) app.publication")
            connection.execute("VACUUM (ANALYZE) app.publication_item")
            connection.execute("VACUUM (ANALYZE) app.publication_bundle")
            connection.execute("VACUUM (ANALYZE) app.publication_bundle_item")
            connection.execute("VACUUM (ANALYZE) app.publication_payload")
            connection.execute("VACUUM (ANALYZE) app.current_publication_item")
        return 6

    def drop_empty_option_partitions(self, *, before: datetime) -> int:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('raw.option_quote.partition', 0))")
            partitions = connection.execute(
                """
                SELECT child.relname
                FROM pg_inherits inheritance
                JOIN pg_class parent ON parent.oid = inheritance.inhparent
                JOIN pg_namespace parent_namespace ON parent_namespace.oid = parent.relnamespace
                JOIN pg_class child ON child.oid = inheritance.inhrelid
                WHERE parent_namespace.nspname = 'raw' AND parent.relname = 'option_quote'
                """
            ).fetchall()
            dropped = 0
            for row in partitions:
                name = str(row["relname"])
                match = OPTION_PARTITION_RE.match(name)
                if match is None:
                    continue
                year, month, day = match.groups()
                partition_start = datetime(
                    int(year), int(month), int(day or 1), tzinfo=UTC
                )
                partition_cutoff = before if day else before.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                if partition_start >= partition_cutoff:
                    continue
                dropped += int(connection.execute(
                    "SELECT raw.detach_option_quote_partition(%s, true) AS detached", [name]
                ).fetchone()["detached"])
        return dropped


def _publication_candidates(
    connection: Any,
    *,
    standard_cutoff: datetime,
    rolling_cutoff: datetime,
    limit: int | None,
) -> list[Any]:
    """Return superseded generations eligible for one bounded retention pass."""

    suffix = "" if limit is None else "LIMIT %s"
    parameters: list[Any] = [
        MARKET_PUBLICATION_SUPERSEDED_LIMIT,
        list(ROLLING_PUBLICATION_SCOPES),
        rolling_cutoff,
        ["market", *ROLLING_PUBLICATION_SCOPES],
        standard_cutoff,
    ]
    if limit is not None:
        parameters.append(limit)
    rows = connection.execute(
        f"""
        WITH ranked AS (
            SELECT id,
                   scope,
                   coalesce(published_at, created_at) AS generation_at,
                   row_number() OVER (
                       PARTITION BY scope
                       ORDER BY published_at DESC NULLS LAST, created_at DESC, id DESC
                   ) AS superseded_rank
            FROM app.publication
            WHERE status = 'superseded'
              AND scope NOT IN ('ticker-opportunity-ranking', 'ticker-outcome-attribution')
        )
        SELECT id
        FROM ranked
        WHERE (
               (scope = 'market' AND superseded_rank > %s)
            OR (scope = ANY(%s) AND generation_at < %s)
            OR (scope <> ALL(%s) AND generation_at < %s)
        )
          AND NOT EXISTS (
              SELECT 1
              FROM analysis.ticker_decision decision
              WHERE decision.market_state_publication_id = ranked.id
          )
        ORDER BY generation_at, id
        {suffix}
        """,
        parameters,
    ).fetchall()
    return [row["id"] for row in rows]


def _delete_publications_and_orphaned_content(connection: Any, candidates: list[Any]) -> dict[str, int]:
    """Delete a bounded publication batch and its now-unreferenced compact content."""

    deleted = connection.execute(
        "DELETE FROM app.publication WHERE id = ANY(%s) RETURNING bundle_id",
        [candidates],
    ).fetchall()
    bundle_ids = sorted({row["bundle_id"] for row in deleted if row["bundle_id"] is not None})
    if not bundle_ids:
        return {}
    content_hashes = [
        row["content_hash"]
        for row in connection.execute(
            """
            SELECT content_hash
            FROM app.publication_bundle_item
            WHERE bundle_id = ANY(%s)
            """,
            [bundle_ids],
        ).fetchall()
    ]
    bundles = connection.execute(
        """
        DELETE FROM app.publication_bundle bundle
        WHERE bundle.id = ANY(%s)
          AND NOT EXISTS (SELECT 1 FROM app.publication publication WHERE publication.bundle_id = bundle.id)
        """,
        [bundle_ids],
    ).rowcount
    result: dict[str, int] = {"publication_bundles": bundles} if bundles else {}
    payloads = _delete_payload_hashes(connection, content_hashes)
    if payloads:
        result["publication_payloads"] = payloads
    return result


def _delete_payload_hashes(connection: Any, content_hashes: list[Any]) -> int:
    deleted = 0
    for start in range(0, len(content_hashes), PUBLICATION_PAYLOAD_CLEANUP_BATCH_SIZE):
        batch = content_hashes[start : start + PUBLICATION_PAYLOAD_CLEANUP_BATCH_SIZE]
        deleted += int(connection.execute(
            """
            DELETE FROM app.publication_payload payload
            WHERE payload.content_hash = ANY(%s)
              AND NOT EXISTS (
                SELECT 1 FROM app.publication_bundle_item item
                WHERE item.content_hash = payload.content_hash
              )
              AND NOT EXISTS (
                SELECT 1 FROM app.current_publication_item item
                WHERE item.content_hash = payload.content_hash
              )
            """,
            [batch],
        ).rowcount)
    return deleted


def _delete_orphaned_payload_batch(connection: Any, archive: PublicationArchive | None = None) -> int:
    """Delete one ordered orphan batch so repeated retention calls make progress."""

    if archive is None:
        return 0
    rows = connection.execute(
        """
        SELECT payload.content_hash
        FROM app.publication_payload payload
        WHERE NOT EXISTS (
            SELECT 1 FROM app.publication_bundle_item item
            WHERE item.content_hash = payload.content_hash
        )
          AND NOT EXISTS (
            SELECT 1 FROM app.current_publication_item item
            WHERE item.content_hash = payload.content_hash
        )
        ORDER BY payload.content_hash
        LIMIT %s
        """,
        [PUBLICATION_PAYLOAD_CLEANUP_BATCH_SIZE],
    ).fetchall()
    if not rows:
        return 0
    hashes = [row["content_hash"] for row in rows]
    archive.rows(connection, "app.publication_payload", "source.content_hash = ANY(%s)", [hashes])
    return _delete_payload_hashes(connection, hashes)


def _trading_day_cutoff(reference: datetime, trading_days: int) -> datetime:
    if trading_days < 0:
        raise ValueError("trading day retention must not be negative")
    remaining = trading_days
    current = reference.date()
    while remaining:
        current -= timedelta(days=1)
        if is_us_market_day(current):
            remaining -= 1
    return datetime.combine(current, datetime.min.time(), tzinfo=reference.tzinfo)
