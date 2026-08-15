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


def _insert_publication(
    connection: psycopg.Connection,
    *,
    scope: str,
    status: str,
    at: datetime,
    sequence: int,
) -> None:
    run_id = connection.execute(
        """
        INSERT INTO analysis.run
            (run_type, input_cutoff, code_version, input_hash, started_at, finished_at, status)
        VALUES ('retention-fixture', %s, %s, %s, %s, %s, 'succeeded')
        RETURNING id
        """,
        [at, f"fixture-{sequence}", f"{sequence:064x}", at, at],
    ).fetchone()["id"]
    connection.execute(
        """
        INSERT INTO app.publication (scope, analysis_run_id, status, created_at, published_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        [scope, run_id, status, at, at if status == "published" else None],
    )


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


def test_publication_retention_is_bounded_dry_run_and_repeatable(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    reference = datetime(2026, 8, 12, 16, tzinfo=UTC)
    try:
        with runtime.transaction() as connection:
            _insert_publication(connection, scope="market", status="published", at=reference, sequence=0)
            for sequence in range(1, 51):
                _insert_publication(
                    connection,
                    scope="market",
                    status="superseded",
                    at=reference - timedelta(days=sequence),
                    sequence=sequence,
                )
            _insert_publication(connection, scope="today", status="published", at=reference, sequence=101)
            for sequence in range(102, 107):
                _insert_publication(
                    connection,
                    scope="today",
                    status="superseded",
                    at=reference - timedelta(days=80 + sequence),
                    sequence=sequence,
                )

        retention = RetentionRepository(runtime)
        dry_run = retention.prune(now=reference, dry_run=True)
        assert dry_run == {"publications": 7, "publication_dry_run": 7}

        first = retention.prune(now=reference, publication_batch_size=3)
        second = retention.prune(now=reference, publication_batch_size=3)
        third = retention.prune(now=reference, publication_batch_size=3)
        fourth = retention.prune(now=reference, publication_batch_size=3)
    finally:
        runtime.close()

    assert first["publications"] == 3
    assert second["publications"] == 3
    assert third["publications"] == 1
    assert fourth["publications"] == 0
    with closing(psycopg.connect(postgres_dsn)) as connection:
        market_superseded = connection.execute(
            "SELECT count(*) FROM app.publication WHERE scope = 'market' AND status = 'superseded'"
        ).fetchone()[0]
        today_superseded = connection.execute(
            "SELECT count(*) FROM app.publication WHERE scope = 'today' AND status = 'superseded'"
        ).fetchone()[0]
        published_by_scope = connection.execute(
            "SELECT scope, count(*) FROM app.publication WHERE status = 'published' GROUP BY scope ORDER BY scope"
        ).fetchall()
    assert market_superseded == 48
    assert today_superseded == 0
    assert published_by_scope == [("market", 1), ("today", 1)]


def test_publication_only_retention_is_restart_safe(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    reference = datetime(2026, 8, 12, 16, tzinfo=UTC)
    try:
        with runtime.transaction() as connection:
            _insert_publication(connection, scope="market", status="published", at=reference, sequence=1000)
            for sequence in range(1001, 1052):
                _insert_publication(
                    connection,
                    scope="market",
                    status="superseded",
                    at=reference - timedelta(days=sequence),
                    sequence=sequence,
                )
        retention = RetentionRepository(runtime)
        assert retention.prune_publications(now=reference, dry_run=True) == {
            "publications": 3,
            "publication_dry_run": 3,
        }
        assert retention.prune_publications(now=reference, batch_size=2) == {"publications": 2}
        assert retention.prune_publications(now=reference, batch_size=2) == {"publications": 1}
        assert retention.prune_publications(now=reference, batch_size=2) == {"publications": 0}
    finally:
        runtime.close()


def test_publication_retention_reclaims_orphaned_compact_content(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    reference = datetime(2026, 8, 12, 16, tzinfo=UTC)
    analysis = AnalysisRepository(runtime)
    try:
        first_run = analysis.start_run(
            "compact-publication", input_cutoff=reference - timedelta(days=100),
            code_version="first", inputs={"generation": "first"},
        )
        analysis.finish_run(first_run, "succeeded")
        first_publication = analysis.publish(
            first_run, "research", {"brief": [{"stable_key": "brief", "headline": "first"}]},
        )
        second_run = analysis.start_run(
            "compact-publication", input_cutoff=reference,
            code_version="second", inputs={"generation": "second"},
        )
        analysis.finish_run(second_run, "succeeded")
        analysis.publish(
            second_run, "research", {"brief": [{"stable_key": "brief", "headline": "second"}]},
        )
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE app.publication SET created_at = %s, published_at = %s WHERE id = %s",
                [reference - timedelta(days=100), reference - timedelta(days=100), first_publication],
            )

        result = RetentionRepository(runtime).prune_publications(now=reference)
        with runtime.read() as connection:
            remaining = connection.execute(
                "SELECT (SELECT count(*) FROM app.publication_bundle) AS bundles, "
                "(SELECT count(*) FROM app.publication_payload) AS payloads, "
                "(SELECT count(*) FROM app.publication WHERE scope = 'research') AS publications"
            ).fetchone()
    finally:
        runtime.close()

    assert result == {"publications": 1, "publication_bundles": 1, "publication_payloads": 1}
    assert remaining == {"bundles": 1, "payloads": 1, "publications": 1}


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


def test_backup_normalizes_sqlalchemy_psycopg_url() -> None:
    safe_dsn, environment = _credential_safe_connection(
        "postgresql+psycopg://market_user:secret@db.internal:5432/market"
    )

    assert "postgresql+psycopg" not in safe_dsn
    assert "password" not in safe_dsn
    assert environment is not None
    assert environment["PGPASSWORD"] == "secret"
