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
        VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
        """,
        [fact_id, available_at, run_id],
    )
