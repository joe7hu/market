from __future__ import annotations

from datetime import UTC, datetime

from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.migrations import upgrade_database
from investment_panel.database.price_fact_versions import confirm_price_fact
from investment_panel.database.price_confirmation_retention import PriceConfirmationRetentionRepository
from investment_panel.database.runtime import DatabaseRuntime


def test_current_price_prefers_latest_available_intraday_quote_over_daily_nominal_close(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    as_of = datetime(2026, 8, 12, 19, 45, tzinfo=UTC)  # 15:45 ET
    try:
        repository.register_source("daily", name="Daily", family="market", kind="daily_bars")
        repository.register_source("robinhood", name="Robinhood", family="broker", kind="quote")
        daily_run = repository.start_run("daily", "price_bars", started_at=datetime(2026, 8, 12, 15, tzinfo=UTC))
        repository.store_price_bars(
            daily_run,
            "daily",
            [{"symbol": "NVDA", "date": "2026-08-12", "close": 180}],
            asset_classes={"NVDA": "equity"},
        )
        repository.finish_run(daily_run, "succeeded")
        quote_run = repository.start_run("robinhood", "quotes", started_at=datetime(2026, 8, 12, 19, 31, tzinfo=UTC))
        repository.store_quotes(
            quote_run,
            "robinhood",
            [{"symbol": "NVDA", "observed_at": datetime(2026, 8, 12, 19, 30, tzinfo=UTC), "price": 185}],
        )
        repository.finish_run(quote_run, "succeeded")
        with runtime.read() as connection:
            instrument_id = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = 'NVDA'"
            ).fetchone()["id"]

        with runtime.read() as connection:
            selected = connection.execute(
                "SELECT price, source_id, observed_at FROM raw.current_price_at(%s, ARRAY[%s::bigint])",
                [as_of, instrument_id],
            ).fetchone()
        assert selected["price"] == 185
        assert selected["source_id"] == "robinhood"
        assert selected["observed_at"] == datetime(2026, 8, 12, 19, 30, tzinfo=UTC)

    finally:
        runtime.close()


def test_current_price_orders_by_information_time_not_daily_nominal_close(
    migrated_postgres_dsn: str,
) -> None:
    """A live quote can be newer even when its observed time is before 16:00 ET."""

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    daily_available = datetime(2026, 8, 12, 19, 50, tzinfo=UTC)
    robinhood_available = datetime(2026, 8, 12, 19, 59, tzinfo=UTC)
    as_of = datetime(2026, 8, 12, 20, 0, tzinfo=UTC)
    try:
        repository.register_source("daily", name="Daily", family="market", kind="daily_bars")
        repository.register_source("robinhood", name="Robinhood", family="broker", kind="quote")
        daily_run = repository.start_run("daily", "price_bars", started_at=daily_available)
        repository.store_price_bars(
            daily_run,
            "daily",
            [{"symbol": "TSLA", "date": "2026-08-12", "close": 330}],
            asset_classes={"TSLA": "equity"},
        )
        repository.finish_run(daily_run, "succeeded")
        quote_run = repository.start_run("robinhood", "quotes", started_at=robinhood_available)
        repository.store_quotes(
            quote_run,
            "robinhood",
            [{"symbol": "TSLA", "observed_at": datetime(2026, 8, 12, 19, 58, tzinfo=UTC), "price": 335}],
        )
        repository.finish_run(quote_run, "succeeded")
        with runtime.transaction() as connection:
            instrument_id = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = 'TSLA'"
            ).fetchone()["id"]
            connection.execute(
                "UPDATE ingest.run SET finished_at = %s WHERE id = %s",
                [daily_available, daily_run],
            )
            connection.execute(
                "UPDATE ingest.run SET finished_at = %s WHERE id = %s",
                [robinhood_available, quote_run],
            )
            connection.execute(
                "UPDATE raw.quote SET available_at = %s WHERE instrument_id = %s AND source_id = 'daily'",
                [daily_available, instrument_id],
            )
            connection.execute(
                "UPDATE raw.price_bar SET available_at = %s WHERE instrument_id = %s AND source_id = 'daily'",
                [daily_available, instrument_id],
            )
            connection.execute(
                "UPDATE raw.quote SET available_at = %s WHERE instrument_id = %s AND source_id = 'robinhood'",
                [robinhood_available, instrument_id],
            )
            connection.execute("DELETE FROM raw.quote_confirmation")
            connection.execute("DELETE FROM raw.price_bar_confirmation")
            connection.execute(
                """
                INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id)
                SELECT id, available_at, ingest_run_id FROM raw.quote
                """
            )
            connection.execute(
                """
                INSERT INTO raw.price_bar_confirmation (fact_id, fact_available_at, ingest_run_id)
                SELECT id, available_at, ingest_run_id FROM raw.price_bar
                """
            )
        with runtime.read() as connection:
            selected = connection.execute(
                "SELECT price, source_id, observed_at, available_at FROM raw.current_price_at(%s, ARRAY[%s::bigint])",
                [as_of, instrument_id],
            ).fetchone()
        assert selected["price"] == 335
        assert selected["source_id"] == "robinhood"
        assert selected["observed_at"] == datetime(2026, 8, 12, 19, 58, tzinfo=UTC)
        assert selected["available_at"] == robinhood_available
    finally:
        runtime.close()


def test_confirmation_is_idempotent_per_price_fact_version(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        repository.register_source("daily", name="Daily", family="market", kind="daily_bars")
        run = repository.start_run("daily", "price_bars")
        repository.store_price_bars(
            run,
            "daily",
            [{"symbol": "NVDA", "date": "2026-08-12", "close": 180}],
            asset_classes={"NVDA": "equity"},
        )
        repository.finish_run(run, "succeeded")
        with runtime.transaction() as connection:
            fact = connection.execute(
                "SELECT id, available_at FROM raw.price_bar WHERE source_id = 'daily'"
            ).fetchone()
            before = connection.execute(
                "SELECT count(*) AS count FROM raw.price_bar_confirmation WHERE fact_id = %s",
                [fact["id"]],
            ).fetchone()["count"]
            confirm_price_fact(connection, "price_bar", fact["id"], fact["available_at"], run)
            after = connection.execute(
                "SELECT count(*) AS count FROM raw.price_bar_confirmation WHERE fact_id = %s",
                [fact["id"]],
            ).fetchone()["count"]
            projection = connection.execute(
                """
                SELECT ingest_run_id
                FROM raw.price_bar_fact_availability
                WHERE fact_id = %s AND fact_available_at = %s
                """,
                [fact["id"], fact["available_at"]],
            ).fetchone()
        assert before == after == 1
        assert projection == {"ingest_run_id": run}
    finally:
        runtime.close()


def test_current_price_projection_backfills_existing_successful_confirmations(postgres_dsn: str) -> None:
    """The selector stays PIT-correct when 0033 upgrades existing audit rows."""

    upgrade_database(postgres_dsn, "20260812_0032")
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        repository.register_source("daily", name="Daily", family="market", kind="daily_bars")
        repository.register_source("robinhood", name="Robinhood", family="broker", kind="quote")
        daily_run = repository.start_run("daily", "price_bars", started_at=datetime(2026, 8, 12, 15, tzinfo=UTC))
        repository.store_price_bars(
            daily_run,
            "daily",
            [{"symbol": "NVDA", "date": "2026-08-12", "close": 180}],
            asset_classes={"NVDA": "equity"},
        )
        repository.finish_run(daily_run, "succeeded")
        quote_run = repository.start_run("robinhood", "quotes", started_at=datetime(2026, 8, 12, 19, 31, tzinfo=UTC))
        repository.store_quotes(
            quote_run,
            "robinhood",
            [{"symbol": "NVDA", "observed_at": datetime(2026, 8, 12, 19, 30, tzinfo=UTC), "price": 185}],
        )
        repository.finish_run(quote_run, "succeeded")
    finally:
        runtime.close()

    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        with runtime.read() as connection:
            instrument_id = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = 'NVDA'"
            ).fetchone()["id"]
        retention = PriceConfirmationRetentionRepository(runtime)
        bar_projection = retention.project_availability_for_instruments(
            table="price_bar", instrument_ids=[instrument_id]
        )
        quote_projection = retention.project_availability_for_instruments(
            table="quote", instrument_ids=[instrument_id]
        )
        with runtime.read() as connection:
            projection_count = connection.execute(
                "SELECT count(*) AS count FROM raw.quote_fact_availability"
            ).fetchone()["count"]
            selected = connection.execute(
                """
                SELECT price, source_id
                FROM raw.current_price_at(
                    %s,
                    ARRAY[(SELECT id FROM catalog.instrument WHERE symbol = 'NVDA')]
                )
                """,
                [datetime(2026, 8, 12, 19, 45, tzinfo=UTC)],
            ).fetchone()
        assert bar_projection["projected"] == 1
        assert quote_projection["projected"] == 2
        assert projection_count == 2
        assert selected == {"price": 185, "source_id": "robinhood"}
    finally:
        runtime.close()


def test_new_retry_preserves_earliest_legacy_confirmation_when_projection_is_missing(
    migrated_postgres_dsn: str,
) -> None:
    """A rolling projection catch-up cannot move a fact's historical availability."""

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        repository.register_source("daily", name="Daily", family="market", kind="daily_bars")
        first_run = repository.start_run("daily", "price_bars")
        repository.store_price_bars(
            first_run,
            "daily",
            [{"symbol": "NVDA", "date": "2026-08-12", "close": 180}],
            asset_classes={"NVDA": "equity"},
        )
        repository.finish_run(first_run, "succeeded")
        with runtime.transaction() as connection:
            fact = connection.execute(
                "SELECT id, available_at FROM raw.price_bar WHERE source_id = 'daily'"
            ).fetchone()
            connection.execute(
                "DELETE FROM raw.price_bar_fact_availability WHERE fact_id = %s AND fact_available_at = %s",
                [fact["id"], fact["available_at"]],
            )
        retry_run = repository.start_run("daily", "price_bars")
        repository.store_price_bars(
            retry_run,
            "daily",
            [{"symbol": "NVDA", "date": "2026-08-12", "close": 180}],
            asset_classes={"NVDA": "equity"},
        )
        repository.finish_run(retry_run, "succeeded")
        with runtime.read() as connection:
            projection = connection.execute(
                """
                SELECT ingest_run_id
                FROM raw.price_bar_fact_availability
                WHERE fact_id = %s AND fact_available_at = %s
                """,
                [fact["id"], fact["available_at"]],
            ).fetchone()
        assert projection == {"ingest_run_id": first_run}
    finally:
        runtime.close()
