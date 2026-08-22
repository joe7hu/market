"""Concurrency and provenance helpers for versioned price facts."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID


FactKind = Literal["price_bar", "quote"]


def lock_price_fact(connection: Any, kind: FactKind, *identity: object) -> None:
    key = "|".join([kind, *(str(value) for value in identity)])
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])


def confirm_price_fact(
    connection: Any,
    kind: FactKind,
    fact_id: int,
    available_at: object,
    run_id: UUID,
) -> None:
    table = "raw.price_bar_confirmation" if kind == "price_bar" else "raw.quote_confirmation"
    projection = f"raw.{kind}_fact_availability"
    projection_available = connection.execute(
        "SELECT to_regclass(%s) IS NOT NULL AS available", [projection]
    ).fetchone()["available"] is True
    if projection_available:
        # Confirmation rows are staging only.  A terminal projection row is
        # the duplicate guard, so repeated successful runs do not recreate
        # historical staging noise after cutover.
        connection.execute(
            f"""
            INSERT INTO {table} (fact_id, fact_available_at, ingest_run_id)
            SELECT %s, %s, %s
            WHERE NOT EXISTS (
                SELECT 1
                FROM {projection} existing
                JOIN ingest.run existing_run ON existing_run.id = existing.ingest_run_id
                WHERE existing.fact_id = %s
                  AND existing.fact_available_at = %s
                  AND existing_run.status IN ('succeeded', 'partial')
                  AND existing_run.finished_at IS NOT NULL
            )
            ON CONFLICT DO NOTHING
            """,
            [fact_id, available_at, run_id, fact_id, available_at],
        )
    else:
        # Keep pre-projection migrations usable during a rolling deployment.
        connection.execute(
            f"""
            INSERT INTO {table} (fact_id, fact_available_at, ingest_run_id)
            SELECT %s, %s, %s
            WHERE NOT EXISTS (
                SELECT 1
                FROM {table} existing
                JOIN ingest.run existing_run ON existing_run.id = existing.ingest_run_id
                WHERE existing.fact_id = %s
                  AND existing.fact_available_at = %s
                  AND existing_run.status IN ('succeeded', 'partial')
            )
            ON CONFLICT DO NOTHING
            """,
            [fact_id, available_at, run_id, fact_id, available_at],
        )
        return
    connection.execute(
        f"""
        INSERT INTO {projection} (fact_id, fact_available_at, ingest_run_id)
        SELECT %s, %s, coalesce(
            (
                SELECT confirmation.ingest_run_id
                FROM {table} confirmation
                JOIN ingest.run confirmation_run
                  ON confirmation_run.id = confirmation.ingest_run_id
                WHERE confirmation.fact_id = %s
                  AND confirmation.fact_available_at = %s
                  AND confirmation_run.status IN ('succeeded', 'partial')
                  AND confirmation_run.finished_at IS NOT NULL
                ORDER BY confirmation_run.finished_at,
                         confirmation.confirmed_at,
                         confirmation.ingest_run_id
                LIMIT 1
            ),
            %s
        )
        ON CONFLICT (fact_id, fact_available_at) DO UPDATE
        SET ingest_run_id = EXCLUDED.ingest_run_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM ingest.run existing_run
            WHERE existing_run.id = {projection}.ingest_run_id
              AND existing_run.status IN ('succeeded', 'partial')
              AND existing_run.finished_at IS NOT NULL
        )
        """,
        [fact_id, available_at, fact_id, available_at, run_id],
    )
