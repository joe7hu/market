from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app import dependencies
from app.data_access import loaders as loaders_owner
import app.panel_snapshot as panel_owner
from app.main import app
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.runtime import DatabaseRuntime
from conftest import typed_config


def _source_rows(dsn: str) -> dict[str, dict[str, object]]:
    panel = loaders_owner.load_table_panel_data(typed_config(dsn), "source_catalog")
    return {str(row["source_id"]): row for row in panel.rows("source_catalog")}


def _register(
    repository: IngestionRepository,
    source_id: str,
    *,
    family: str = "research",
    kind: str = "news",
    enabled: bool = True,
) -> None:
    repository.register_source(
        source_id,
        name=source_id.replace("_", " ").title(),
        family=family,
        kind=kind,
        origin="test",
        capabilities={kind: True},
    )
    with repository.runtime.transaction() as connection:
        connection.execute(
            "UPDATE ingest.source SET enabled = %s WHERE id = %s",
            [enabled, source_id],
        )


def _finish(
    repository: IngestionRepository,
    source_id: str,
    status: str,
    *,
    capability: str = "news",
    failure_detail: str | None = None,
) -> str:
    run_id = repository.start_run(source_id, capability)
    repository.finish_run(run_id, status, item_count=0, failure_detail=failure_detail)
    return str(run_id)


def test_source_health_separates_run_outcome_freshness_and_disabled_state(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        _register(repository, "health_disabled", enabled=False)
        _register(repository, "health_failed")
        _finish(repository, "health_failed", "failed", failure_detail="BROWSER_CONNECT: profile is not connected")
        _register(repository, "health_partial")
        repository.register_source(
            "health_partial",
            name="Health Partial",
            family="research",
            kind="news",
            origin="test",
            capabilities={"news": True, "archive_import": True},
        )
        _finish(repository, "health_partial", "succeeded", capability="archive_import")
        _finish(repository, "health_partial", "partial", failure_detail="403 Forbidden")
        _register(repository, "health_stale")
        stale_run = _finish(repository, "health_stale", "succeeded")
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [datetime.now(UTC) - timedelta(days=4), datetime.now(UTC) - timedelta(days=4), stale_run],
            )

        rows = _source_rows(migrated_postgres_dsn)
    finally:
        runtime.close()

    assert rows["health_disabled"]["effective_status"] == "disabled"
    assert rows["health_disabled"]["freshness_status"] == "disabled"
    assert rows["health_failed"]["run_status"] == "failed"
    assert rows["health_failed"]["effective_status"] == "failed"
    assert rows["health_failed"]["freshness_status"] == "missing"
    assert rows["health_failed"]["remediation"] == "Reconnect the configured OpenCLI browser profile, then rerun this source."
    assert rows["health_partial"]["run_status"] == "partial"
    assert rows["health_partial"]["freshness_status"] == "fresh"
    assert rows["health_partial"]["effective_status"] == "degraded"
    assert {entry["capability"]: entry["status"] for entry in rows["health_partial"]["capability_health"]} == {
        "archive_import": "succeeded",
        "news": "partial",
    }
    assert rows["health_stale"]["freshness_status"] == "stale"
    assert rows["health_stale"]["effective_status"] == "stale"


def test_source_health_ignores_runs_for_removed_capabilities(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        _register(repository, "health_capability_removed")
        _finish(
            repository,
            "health_capability_removed",
            "failed",
            capability="retired_path",
            failure_detail="obsolete failure",
        )
        _finish(repository, "health_capability_removed", "succeeded", capability="news")
        rows = _source_rows(migrated_postgres_dsn)
    finally:
        runtime.close()

    source = rows["health_capability_removed"]
    assert source["effective_status"] == "healthy"
    assert source["run_status"] == "succeeded"
    assert source["latest_capability"] == "news"
    assert [entry["capability"] for entry in source["capability_health"]] == ["news"]


def test_research_enablement_sync_disables_removed_sources_and_live_x_path(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        _register(repository, "news_removed")
        _register(repository, "news_current")
        repository.register_source(
            "birdclaw_primary_tweets",
            name="Birdclaw",
            family="social",
            kind="social",
            origin="test",
            capabilities={"content": True, "x_list": True},
        )
        repository.sync_research_source_enablement(
            news_ids=["news_current"],
            blog_sources=[("blog_new", "rss")],
            news_enabled=True,
            blogs_enabled=False,
            x_enabled=False,
        )
        with runtime.read() as connection:
            records = connection.execute(
                "SELECT id, enabled, capabilities FROM ingest.source "
                "WHERE id IN ('news_removed', 'news_current', 'blog_new', 'birdclaw_primary_tweets') ORDER BY id"
            ).fetchall()
    finally:
        runtime.close()

    by_id = {str(row["id"]): row for row in records}
    assert by_id["news_removed"]["enabled"] is False
    assert by_id["news_current"]["enabled"] is True
    assert by_id["blog_new"]["enabled"] is False
    assert by_id["blog_new"]["capabilities"] == {"rss": True}
    assert by_id["birdclaw_primary_tweets"]["enabled"] is True
    assert by_id["birdclaw_primary_tweets"]["capabilities"] == {"content": True}


def test_source_health_inherits_successful_sec_aggregate_check_for_empty_form_source(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        _register(repository, "sec_edgar", family="filing", kind="sec_api")
        _finish(repository, "sec_edgar", "succeeded", capability="filings")
        _register(repository, "sec_material_events_8k", family="filing", kind="sec_filing")
        rows = _source_rows(migrated_postgres_dsn)
    finally:
        runtime.close()

    child = rows["sec_material_events_8k"]
    assert child["run_status"] == "succeeded"
    assert child["effective_status"] == "healthy"
    assert child["inherited_check"] is True
    assert child["last_data_at"] is None
    assert child["refresh_job"] is None
    assert child["refresh_jobs"] == []


def test_source_health_refresh_jobs_are_exact_and_allowlisted(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        _register(repository, "birdclaw_primary_tweets", family="social", kind="social")
        _register(repository, "news_reuters", family="research", kind="news")
        _register(repository, "robinhood", family="broker", kind="option_chain")
        _register(repository, "disclosure_csv_house", family="disclosures", kind="house_financial_disclosure")
        rows = _source_rows(migrated_postgres_dsn)
    finally:
        runtime.close()

    assert rows["birdclaw_primary_tweets"]["refresh_job"] == "update_social_sources"
    assert rows["news_reuters"]["refresh_job"] == "update_research_sources"
    assert rows["robinhood"]["refresh_job"] == "options_radar_hard_refresh"
    assert rows["robinhood"]["refresh_jobs"] == ["options_radar_hard_refresh"]
    assert rows["disclosure_csv_house"]["refresh_job"] == "update_disclosures"


def test_source_health_cadences_match_operational_schedulers(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        _register(repository, "watchlist_quote", family="market_data", kind="daily_quote")
        _register(repository, "arco", family="research", kind="private_evidence")
        _register(repository, "robinhood", family="broker", kind="option_chain")
        rows = _source_rows(migrated_postgres_dsn)
    finally:
        runtime.close()

    assert rows["watchlist_quote"]["cadence_label"] == "event driven"
    assert rows["arco"]["cadence_label"] == "4 hr"
    assert rows["robinhood"]["cadence_label"] == "3 day"


def test_historical_snapshots_are_event_driven_and_non_actionable(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        repository.register_source(
            "legacy-market-snapshot",
            name="Legacy Market Snapshot",
            family="market_data",
            kind="daily_quote",
            origin="historical-archive",
            capabilities={"daily_quote": True},
        )
        origin_run_id = _finish(repository, "legacy-market-snapshot", "succeeded", capability="daily_quote")
        repository.register_source(
            "legacy-capability-snapshot",
            name="Capability Legacy Snapshot",
            family="market_data",
            kind="daily_quote",
            origin="migration",
            capabilities={"historical_snapshot": True},
        )
        capability_run_id = _finish(
            repository, "legacy-capability-snapshot", "succeeded", capability="historical_snapshot"
        )
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id IN (%s, %s)",
                [
                    datetime.now(UTC) - timedelta(days=30),
                    datetime.now(UTC) - timedelta(days=30),
                    origin_run_id,
                    capability_run_id,
                ],
            )
        rows = _source_rows(migrated_postgres_dsn)
    finally:
        runtime.close()

    for source_id in ("legacy-market-snapshot", "legacy-capability-snapshot"):
        snapshot = rows[source_id]
        assert snapshot["freshness_status"] == "fresh"
        assert snapshot["cadence_label"] == "event driven"
        assert snapshot["refresh_job"] is None
        assert snapshot["refresh_jobs"] == []


def test_source_health_surfaces_an_active_capability_run(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        _register(repository, "health_running")
        _finish(repository, "health_running", "succeeded")
        repository.start_run("health_running", "news")
        rows = _source_rows(migrated_postgres_dsn)
    finally:
        runtime.close()

    running = rows["health_running"]
    assert running["run_status"] == "running"
    assert running["effective_status"] == "running"
    assert running["last_attempt_at"] is not None
    assert running["remediation"] == "Refresh is currently in progress."


def test_source_health_degrades_abandoned_running_attempts_and_ignores_legacy_age(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        _register(repository, "health_abandoned")
        run_id = repository.start_run("health_abandoned", "news")
        _register(repository, "health_legacy", family="legacy", kind="content")
        legacy_run = _finish(repository, "health_legacy", "succeeded", capability="historical_snapshot")
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s WHERE id = %s",
                [datetime.now(UTC) - timedelta(hours=4), run_id],
            )
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [datetime.now(UTC) - timedelta(days=30), datetime.now(UTC) - timedelta(days=30), legacy_run],
            )
        rows = _source_rows(migrated_postgres_dsn)
    finally:
        runtime.close()

    assert rows["health_abandoned"]["effective_status"] == "degraded"
    assert rows["health_abandoned"]["remediation"] == "The previous refresh appears abandoned; retry the owning job."
    assert rows["health_legacy"]["freshness_status"] == "fresh"
    assert rows["health_legacy"]["operational_group"] == "legacy"


def test_source_health_coverage_includes_non_content_market_facts(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        _register(repository, "health_market", family="market_data", kind="daily_quote")
        run_id = repository.start_run("health_market", "quotes")
        repository.store_quotes(
            run_id,
            "health_market",
            [{"symbol": "NVDA", "observed_at": datetime.now(UTC), "price": 180.0}],
        )
        repository.finish_run(run_id, "succeeded", item_count=1, instrument_count=1)
        _finish(repository, "health_market", "succeeded", capability="quotes")
        rows = _source_rows(migrated_postgres_dsn)
    finally:
        runtime.close()

    assert rows["health_market"]["item_count"] == 1
    assert rows["health_market"]["ticker_count"] == 1
    assert rows["health_market"]["last_data_at"] is not None


def test_aggregate_arco_components_do_not_advertise_a_job_that_cannot_update_them(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        _register(repository, "arco", family="research", kind="private_evidence")
        _register(repository, "arco_birdclaw", family="private_graph", kind="arco_bridge")
        _register(repository, "browser_primary_captures", family="private_graph", kind="browser_capture_export")
        rows = _source_rows(migrated_postgres_dsn)
    finally:
        runtime.close()

    assert rows["arco"]["refresh_jobs"] == ["update_arco_data"]
    assert rows["arco_birdclaw"]["refresh_jobs"] == []
    assert rows["browser_primary_captures"]["refresh_jobs"] == []


def test_source_health_uses_worst_latest_capability_and_exposes_each_owning_job(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        _register(repository, "ibkr", family="broker", kind="broker_account")
        _finish(repository, "ibkr", "failed", capability="option_quotes", failure_detail="gateway offline")
        _finish(repository, "ibkr", "succeeded", capability="broker_sync")
        rows = _source_rows(migrated_postgres_dsn)
    finally:
        runtime.close()

    ibkr = rows["ibkr"]
    assert ibkr["run_status"] == "failed"
    assert ibkr["latest_capability"] == "option_quotes"
    assert ibkr["effective_status"] == "failed"
    assert ibkr["refresh_job"] == "update_ibkr_options"
    assert set(ibkr["refresh_jobs"]) == {"update_broker_sources", "update_ibkr_options"}


def test_source_catalog_endpoint_matches_health_snapshot_contract(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = IngestionRepository(runtime)
    try:
        _register(repository, "contract_healthy")
        _finish(repository, "contract_healthy", "succeeded")
        _register(repository, "contract_disabled", enabled=False)
    finally:
        runtime.close()

    panel_owner.invalidate_context_cache()
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_config, lambda: typed_config(migrated_postgres_dsn))
    client = TestClient(app)
    catalog = client.get("/api/source-catalog")
    snapshot = client.get("/api/panel-snapshot?scope=health")

    assert catalog.status_code == 200
    assert snapshot.status_code == 200
    catalog_payload = catalog.json()
    snapshot_rows = snapshot.json()["tables"]["source_catalog"]["rows"]
    catalog_status = {row["source_id"]: row["effective_status"] for row in catalog_payload["rows"]}
    snapshot_status = {row["source_id"]: row["effective_status"] for row in snapshot_rows}
    assert catalog_status == snapshot_status
    assert all("config" not in row for row in catalog_payload["rows"])
    assert catalog_payload["summary"]["enabled"] == 1
    assert catalog_payload["summary"]["healthy"] == 1
    assert catalog_payload["summary"]["disabled"] == 1
