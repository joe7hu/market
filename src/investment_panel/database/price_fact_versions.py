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
    projection = f"raw.{kind}_fact_availability"
    # Allow the same ingestion code to run while a rolling deployment is still
    # at the immediately preceding schema revision.  The migration backfills
    # those legacy confirmations before it makes the projection authoritative.
    if connection.execute(
        "SELECT to_regclass(%s) IS NOT NULL AS available", [projection]
    ).fetchone()["available"] is not True:
        return
    connection.execute(
        f"""
        INSERT INTO {projection} (fact_id, fact_available_at, ingest_run_id)
        SELECT %s, %s, COALESCE(
            (
                SELECT legacy.ingest_run_id
                FROM {table} legacy
                JOIN ingest.run legacy_run ON legacy_run.id = legacy.ingest_run_id
                WHERE legacy.fact_id = %s
                  AND legacy.fact_available_at = %s
                  AND legacy_run.status IN ('succeeded', 'partial')
                  AND legacy_run.finished_at IS NOT NULL
                ORDER BY legacy_run.finished_at, legacy.confirmed_at,
                         legacy.ingest_run_id
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
