"""Bounded cleanup for repeated successful price confirmations."""

from __future__ import annotations

from typing import Any, Literal

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


ConfirmationTable = Literal["price_bar", "quote"]


class PriceConfirmationRetentionRepository:
    """Delete redundant successful confirmations without touching failed audit rows."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def compact(
        self,
        *,
        table: ConfirmationTable,
        after_fact_id: int = 0,
        fact_batch_size: int = 1_000,
        dry_run: bool = False,
    ) -> dict[str, int | None]:
        if table not in {"price_bar", "quote"}:
            raise ValueError("confirmation table is invalid")
        if after_fact_id < 0 or fact_batch_size < 1 or fact_batch_size > 10_000:
            raise ValueError("confirmation cleanup bounds are invalid")
        relation = f"raw.{table}_confirmation"
        with self.runtime.transaction(JOB_PROFILE) as connection:
            candidates = connection.execute(
                f"""
                SELECT confirmation.fact_id, confirmation.fact_available_at
                FROM {relation} confirmation
                JOIN ingest.run run ON run.id = confirmation.ingest_run_id
                WHERE confirmation.fact_id > %s
                  AND run.status IN ('succeeded', 'partial')
                GROUP BY confirmation.fact_id, confirmation.fact_available_at
                HAVING count(*) > 1
                ORDER BY confirmation.fact_id, confirmation.fact_available_at
                LIMIT %s
                """,
                [after_fact_id, fact_batch_size],
            ).fetchall()
            values = [(int(row["fact_id"]), row["fact_available_at"]) for row in candidates]
            deleted = self._compact_pairs(connection, relation, values, dry_run=dry_run)
        return {
            "fact_versions": len(values),
            "deleted": int(deleted),
            "next_after_fact_id": max((fact_id for fact_id, _ in values), default=None),
        }

    def compact_for_instruments(
        self,
        *,
        table: ConfirmationTable,
        instrument_ids: list[int],
        fact_batch_size: int = 1_000,
        dry_run: bool = False,
    ) -> dict[str, int | None]:
        """Compact confirmation versions needed by a bounded live price set."""

        if table not in {"price_bar", "quote"}:
            raise ValueError("confirmation table is invalid")
        normalized = sorted({int(value) for value in instrument_ids if int(value) > 0})
        if not normalized or fact_batch_size < 1 or fact_batch_size > 10_000:
            raise ValueError("confirmation cleanup bounds are invalid")
        relation = f"raw.{table}_confirmation"
        fact_relation = f"raw.{table}"
        history_relation = f"raw.{table}_history"
        with self.runtime.transaction(JOB_PROFILE) as connection:
            candidates = connection.execute(
                f"""
                WITH facts AS (
                    SELECT id AS fact_id, available_at
                    FROM {fact_relation}
                    WHERE instrument_id = ANY(%s)
                    UNION
                    SELECT id AS fact_id, available_at
                    FROM {history_relation}
                    WHERE instrument_id = ANY(%s)
                )
                SELECT confirmation.fact_id, confirmation.fact_available_at
                FROM {relation} confirmation
                JOIN facts ON facts.fact_id = confirmation.fact_id
                  AND facts.available_at = confirmation.fact_available_at
                JOIN ingest.run run ON run.id = confirmation.ingest_run_id
                WHERE run.status IN ('succeeded', 'partial')
                GROUP BY confirmation.fact_id, confirmation.fact_available_at
                HAVING count(*) > 1
                ORDER BY confirmation.fact_id, confirmation.fact_available_at
                LIMIT %s
                """,
                [normalized, normalized, fact_batch_size],
            ).fetchall()
            values = [(int(row["fact_id"]), row["fact_available_at"]) for row in candidates]
            deleted = self._compact_pairs(connection, relation, values, dry_run=dry_run)
        return {
            "fact_versions": len(values),
            "deleted": int(deleted),
            "next_after_fact_id": max((fact_id for fact_id, _ in values), default=None),
        }

    def project_availability_for_instruments(
        self,
        *,
        table: ConfirmationTable,
        instrument_ids: list[int],
        after_fact_id: int = 0,
        after_available_at: Any | None = None,
        fact_batch_size: int = 1_000,
        dry_run: bool = False,
    ) -> dict[str, int | None]:
        """Project one bounded live price set without scanning audit history.

        The projection retains the earliest successful run for each fact
        version.  A later retry cannot move historical availability forward.
        """

        if table not in {"price_bar", "quote"}:
            raise ValueError("confirmation table is invalid")
        normalized = sorted({int(value) for value in instrument_ids if int(value) > 0})
        if not normalized or after_fact_id < 0 or fact_batch_size < 1 or fact_batch_size > 10_000:
            raise ValueError("availability projection bounds are invalid")
        fact_relation = f"raw.{table}"
        history_relation = f"raw.{table}_history"
        with self.runtime.transaction(JOB_PROFILE) as connection:
            candidates = connection.execute(
                f"""
                SELECT fact_id, fact_available_at
                FROM (
                    SELECT id AS fact_id, available_at AS fact_available_at
                    FROM {fact_relation}
                    WHERE instrument_id = ANY(%s)
                    UNION
                    SELECT id AS fact_id, available_at AS fact_available_at
                    FROM {history_relation}
                    WHERE instrument_id = ANY(%s)
                ) facts
                WHERE (fact_id, fact_available_at) > (
                    %s::bigint,
                    coalesce(%s::timestamptz, '-infinity'::timestamptz)
                )
                ORDER BY fact_id, fact_available_at
                LIMIT %s
                """,
                [normalized, normalized, after_fact_id, after_available_at, fact_batch_size],
            ).fetchall()
            values = [(int(row["fact_id"]), row["fact_available_at"]) for row in candidates]
            projected = self._project_pairs(connection, table, values, dry_run=dry_run)
        return {
            "fact_versions": len(values),
            "projected": int(projected),
            "next_after_fact_id": values[-1][0] if values else None,
            "next_after_available_at": values[-1][1] if values else None,
        }

    @staticmethod
    def _compact_pairs(
        connection: Any,
        relation: str,
        values: list[tuple[int, Any]],
        *,
        dry_run: bool,
    ) -> int:
        if not values:
            return 0
        fact_ids = [fact_id for fact_id, _ in values]
        available_ats = [available_at for _, available_at in values]
        if dry_run:
            return int(connection.execute(
                f"""
                WITH target AS (
                    SELECT *
                    FROM unnest(%s::bigint[], %s::timestamptz[])
                         AS value(fact_id, fact_available_at)
                )
                SELECT count(*) AS count
                FROM (
                    SELECT row_number() OVER (
                               PARTITION BY confirmation.fact_id, confirmation.fact_available_at
                               ORDER BY run.finished_at, confirmation.confirmed_at,
                                        confirmation.ingest_run_id
                           ) AS position
                    FROM {relation} confirmation
                    JOIN target ON target.fact_id = confirmation.fact_id
                      AND target.fact_available_at = confirmation.fact_available_at
                    JOIN ingest.run run ON run.id = confirmation.ingest_run_id
                    WHERE run.status IN ('succeeded', 'partial')
                ) ranked
                WHERE position > 1
                """,
                [fact_ids, available_ats],
            ).fetchone()["count"])
        return int(connection.execute(
            f"""
            WITH target AS (
                SELECT *
                FROM unnest(%s::bigint[], %s::timestamptz[])
                     AS value(fact_id, fact_available_at)
            ), ranked AS (
                SELECT confirmation.ctid,
                       row_number() OVER (
                           PARTITION BY confirmation.fact_id, confirmation.fact_available_at
                           ORDER BY run.finished_at, confirmation.confirmed_at,
                                    confirmation.ingest_run_id
                       ) AS position
                FROM {relation} confirmation
                JOIN target ON target.fact_id = confirmation.fact_id
                  AND target.fact_available_at = confirmation.fact_available_at
                JOIN ingest.run run ON run.id = confirmation.ingest_run_id
                WHERE run.status IN ('succeeded', 'partial')
            )
            DELETE FROM {relation} confirmation
            USING ranked
            WHERE confirmation.ctid = ranked.ctid
              AND ranked.position > 1
            """,
            [fact_ids, available_ats],
        ).rowcount)

    @staticmethod
    def _project_pairs(
        connection: Any,
        table: ConfirmationTable,
        values: list[tuple[int, Any]],
        *,
        dry_run: bool,
    ) -> int:
        if not values:
            return 0
        fact_ids = [fact_id for fact_id, _ in values]
        available_ats = [available_at for _, available_at in values]
        confirmation_relation = f"raw.{table}_confirmation"
        projection_relation = f"raw.{table}_fact_availability"
        if dry_run:
            return int(connection.execute(
                f"""
                WITH target AS (
                    SELECT *
                    FROM unnest(%s::bigint[], %s::timestamptz[])
                         AS value(fact_id, fact_available_at)
                )
                SELECT count(*) AS count
                FROM target
                JOIN {confirmation_relation} confirmation
                  ON confirmation.fact_id = target.fact_id
                 AND confirmation.fact_available_at = target.fact_available_at
                JOIN ingest.run run ON run.id = confirmation.ingest_run_id
                WHERE run.status IN ('succeeded', 'partial')
                  AND run.finished_at IS NOT NULL
                """,
                [fact_ids, available_ats],
            ).fetchone()["count"])
        return int(connection.execute(
            f"""
            WITH target AS (
                SELECT *
                FROM unnest(%s::bigint[], %s::timestamptz[])
                     AS value(fact_id, fact_available_at)
            ), first_finished AS (
                SELECT confirmation.fact_id,
                       confirmation.fact_available_at,
                       min(run.finished_at) AS finished_at
                FROM {confirmation_relation} confirmation
                JOIN target ON target.fact_id = confirmation.fact_id
                  AND target.fact_available_at = confirmation.fact_available_at
                JOIN ingest.run run ON run.id = confirmation.ingest_run_id
                WHERE run.status IN ('succeeded', 'partial')
                  AND run.finished_at IS NOT NULL
                GROUP BY confirmation.fact_id, confirmation.fact_available_at
            ), first_run AS (
                SELECT first_finished.fact_id,
                       first_finished.fact_available_at,
                       min(confirmation.ingest_run_id::text)::uuid AS ingest_run_id
                FROM first_finished
                JOIN {confirmation_relation} confirmation
                  ON confirmation.fact_id = first_finished.fact_id
                 AND confirmation.fact_available_at = first_finished.fact_available_at
                JOIN ingest.run run ON run.id = confirmation.ingest_run_id
                 AND run.finished_at = first_finished.finished_at
                WHERE run.status IN ('succeeded', 'partial')
                GROUP BY first_finished.fact_id, first_finished.fact_available_at
            )
            INSERT INTO {projection_relation} (
                fact_id, fact_available_at, ingest_run_id
            )
            SELECT fact_id, fact_available_at, ingest_run_id
            FROM first_run
            ON CONFLICT (fact_id, fact_available_at) DO UPDATE
            SET ingest_run_id = EXCLUDED.ingest_run_id
            WHERE (
                SELECT existing_run.finished_at
                FROM ingest.run existing_run
                WHERE existing_run.id = {projection_relation}.ingest_run_id
            ) > (
                SELECT replacement_run.finished_at
                FROM ingest.run replacement_run
                WHERE replacement_run.id = EXCLUDED.ingest_run_id
            )
            """,
            [fact_ids, available_ats],
        ).rowcount)
