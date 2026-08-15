"""Retention policies for bounded PostgreSQL operational and option storage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from typing import Any

import psycopg
from psycopg import sql

from investment_panel.core.decision import is_us_market_day
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


OPTION_PARTITION_RE = re.compile(r"^option_quote_(\d{4})(\d{2})$")
ROLLING_PUBLICATION_SCOPES = ("today", "options-radar", "options-decision-system")
MARKET_PUBLICATION_SUPERSEDED_LIMIT = 48
ROLLING_PUBLICATION_TRADING_DAYS = 30


class RetentionRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def prune(
        self,
        *,
        now: datetime | None = None,
        option_days: int = 7,
        analysis_days: int = 365,
        publication_days: int = 90,
        job_days: int = 30,
        publication_batch_size: int = 1_000,
        dry_run: bool = False,
        vacuum_analyze: bool = False,
    ) -> dict[str, int]:
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("retention reference time must be timezone-aware")
        if publication_batch_size < 1:
            raise ValueError("publication batch size must be positive")
        cutoffs = {
            "option": reference - timedelta(days=option_days),
            "history": reference - timedelta(days=730),
            "event_strip": reference - timedelta(days=365),
            "history_payload": reference - timedelta(days=90),
            "event_payload": reference - timedelta(days=30),
            "event_derived": reference - timedelta(days=730),
            "analysis": reference - timedelta(days=analysis_days),
            "publication": reference - timedelta(days=publication_days),
            "job": reference - timedelta(days=job_days),
        }
        counts: dict[str, int] = {}
        rolling_publication_cutoff = _trading_day_cutoff(reference, ROLLING_PUBLICATION_TRADING_DAYS)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            publication_candidates = _publication_candidates(
                connection,
                standard_cutoff=cutoffs["publication"],
                rolling_cutoff=rolling_publication_cutoff,
                limit=None if dry_run else publication_batch_size,
            )
            counts["publications"] = len(publication_candidates)
            if not dry_run and publication_candidates:
                counts.update(_delete_publications_and_orphaned_content(connection, publication_candidates))
            if dry_run:
                counts["publication_dry_run"] = len(publication_candidates)
                return counts
            counts["analysis_runs"] = connection.execute(
                """
                DELETE FROM analysis.run run
                WHERE run.started_at < %s
                  AND NOT EXISTS (SELECT 1 FROM app.publication publication WHERE publication.analysis_run_id = run.id)
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis.decision decision
                      JOIN analysis.option_outcome outcome ON outcome.decision_id = decision.id
                      WHERE decision.run_id = run.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis.decision decision
                      JOIN analysis.shadow_trade trade ON trade.decision_id = decision.id
                      WHERE decision.run_id = run.id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis.decision decision
                      JOIN app.trade_journal journal ON journal.decision_id = decision.id
                      WHERE decision.run_id = run.id
                  )
                """,
                [cutoffs["analysis"]],
            ).rowcount
            counts["option_quotes"] = connection.execute(
                """
                DELETE FROM raw.option_quote quote
                USING raw.option_snapshot snapshot
                WHERE snapshot.id = quote.snapshot_id
                  AND quote.observed_at < CASE
                        WHEN snapshot.collection_profile = 'history_full' THEN %s
                        WHEN snapshot.collection_profile = 'event_strip' THEN %s
                        ELSE %s END
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis.option_feature feature
                      WHERE feature.snapshot_id = quote.snapshot_id
                        AND feature.contract_id = quote.contract_id
                        AND feature.quote_observed_at = quote.observed_at
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis.option_decision decision
                      WHERE decision.snapshot_id = quote.snapshot_id
                        AND decision.contract_id = quote.contract_id
                        AND decision.quote_observed_at = quote.observed_at
                  )
                """,
                [cutoffs["history"], cutoffs["event_strip"], cutoffs["option"]],
            ).rowcount
            provider_payloads = connection.execute(
                """
                UPDATE raw.option_quote quote
                SET provider_payload = '{}'::jsonb
                FROM raw.option_snapshot snapshot
                WHERE snapshot.id = quote.snapshot_id
                  AND quote.provider_payload <> '{}'::jsonb
                  AND quote.observed_at < CASE
                        WHEN snapshot.collection_profile = 'history_full' THEN %s
                        WHEN snapshot.collection_profile = 'event_strip' THEN %s
                        ELSE %s END
                """,
                [cutoffs["history_payload"], cutoffs["event_payload"], cutoffs["option"]],
            ).rowcount
            if provider_payloads:
                counts["option_provider_payloads"] = provider_payloads
            counts["option_snapshots"] = connection.execute(
                """
                DELETE FROM raw.option_snapshot snapshot
                WHERE snapshot.observed_at < CASE
                        WHEN snapshot.collection_profile = 'history_full' THEN %s
                        WHEN snapshot.collection_profile = 'event_strip' THEN %s
                        ELSE %s END
                  AND NOT EXISTS (SELECT 1 FROM raw.option_quote quote WHERE quote.snapshot_id = snapshot.id)
                """,
                [cutoffs["history"], cutoffs["event_strip"], cutoffs["option"]],
            ).rowcount
            closed_events = connection.execute(
                """
                DELETE FROM analysis.option_event event
                WHERE event.status = 'closed'
                  AND event.closed_at < %s
                """,
                [cutoffs["event_derived"]],
            ).rowcount
            if closed_events:
                counts["closed_option_events"] = closed_events
            counts["job_runs"] = connection.execute(
                "DELETE FROM ops.job_run WHERE status <> 'running' AND started_at < %s",
                [cutoffs["job"]],
            ).rowcount
        counts["option_partitions"] = self.drop_empty_option_partitions(before=cutoffs["option"])
        if vacuum_analyze and counts["publications"]:
            counts["publication_vacuum_tables"] = self.vacuum_analyze_publications()
        return counts

    def prune_publications(
        self,
        *,
        now: datetime | None = None,
        batch_size: int = 1_000,
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
        if batch_size < 1:
            raise ValueError("publication batch size must be positive")
        rolling_cutoff = _trading_day_cutoff(reference, ROLLING_PUBLICATION_TRADING_DAYS)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            candidates = _publication_candidates(
                connection,
                standard_cutoff=reference - timedelta(days=90),
                rolling_cutoff=rolling_cutoff,
                limit=None if dry_run else batch_size,
            )
            count = len(candidates)
            if not dry_run and candidates:
                compact_counts = _delete_publications_and_orphaned_content(connection, candidates)
            else:
                compact_counts = {}
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
                year, month = map(int, match.groups())
                partition_start = datetime(year, month, 1, tzinfo=UTC)
                if partition_start >= before.replace(day=1, hour=0, minute=0, second=0, microsecond=0):
                    continue
                has_rows = connection.execute(
                    sql.SQL("SELECT EXISTS (SELECT 1 FROM raw.{} LIMIT 1) AS has_rows").format(sql.Identifier(name))
                ).fetchone()["has_rows"]
                if not has_rows:
                    connection.execute(
                        sql.SQL("ALTER TABLE raw.option_quote DETACH PARTITION raw.{}").format(sql.Identifier(name))
                    )
                    connection.execute(sql.SQL("DROP TABLE raw.{}").format(sql.Identifier(name)))
                    dropped += 1
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
        )
        SELECT id
        FROM ranked
        WHERE (scope = 'market' AND superseded_rank > %s)
           OR (scope = ANY(%s) AND generation_at < %s)
           OR (scope <> ALL(%s) AND generation_at < %s)
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
    bundles = connection.execute(
        """
        DELETE FROM app.publication_bundle bundle
        WHERE bundle.id = ANY(%s)
          AND NOT EXISTS (SELECT 1 FROM app.publication publication WHERE publication.bundle_id = bundle.id)
        """,
        [bundle_ids],
    ).rowcount
    if not bundles:
        return {}
    payloads = connection.execute(
        """
        DELETE FROM app.publication_payload payload
        WHERE NOT EXISTS (
            SELECT 1 FROM app.publication_bundle_item item WHERE item.content_hash = payload.content_hash
        )
        """
    ).rowcount
    result: dict[str, int] = {"publication_bundles": bundles}
    if payloads:
        result["publication_payloads"] = payloads
    return result


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
