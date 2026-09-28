"""Cross-run replay must retain usable fact provenance without blocking recovery."""
from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime


@pytest.fixture
def capture(application_postgres_dsn):
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    try:
        repository = IngestionRepository(runtime)
        repository.register_source("cross-run-fundamentals", name="Audit", family="filing", kind="fundamentals")
        at = datetime(2026, 9, 25, 14, tzinfo=UTC)
        row = {"symbol": "CROSSRUN", "observed_at": at, "period_end": "2026-06-30",
               "filed_at": at - timedelta(days=30), "values": {"revenue": 123456789, "margin": 0.125}}
        yield runtime, repository, row
    finally:
        runtime.close()


def _fact(runtime):
    with runtime.read() as connection:
        return connection.execute("""SELECT observation.id, observation.xmin::text,
            observation.ctid::text, observation.values, observation.filed_at,
            observation.ingest_run_id, run.finished_at AS available_at, run.status
            FROM raw.fundamental_observation observation
            JOIN ingest.run run ON run.id = observation.ingest_run_id""").fetchone()


def _store(repository, row):
    run_id = repository.start_run("cross-run-fundamentals", "fundamentals")
    assert repository.store_fundamental_observations(
        run_id, "cross-run-fundamentals", "quarterly", [row],
    ) == 1
    return run_id


@pytest.mark.parametrize("status", ["succeeded", "partial"])
def test_usable_cross_run_retry_preserves_tuple_and_original_availability(capture, status):
    runtime, repository, row = capture
    original = _store(repository, row)
    repository.finish_run(original, status)
    before = _fact(runtime)
    retry = _store(repository, row)
    # Do not temporarily hide the good fact behind the new running run.
    assert _fact(runtime) == before
    repository.finish_run(retry, "succeeded")
    assert _fact(runtime) == before
    failed_retry = _store(repository, row)
    repository.finish_run(failed_retry, "failed")
    assert _fact(runtime) == before
    # Real corrections from another run still replace their substantive values
    # and acquire that run's information-availability provenance.
    row["values"] = {"revenue": 123456790, "margin": 0.125}
    row["filed_at"] += timedelta(days=1)
    correction = _store(repository, row)
    repository.finish_run(correction, "succeeded")
    after = _fact(runtime)
    assert after["id"] == before["id"]
    assert after["xmin"] != before["xmin"]
    assert after["values"] == row["values"]
    assert after["filed_at"] == row["filed_at"]
    assert after["ingest_run_id"] == correction
    assert after["available_at"] > before["available_at"]


@pytest.mark.parametrize("status", ["failed", "skipped", "running"])
def test_cross_run_retry_repairs_unusable_provenance(capture, status):
    runtime, repository, row = capture
    original = _store(repository, row)
    if status != "running":
        repository.finish_run(original, status)
    before = _fact(runtime)
    retry = _store(repository, row)
    repository.finish_run(retry, "succeeded")
    after = _fact(runtime)
    assert after["id"] == before["id"]
    assert after["values"] == before["values"]
    assert after["ingest_run_id"] == retry
    assert after["status"] == "succeeded" and after["available_at"] is not None
    # Once repaired, further retries no longer replace provenance or the tuple.
    again = _store(repository, row)
    repository.finish_run(again, "succeeded")
    assert _fact(runtime) == after
