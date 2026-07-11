"""Retention policies for bounded PostgreSQL operational and option storage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from typing import Any

from psycopg import sql

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


OPTION_PARTITION_RE = re.compile(r"^option_quote_(\d{4})(\d{2})$")


class RetentionRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def prune(
        self,
        *,
        now: datetime | None = None,
        option_days: int = 120,
        analysis_days: int = 365,
        publication_days: int = 90,
        job_days: int = 30,
    ) -> dict[str, int]:
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("retention reference time must be timezone-aware")
        cutoffs = {
            "option": reference - timedelta(days=option_days),
            "analysis": reference - timedelta(days=analysis_days),
            "publication": reference - timedelta(days=publication_days),
            "job": reference - timedelta(days=job_days),
        }
        counts: dict[str, int] = {}
        with self.runtime.transaction(JOB_PROFILE) as connection:
            counts["publications"] = connection.execute(
                "DELETE FROM app.publication WHERE status = 'superseded' AND created_at < %s",
                [cutoffs["publication"]],
            ).rowcount
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
                WHERE quote.observed_at < %s
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
                [cutoffs["option"]],
            ).rowcount
            counts["option_snapshots"] = connection.execute(
                """
                DELETE FROM raw.option_snapshot snapshot
                WHERE snapshot.observed_at < %s
                  AND NOT EXISTS (SELECT 1 FROM raw.option_quote quote WHERE quote.snapshot_id = snapshot.id)
                """,
                [cutoffs["option"]],
            ).rowcount
            counts["job_runs"] = connection.execute(
                "DELETE FROM ops.job_run WHERE status <> 'running' AND started_at < %s",
                [cutoffs["job"]],
            ).rowcount
        counts["option_partitions"] = self.drop_empty_option_partitions(before=cutoffs["option"])
        return counts

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
