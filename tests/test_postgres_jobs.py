from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from investment_panel.core import refresh_jobs
from investment_panel.database.jobs import JobRepository
from investment_panel.database.migrations import upgrade_database
from investment_panel.database.runtime import DatabaseRuntime


@pytest.fixture
def job_repository(postgres_dsn: str):
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn, min_size=1, max_size=6)
    runtime.open()
    try:
        yield JobRepository(runtime)
    finally:
        runtime.close()


def test_job_repository_start_finish_and_rows(job_repository: JobRepository) -> None:
    job = job_repository.start("unit-refresh")
    assert job["created"] is True
    finished = job_repository.finish(job["id"], "succeeded", summary={"rows": 3})
    assert finished["status"] == "succeeded"
    assert job_repository.rows()[0]["summary"] == {"rows": 3}


def test_job_repository_serializes_nested_datetime_summary(job_repository: JobRepository) -> None:
    job = job_repository.start("timed-refresh")
    started_at = datetime(2026, 7, 12, 18, 53, tzinfo=UTC)

    job_repository.finish(
        job["id"],
        "succeeded",
        summary={"started_at": started_at, "steps": [{"observed_on": started_at.date()}]},
    )

    summary = job_repository.rows()[0]["summary"]
    assert summary == {
        "started_at": "2026-07-12T18:53:00+00:00",
        "steps": [{"observed_on": "2026-07-12"}],
    }


def test_concurrent_job_starts_are_single_flight(job_repository: JobRepository) -> None:
    with ThreadPoolExecutor(max_workers=4) as executor:
        jobs = list(executor.map(lambda _index: job_repository.start("options-radar"), range(4)))
    assert sum(bool(job["created"]) for job in jobs) == 1
    assert len({job["id"] for job in jobs}) == 1
    assert len(job_repository.rows()) == 1


def test_stale_and_restart_recovery_are_database_owned(
    job_repository: JobRepository,
    postgres_dsn: str,
) -> None:
    stale = job_repository.start("stale-job")
    active = job_repository.start("active-job")
    with closing(psycopg.connect(postgres_dsn)) as connection:
        connection.execute(
            "UPDATE ops.job_run SET started_at = %s WHERE id = %s",
            [datetime.now(UTC) - timedelta(hours=4), stale["id"]],
        )
        connection.commit()
    assert job_repository.mark_stale(stale_after=timedelta(hours=3)) == 0
    assert job_repository.heartbeat(stale["id"]) is True
    with closing(psycopg.connect(postgres_dsn)) as connection:
        connection.execute(
            "UPDATE ops.job_run SET heartbeat_at = %s WHERE id = %s",
            [datetime.now(UTC) - timedelta(hours=4), stale["id"]],
        )
        connection.commit()
    assert job_repository.mark_stale(stale_after=timedelta(hours=3)) == 1
    assert job_repository.fail_all_running("server restarted") == 1
    states = {row["job_name"]: row for row in job_repository.rows()}
    assert "did not finish" in states["stale-job"]["error"]
    assert states["active-job"]["error"] == "server restarted"
    assert active["id"] != stale["id"]


def test_refresh_job_facade_uses_postgresql_without_sidecars(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(refresh_jobs.ALLOWLIST, "unit_refresh", lambda _config_path: {"ok": True, "rows": 5})

    result = refresh_jobs.run_refresh_job("unit_refresh", migrated_postgres_dsn, "config.yaml")

    assert result["status"] == "succeeded"
    assert refresh_jobs.refresh_job_rows(migrated_postgres_dsn)[0]["summary"] == {"ok": True, "rows": 5}
