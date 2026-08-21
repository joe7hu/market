from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.source_facts import SourceFactRepository
from investment_panel.jobs import update_market_events


def test_market_event_refresh_is_idempotent_and_projects_catalyst(migrated_postgres_dsn: str, monkeypatch) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    config = SimpleNamespace(
        database=SimpleNamespace(url=migrated_postgres_dsn),
        event_sources=SimpleNamespace(enabled=True, bls_enabled=True, dol_enabled=False, federal_reserve_enabled=False),
        market_data=SimpleNamespace(user_agent="test"),
    )
    event = {
        "source_key": "cpi-2026-07",
        "event_scope": "macro",
        "event_kind": "inflation",
        "title": "June CPI release",
        "starts_at": datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
        "importance": "high",
        "verification_status": "confirmed",
        "source_url": "https://bls.example/cpi",
        "expected_impact": "Inflation and rates catalyst",
        "details": {"official_source": "bls"},
    }
    monkeypatch.setattr(update_market_events, "load_config", lambda _path=None: config)
    monkeypatch.setattr(update_market_events, "runtime_for_config", lambda _config: runtime)
    monkeypatch.setattr(update_market_events, "_bls_events", lambda _agent: ([event], [], []))
    try:
        assert update_market_events.run()["events"] == 1
        assert update_market_events.run()["events"] == 1
        with runtime.read() as connection:
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM raw.market_event) AS events, "
                "(SELECT count(*) FROM raw.market_event_version) AS event_versions, "
                "(SELECT count(*) FROM app.catalyst) AS catalysts"
            ).fetchone()
            catalyst = connection.execute("SELECT title, expected_impact FROM app.catalyst").fetchone()
        assert (counts["events"], counts["event_versions"], counts["catalysts"]) == (1, 2, 1)
        assert (catalyst["title"], catalyst["expected_impact"]) == (
            "June CPI release", "Inflation and rates catalyst"
        )
    finally:
        runtime.close()


def test_catalyst_projection_keeps_company_confirmation_over_yfinance_estimate(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    facts = SourceFactRepository(runtime)
    estimate = {
        "source_key": "NBIS-2026Q2", "event_key": "earnings:NBIS:2026Q2",
        "symbol": "NBIS", "event_scope": "company", "event_kind": "earnings",
        "title": "Nebius estimated earnings", "starts_at": datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
        "verification_status": "estimated", "source_tier": "estimate",
    }
    confirmed = {
        "source_key": "NBIS-2026Q2-results", "event_key": "earnings:NBIS:2026Q2",
        "symbol": "NBIS", "event_scope": "company", "event_kind": "earnings",
        "title": "Nebius Q2 2026 results", "starts_at": datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
        "verification_status": "confirmed", "source_tier": "company_ir",
    }
    try:
        for source_id, name, row in (
            ("yfinance", "Yahoo Finance", estimate),
            ("company-ir", "Company investor relations", confirmed),
            ("yfinance", "Yahoo Finance", estimate),
        ):
            ingestion.register_source(source_id, name=name, family="market", kind="calendar")
            with ingestion.run(source_id, "market_event_test") as run:
                assert facts.store_market_events(run.id, source_id, [row]) == 1
                run.finish("succeeded")
        with runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT status, version, starts_at, source_id, source_priority, confidence,
                       supersedes_id IS NOT NULL AS supersedes
                FROM app.catalyst
                WHERE event_key = 'earnings:NBIS:2026Q2'
                ORDER BY version
                """
            ).fetchall()
            current = connection.execute(
                """
                SELECT starts_at, source_id FROM app.catalyst
                WHERE event_key = 'earnings:NBIS:2026Q2' AND status = 'current'
                """
            ).fetchone()
    finally:
        runtime.close()

    assert [dict(row) for row in rows] == [
        {
            "status": "superseded", "version": 1,
            "starts_at": datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
            "source_id": "yfinance", "source_priority": 100, "confidence": 0.45,
            "supersedes": False,
        },
        {
            "status": "current", "version": 2,
            "starts_at": datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
            "source_id": "company-ir", "source_priority": 400, "confidence": 1.0,
            "supersedes": True,
        },
    ]
    assert dict(current) == {
        "starts_at": datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
        "source_id": "company-ir",
    }


def test_market_event_refresh_preserves_upstream_degradation_when_bls_is_blocked(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    config = SimpleNamespace(
        database=SimpleNamespace(url=migrated_postgres_dsn),
        event_sources=SimpleNamespace(enabled=True, bls_enabled=True, dol_enabled=False, federal_reserve_enabled=False),
        market_data=SimpleNamespace(user_agent="test"),
    )

    class BlockedResponse:
        text = "blocked"

        def raise_for_status(self) -> None:
            request = update_market_events.httpx.Request("GET", "https://www.bls.gov")
            response = update_market_events.httpx.Response(403, request=request)
            raise update_market_events.httpx.HTTPStatusError("blocked", request=request, response=response)

    monkeypatch.setattr(update_market_events, "load_config", lambda _path=None: config)
    monkeypatch.setattr(update_market_events, "runtime_for_config", lambda _config: runtime)
    monkeypatch.setattr(update_market_events.httpx, "get", lambda *_args, **_kwargs: BlockedResponse())
    try:
        result = update_market_events.run()
        with runtime.read() as connection:
            run = connection.execute(
                "SELECT status, failure_detail, summary FROM ingest.run WHERE source_id = 'official-event-calendar'"
            ).fetchone()
    finally:
        runtime.close()

    assert result["status"] == "partial"
    assert result["source_errors"]
    assert run["status"] == "partial"
    assert "blocked" in run["failure_detail"]
