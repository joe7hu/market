"""Database regression coverage for the Health repairs."""

from datetime import UTC, datetime, timedelta

from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime


def test_current_price_prefers_newer_observation_over_late_old_confirmation(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    old_observed = datetime(2026, 8, 11, 20, tzinfo=UTC)
    new_observed = datetime(2026, 8, 12, 20, tzinfo=UTC)
    try:
        repository.register_source(
            "timed-quote", name="timed-quote", family="market", kind="quote",
            operational_state="active", health_owner="test", freshness_seconds=3600,
        )
        old_run = repository.start_run("timed-quote", "quotes", started_at=old_observed)
        repository.store_quotes(old_run, "timed-quote", [{"symbol": "TIME", "price": 100, "observed_at": old_observed}])
        repository.finish_run(old_run, "succeeded")
        new_run = repository.start_run("timed-quote", "quotes", started_at=new_observed)
        repository.store_quotes(new_run, "timed-quote", [{"symbol": "TIME", "price": 110, "observed_at": new_observed}])
        repository.finish_run(new_run, "succeeded")
        with runtime.transaction() as connection:
            instrument_id = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = 'TIME'"
            ).fetchone()["id"]
            connection.execute(
                "UPDATE ingest.run SET finished_at = %s WHERE id = %s",
                [datetime(2026, 8, 13, 20, tzinfo=UTC), old_run],
            )
            connection.execute(
                "UPDATE ingest.run SET finished_at = %s WHERE id = %s",
                [datetime(2026, 8, 13, 19, tzinfo=UTC), new_run],
            )
        with runtime.read() as connection:
            selected = connection.execute(
                "SELECT price, source_id, observed_at FROM raw.current_price_at(%s, ARRAY[%s::bigint])",
                [datetime.now(UTC) + timedelta(minutes=1), instrument_id],
            ).fetchone()
        assert selected == {"price": 110, "source_id": "timed-quote", "observed_at": new_observed}
    finally:
        runtime.close()


def test_advisor_context_indexes_exist(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.read() as connection:
            names = {
                row["indexname"]
                for row in connection.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = 'app'"
                ).fetchall()
            }
        assert {"ix_app_publication_payload_symbol", "ix_app_publication_bundle_item_content_hash"} <= names
    finally:
        runtime.close()
