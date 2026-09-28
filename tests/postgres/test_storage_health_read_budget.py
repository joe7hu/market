"""Storage health uses real PostgreSQL reads without maintenance-size scans."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from investment_panel.infrastructure.postgres.runtime import API_PROFILE, DatabaseRuntime
from investment_panel.infrastructure.postgres.storage_archive import StorageArchiveService


def test_storage_health_preserves_capacity_without_scanning_historical_payloads(
    application_postgres_dsn, tmp_path, monkeypatch,
):
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    monkeypatch.setenv("MARKET_STORAGE_DATABASE_PATH", str(tmp_path))
    monkeypatch.setattr("shutil.disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3, total=200 * 1024**3))
    original_read = runtime.read
    queries = []
    profiles = []

    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, *args, **kwargs):
            # Observe, rather than replace, every real database query.
            queries.append(str(query))
            return self.connection.execute(query, *args, **kwargs)

    @contextmanager
    def observed_read(profile=API_PROFILE):
        profiles.append(profile)
        with original_read(profile) as connection:
            assert connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
            yield ObservedConnection(connection)

    monkeypatch.setattr(runtime, "read", observed_read)
    try:
        service = StorageArchiveService(runtime, Path(tmp_path) / "archive")
        health = service.health()
        assert all(profile.statement_timeout_ms <= API_PROFILE.statement_timeout_ms for profile in profiles)
        assert not any("pg_column_size" in query for query in queries)
        assert health["accounting"]["database_bytes"] > 0
        assert health["accounting"]["volume_free_bytes"] == 100 * 1024**3
        assert health["accounting"]["archive_backlog"] is None
        assert health["accounting"]["protected_bytes"] is None
        assert health["accounting"]["archive_backlog_basis"] == "not_scanned_on_health_request"
        assert health["full_history_collection_allowed"] is True
        # Explicit maintenance accounting still supplies the detailed values.
        queries.clear()
        report = service.account()
        assert any("pg_column_size" in query for query in queries)
        assert report["archive_backlog"] == {"decisions": 0, "option_scans": 0}
        assert report["protected_bytes"] == 0
        assert report["archive_backlog_basis"] == "old_local_rows_not_eligibility_count"
    finally:
        runtime.close()
