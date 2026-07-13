from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from investment_panel.database.runtime import DatabaseRuntime
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
                "SELECT (SELECT count(*) FROM raw.market_event) AS events, (SELECT count(*) FROM app.catalyst) AS catalysts"
            ).fetchone()
            catalyst = connection.execute("SELECT title, expected_impact FROM app.catalyst").fetchone()
        assert (counts["events"], counts["catalysts"]) == (1, 1)
        assert (catalyst["title"], catalyst["expected_impact"]) == (
            "June CPI release", "Inflation and rates catalyst"
        )
    finally:
        runtime.close()


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
