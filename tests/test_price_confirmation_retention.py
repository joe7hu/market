from __future__ import annotations

from datetime import UTC, datetime

from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.price_confirmation_retention import PriceConfirmationRetentionRepository
from investment_panel.database.runtime import DatabaseRuntime


def test_confirmation_retention_compacts_success_duplicates_but_keeps_failed_audit(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    try:
        ingestion.register_source("daily", name="Daily", family="market", kind="daily_bars")
        runs = [ingestion.start_run("daily", "price_bars") for _ in range(3)]
        for run in runs:
            ingestion.store_price_bars(
                run,
                "daily",
                [{"symbol": "NVDA", "date": "2026-08-12", "close": 180}],
                asset_classes={"NVDA": "equity"},
            )
        ingestion.finish_run(runs[0], "failed", failure_detail="fixture")
        ingestion.finish_run(runs[1], "succeeded")
        ingestion.finish_run(runs[2], "succeeded")
        with runtime.transaction() as connection:
            fact = connection.execute(
                "SELECT id, available_at FROM raw.price_bar WHERE source_id = 'daily'"
            ).fetchone()
            connection.execute(
                "INSERT INTO raw.price_bar_confirmation (fact_id, fact_available_at, ingest_run_id) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                [fact["id"], fact["available_at"], runs[2]],
            )
        retention = PriceConfirmationRetentionRepository(runtime)
        preview = retention.compact(table="price_bar", fact_batch_size=10, dry_run=True)
        result = retention.compact(table="price_bar", fact_batch_size=10)
        with runtime.read() as connection:
            statuses = connection.execute(
                """
                SELECT run.status
                FROM raw.price_bar_confirmation confirmation
                JOIN ingest.run run ON run.id = confirmation.ingest_run_id
                ORDER BY run.status
                """
            ).fetchall()
    finally:
        runtime.close()

    assert preview["fact_versions"] == 1
    assert preview["deleted"] == 1
    assert result["deleted"] == 1
    assert [row["status"] for row in statuses] == ["failed", "succeeded"]


def test_confirmation_retention_can_target_one_instrument(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    try:
        ingestion.register_source("daily", name="Daily", family="market", kind="daily_bars")
        runs = [ingestion.start_run("daily", "price_bars") for _ in range(2)]
        for run in runs:
            ingestion.store_price_bars(
                run,
                "daily",
                [{"symbol": "NVDA", "date": "2026-08-12", "close": 180}],
                asset_classes={"NVDA": "equity"},
            )
        for run in runs:
            ingestion.finish_run(run, "succeeded")
        with runtime.read() as connection:
            instrument_id = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = 'NVDA'"
            ).fetchone()["id"]
        result = PriceConfirmationRetentionRepository(runtime).compact_for_instruments(
            table="price_bar", instrument_ids=[instrument_id], fact_batch_size=10
        )
    finally:
        runtime.close()

    assert result["fact_versions"] == 1
    assert result["deleted"] == 1


def test_availability_projection_cursor_keeps_multiple_versions_of_one_fact(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    try:
        ingestion.register_source("daily", name="Daily", family="market", kind="daily_bars")
        for close in (180, 181, 182):
            run = ingestion.start_run("daily", "price_bars")
            ingestion.store_price_bars(
                run,
                "daily",
                [{"symbol": "NVDA", "date": "2026-08-12", "close": close}],
                asset_classes={"NVDA": "equity"},
            )
            ingestion.finish_run(run, "succeeded")
        with runtime.transaction() as connection:
            instrument_id = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = 'NVDA'"
            ).fetchone()["id"]
            connection.execute("DELETE FROM raw.price_bar_fact_availability")
        retention = PriceConfirmationRetentionRepository(runtime)
        cursor_id = 0
        cursor_at = None
        projected = 0
        for _ in range(3):
            batch = retention.project_availability_for_instruments(
                table="price_bar",
                instrument_ids=[instrument_id],
                after_fact_id=cursor_id,
                after_available_at=cursor_at,
                fact_batch_size=1,
            )
            projected += int(batch["projected"])
            cursor_id = int(batch["next_after_fact_id"])
            cursor_at = batch["next_after_available_at"]
        with runtime.read() as connection:
            rows = connection.execute(
                "SELECT count(*) AS count FROM raw.price_bar_fact_availability"
            ).fetchone()["count"]
    finally:
        runtime.close()

    assert projected == 3
    assert rows == 3


def test_global_availability_projection_is_bounded_and_resumable(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    try:
        ingestion.register_source("daily", name="Daily", family="market", kind="daily_bars")
        for symbol, close in (("NVDA", 180), ("AMD", 181)):
            run = ingestion.start_run("daily", "price_bars")
            ingestion.store_price_bars(
                run,
                "daily",
                [{"symbol": symbol, "date": "2026-08-12", "close": close}],
                asset_classes={symbol: "equity"},
            )
            ingestion.finish_run(run, "succeeded")
        with runtime.transaction() as connection:
            connection.execute("DELETE FROM raw.price_bar_fact_availability")
        retention = PriceConfirmationRetentionRepository(runtime)
        first = retention.project_availability_batch(table="price_bar", fact_batch_size=1)
        second = retention.project_availability_batch(
            table="price_bar",
            after_fact_id=int(first["next_after_fact_id"]),
            after_available_at=first["next_after_available_at"],
            fact_batch_size=1,
        )
        with runtime.read() as connection:
            projected = connection.execute(
                "SELECT count(*) AS count FROM raw.price_bar_fact_availability"
            ).fetchone()["count"]
    finally:
        runtime.close()

    assert first["projected"] == 1
    assert second["projected"] == 1
    assert projected == 2
