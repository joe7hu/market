from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

import psycopg

from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.backup import _credential_safe_connection, create_verified_backup
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.jobs import JobRepository
from investment_panel.database.migrations import upgrade_database
from investment_panel.database.retention import RetentionRepository
from investment_panel.database.runtime import DatabaseRuntime


def test_retention_prunes_unreferenced_history_and_keeps_published_generation(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    reference = datetime(2026, 7, 11, 12, tzinfo=UTC)
    ingestion = IngestionRepository(runtime)
    analysis = AnalysisRepository(runtime)
    jobs = JobRepository(runtime)
    ingestion.register_source("retention-test", name="Retention", family="test", kind="option_chain")
    try:
        for label, observed_at in (("old", reference - timedelta(days=180)), ("new", reference - timedelta(days=1))):
            ingest_run = ingestion.start_run("retention-test", "option_quotes", source_run_key=label, started_at=observed_at)
            ingestion.store_option_snapshot(
                ingest_run,
                source_id="retention-test",
                observed_at=observed_at,
                market_session="premarket",
                universe=label,
                rows=[
                    {
                        "symbol": "NVDA",
                        "expiration": "2027-01-15",
                        "strike": 200 if label == "old" else 210,
                        "option_type": "call",
                        "mid": 5,
                    }
                ],
            )
            ingestion.finish_run(ingest_run, "succeeded")

        old_analysis = analysis.start_run(
            "old-derived",
            input_cutoff=reference - timedelta(days=500),
            code_version="old",
            inputs={"old": True},
        )
        analysis.finish_run(old_analysis, "succeeded")
        published_analysis = analysis.start_run(
            "published",
            input_cutoff=reference - timedelta(days=500),
            code_version="kept",
            inputs={"published": True},
        )
        analysis.finish_run(published_analysis, "succeeded")
        analysis.publish(published_analysis, "today", {"daily_brief": [{"stable_key": "brief", "headline": "keep"}]})
        old_job = jobs.start("old-job")
        jobs.finish(old_job["id"], "succeeded")
        with runtime.transaction() as connection:
            connection.execute("UPDATE analysis.run SET started_at = %s WHERE id = ANY(%s)", [reference - timedelta(days=500), [old_analysis, published_analysis]])
            connection.execute("UPDATE app.publication SET created_at = %s WHERE analysis_run_id = %s", [reference - timedelta(days=100), published_analysis])
            connection.execute("UPDATE ops.job_run SET started_at = %s, finished_at = %s WHERE id = %s", [reference - timedelta(days=60), reference - timedelta(days=60), old_job["id"]])

        counts = RetentionRepository(runtime).prune(now=reference, option_days=120, analysis_days=365, publication_days=90, job_days=30)
    finally:
        runtime.close()

    assert counts == {
        "publications": 0,
        "analysis_runs": 1,
        "option_quotes": 1,
        "option_snapshots": 1,
        "job_runs": 1,
        "option_partitions": 1,
    }
    with closing(psycopg.connect(postgres_dsn)) as connection:
        quote_count = connection.execute("SELECT count(*) FROM raw.option_quote").fetchone()[0]
        kept_run = connection.execute("SELECT count(*) FROM analysis.run WHERE id = %s", [published_analysis]).fetchone()[0]
        old_partition = connection.execute("SELECT to_regclass('raw.option_quote_202601')").fetchone()[0]
    assert quote_count == 1
    assert kept_run == 1
    assert old_partition is None


def test_backup_is_custom_format_sha_verified_and_contains_all_schemas(
    migrated_postgres_dsn: str,
    tmp_path: Path,
) -> None:
    result = create_verified_backup(
        migrated_postgres_dsn,
        tmp_path,
        now=datetime(2026, 7, 11, 12, tzinfo=UTC),
    )

    dump_path = Path(result["dump_path"])
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert result["status"] == "verified"
    assert dump_path.read_bytes()[:5] == b"PGDMP"
    assert manifest["sha256"] == hashlib.sha256(dump_path.read_bytes()).hexdigest()
    assert manifest["schemas"] == ["analysis", "app", "catalog", "ingest", "ops", "raw"]


def test_backup_removes_password_from_pg_dump_arguments() -> None:
    safe_dsn, environment = _credential_safe_connection(
        "postgresql://market_user:do-not-expose@db.internal:5432/market?sslmode=require"
    )

    assert "do-not-expose" not in safe_dsn
    assert "password" not in safe_dsn
    assert "user=market_user" in safe_dsn
    assert environment is not None
    assert environment["PGPASSWORD"] == "do-not-expose"
