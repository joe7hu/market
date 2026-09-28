"""Fundamental observation persistence must be retry-idempotent, not lossy."""
from datetime import UTC, datetime, timedelta

from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime


def test_fundamental_retries_skip_unchanged_rows_but_keep_corrections_and_new_times(application_postgres_dsn):
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    try:
        repository = IngestionRepository(runtime)
        repository.register_source("fundamental-retry", name="Audit", family="filing", kind="fundamentals")
        at = datetime(2026, 9, 25, 14, tzinfo=UTC)
        run_id = repository.start_run("fundamental-retry", "fundamentals", started_at=at)
        row = {"symbol": "FUNDRETRY", "observed_at": at, "period_end": "2026-06-30",
               "filed_at": at - timedelta(days=30), "values": {"revenue": 123456789, "margin": 0.125}}

        def versions():
            with runtime.read() as connection:
                return connection.execute("""SELECT id, xmin::text, ctid::text, values, filed_at
                    FROM raw.fundamental_observation ORDER BY id""").fetchall()

        assert repository.store_fundamental_observations(run_id, "fundamental-retry", "quarterly", [row]) == 1
        first = versions()
        repository.store_fundamental_observations(run_id, "fundamental-retry", "quarterly", [row])
        assert versions() == first
        row["values"] = {"revenue": 123456790, "margin": 0.125}
        row["filed_at"] = at - timedelta(days=29)
        repository.store_fundamental_observations(run_id, "fundamental-retry", "quarterly", [row])
        corrected = versions()
        assert len(corrected) == 1
        assert corrected[0]["id"] == first[0]["id"]
        assert corrected[0]["xmin"] != first[0]["xmin"]
        assert corrected[0]["values"]["revenue"] == 123456790
        repository.store_fundamental_observations(run_id, "fundamental-retry", "quarterly", [row])
        assert versions() == corrected
        row["observed_at"] = at + timedelta(hours=1)
        repository.store_fundamental_observations(run_id, "fundamental-retry", "quarterly", [row])
        assert len(versions()) == 2
    finally:
        runtime.close()
