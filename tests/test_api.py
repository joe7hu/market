from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from psycopg.types.json import Jsonb
from fastapi import HTTPException
from fastapi.testclient import TestClient

from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.options_radar import DEFAULT_STRATEGY_VERSION, refresh_options_radar
from investment_panel.database.authority import close_cached_runtimes
from investment_panel.database.agents import AgentRepository
from investment_panel.database.authority import runtime_for_url
from investment_panel.database.migrations import upgrade_database
from app.data_access import DataStatus, PanelData, settings_payload, ticker_decision_brief, update_agent_settings_config, update_research_sources_config
import app.main as app_main
import app.deps as app_deps
from app import panel_contracts
from app.main import app, _require_local_request
from tests.test_option_agent_postmortem import seed_missed_winner
from tests.test_option_agent_thesis import seed_fire_candidate


def _use_temp_api_db(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    app_main._invalidate_context_cache()
    monkeypatch.setattr(
        app_deps,
        "load_config",
        lambda _path=None: {
            "database": {"duckdb_path": str(db_path)},
            "nas": {"status_dir": str(db_path.parent / "status")},
        },
    )


def _use_postgres_api(monkeypatch: pytest.MonkeyPatch, dsn: str) -> None:
    app_main._invalidate_context_cache()
    monkeypatch.setattr(app_deps, "load_config", lambda _path=None: {"database": {"url": dsn}})


def test_api_routes_return_json(postgresql, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    info = postgresql.info
    credentials = info.user if not info.password else f"{info.user}:{info.password}"
    postgres_dsn = f"postgresql://{credentials}@{info.host}:{info.port}/{info.dbname}"
    upgrade_database(postgres_dsn)
    duckdb_path = tmp_path / "api-smoke.duckdb"
    _use_temp_api_db(monkeypatch, duckdb_path)
    monkeypatch.setattr(
        app_deps,
        "load_config",
        lambda _path=None: {
            "database": {"url": postgres_dsn, "duckdb_path": str(duckdb_path)},
            "nas": {"status_dir": str(tmp_path / "status")},
        },
    )
    client = TestClient(app)
    try:
        paths = [
        "/api/status",
        "/api/panel-contract",
        "/api/dashboard",
        "/api/panel-snapshot?scope=feed",
        "/api/panel-snapshot?scope=watchlist",
        "/api/panel-snapshot?scope=sources",
        "/api/panel-snapshot?scope=superinvestors",
        "/api/panel-snapshot?scope=market",
        "/api/panel-snapshot?scope=options-radar",
        "/api/panel-snapshot?scope=today",
        "/api/panel-snapshot?scope=dashboard",
        "/api/decision-readiness",
        "/api/signals",
        "/api/opportunities-ranked",
        "/api/opportunity-sources",
        "/api/candidates",
        "/api/portfolio",
        "/api/theses",
        "/api/thesis-monitor",
        "/api/trader-twins",
        "/api/catalysts",
        "/api/fundamentals",
        "/api/disclosures",
        "/api/quotes",
        "/api/screener",
        "/api/options-expiries",
        "/api/options-chain",
        "/api/options-payoff-scenarios",
        "/api/options-provider-capabilities",
        "/api/options-expiry-signals",
        "/api/options-ticker-signals",
        "/api/option-strategy-versions",
        "/api/option-radar-opportunities",
        "/api/option-snapshot",
        "/api/option-features",
        "/api/stock-features",
        "/api/agent-thesis",
        "/api/agent-thesis-requests",
        "/api/agent-thesis-validations",
        "/api/agent-postmortem-requests",
        "/api/agent-postmortems",
        "/api/candidate-events",
        "/api/candidate-event-marks",
        "/api/candidate-event-attributions",
        "/api/shadow-trades",
        "/api/shadow-trade-marks",
        "/api/radar-state-transitions",
        "/api/option-attributions",
        "/api/missed-winner-events",
        "/api/strategy-mutation-proposals",
        "/api/strategy-backtests",
        "/api/strategy-forward-tests",
        "/api/strategy-cohorts",
        "/api/news",
        "/api/tradingview-symbol-search",
        "/api/tradingview-watchlists",
        "/api/tradingview-alerts",
        "/api/tradingview-chart-state",
        "/api/sepa",
        "/api/liquidity",
        "/api/correlations",
        "/api/etf-premiums",
        "/api/analyst-estimates",
        "/api/earnings",
        "/api/earnings-setups",
        "/api/valuations",
        "/api/technicals",
        "/api/research-packets",
        "/api/provider-runs",
        "/api/broker/status",
        "/api/broker/accounts",
        "/api/broker/positions",
        "/api/agent/recommendations",
        "/api/paper-orders",
        "/api/daily-brief",
        "/api/feed",
        "/api/watchlist-screen",
        "/api/watchlist/symbols",
        "/api/source-consensus",
        "/api/source-ticker-rankings",
        "/api/ownership-consensus",
        "/api/market-context",
        "/api/portfolio-risk/exposure-clusters",
        "/api/portfolio-risk/correlation-edges",
        "/api/portfolio-risk/cards",
        "/api/portfolio-risk/review-actions",
        "/api/source-health",
        "/api/sources",
        "/api/source-items",
        "/api/source-runs",
        "/api/ticker-source-signals",
        "/api/sources/sec_edgar",
        "/api/source-ingestion-audit",
        "/api/refresh-jobs",
        "/api/settings",
        "/api/tickers/TSLA",
        ]
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("application/json")
    finally:
        close_cached_runtimes()


def test_settings_payload_includes_agent_control_metadata() -> None:
    payload = settings_payload(
        {
            "database": {"duckdb_path": "data/test.duckdb"},
            "research_sources": {
                "x": {"enabled": True, "list_id": "123", "priority_handles": ["balajis"]},
                "news": {"enabled": True, "providers": ["bloomberg"]},
                "blogs": {"enabled": True, "substack_urls": ["https://example.substack.com"], "rss_urls": ["https://example.com/feed"]},
            },
            "agents": {
                "option_thesis": {"enabled": True, "command": "market-codex-option-thesis-agent", "timeout_seconds": 180, "limit": 8},
                "option_postmortem": {"enabled": False, "command": "market-codex-option-postmortem-agent", "timeout_seconds": 120, "limit": 2},
            },
        },
        PanelData(
            status=DataStatus(True, "ok", "test"),
            tables={
                "source_runs": [
                    {"source_id": "news_bloomberg", "status": "ok", "capability": "news", "finished_at": "2026-06-15T10:00:00", "item_count": 20, "ticker_count": 3},
                    {"source_id": "blog_example_com", "status": "failed", "capability": "rss", "finished_at": "2026-06-15T11:00:00", "item_count": 0, "ticker_count": 0, "failure_detail": "bad feed"},
                ]
            },
        ),
    )

    assert payload["agents"]["config"]["option_thesis"]["limit"] == 8
    assert payload["agents"]["runtime"]["option_thesis"]["active"] is True
    assert payload["agents"]["runtime"]["option_postmortem"]["status"] == "paused"
    assert payload["agents"]["scheduler"]["agent_refresh_seconds"] == "0"
    assert payload["agents"]["scheduler"]["radar_refresh_seconds"] == "0"
    assert payload["agents"]["scheduler"]["source_refresh_seconds"] == "0"
    assert payload["agents"]["scheduler"]["market_environment_refresh_seconds"] == "0"
    sources = payload["sources"]["rows"]
    assert len(sources) == 5
    bloomberg = next(row for row in sources if row["source_id"] == "news_bloomberg")
    assert bloomberg["latest_status"] == "ok"
    assert bloomberg["latest_item_count"] == 20
    rss = next(row for row in sources if row["value"] == "https://example.com/feed")
    assert rss["kind"] == "rss"
    assert rss["latest_status"] == "failed"


def test_update_agent_settings_config_rewrites_only_agents_block(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
database:
  duckdb_path: data/test.duckdb

agents:
  option_thesis:
    enabled: true
    command: old-thesis
    timeout_seconds: 180
    limit: 8
  option_postmortem:
    enabled: true
    command: old-postmortem
    timeout_seconds: 180
    limit: 4

disclosures:
  public_disclosure_csvs: []
""".lstrip(),
        encoding="utf-8",
    )

    update_agent_settings_config(
        config_path,
        {
            "option_thesis": {"enabled": False, "command": "new-thesis", "timeout_seconds": 90, "limit": 3},
            "option_postmortem": {"enabled": False, "limit": 0},
        },
    )

    text = config_path.read_text(encoding="utf-8")
    assert "duckdb_path: data/test.duckdb" in text
    assert "command: new-thesis" in text
    assert "limit: 3" in text
    assert "option_postmortem:" in text
    assert "limit: 0" in text
    assert "disclosures:" in text


def test_update_research_sources_config_rewrites_only_research_block(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
database:
  duckdb_path: data/test.duckdb

research_sources:
  x:
    enabled: true
    list_id: ""
    priority_handles: [balajis]
    limit: 30

disclosures:
  public_disclosure_csvs: []
""".lstrip(),
        encoding="utf-8",
    )

    update_research_sources_config(
        config_path,
        {
            "x": {"enabled": True, "list_id": "1734567890", "priority_handles": "@balajis, karpathy, karpathy", "limit": 40},
            "news": {"enabled": False, "providers": ["bloomberg", "reuters"]},
            "blogs": {"enabled": True, "substack_urls": ["https://example.substack.com"], "rss_urls": ["https://example.com/feed"]},
        },
    )

    text = config_path.read_text(encoding="utf-8")
    assert "duckdb_path: data/test.duckdb" in text
    assert "list_id: '1734567890'" in text or "list_id: \"1734567890\"" in text or "list_id: 1734567890" in text
    # @ stripped, de-duped
    assert "balajis" in text and "karpathy" in text
    assert text.count("karpathy") == 1
    assert "limit: 40" in text
    assert "https://example.substack.com" in text
    assert "https://example.com/feed" in text
    assert "disclosures:" in text


def test_update_research_sources_config_rejects_bad_values(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("research_sources:\n  x:\n    enabled: true\n", encoding="utf-8")
    with pytest.raises(ValueError):
        update_research_sources_config(config_path, {"x": {"limit": 9999}})


def test_update_agent_settings_endpoint_is_local_and_scoped(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "settings-api.duckdb"
    _use_temp_api_db(monkeypatch, db_path)
    captured: dict[str, Any] = {}

    def fake_update(config: dict[str, Any], section: str, payload: dict[str, Any]) -> None:
        captured["config"] = config
        captured["section"] = section
        captured["payload"] = payload

    monkeypatch.setattr(app_deps, "persist_setting_section", fake_update)
    monkeypatch.setattr(
        app_deps,
        "load_panel_data",
        lambda _config: PanelData(status=DataStatus(True, "loaded settings", "test"), tables={}),
    )

    client = TestClient(app)
    response = client.patch(
        "/api/settings/agents",
        json={"option_thesis": {"enabled": False, "command": "market-codex-option-thesis-agent", "timeout_seconds": 90, "limit": 3}},
    )

    assert response.status_code == 200
    assert captured["section"] == "agents"
    assert captured["payload"]["option_thesis"]["enabled"] is False
    assert response.json()["status"]["ready"] is True


def test_market_snapshot_only_returns_market_tables() -> None:
    client = TestClient(app)

    response = client.get("/api/panel-snapshot?scope=market")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload["tables"]) == {
        "market_valuation_reference_charts",
        "market_environment_assets",
        "market_environment_model",
    }


def test_settings_snapshot_returns_no_panel_tables() -> None:
    client = TestClient(app)

    response = client.get("/api/panel-snapshot?scope=settings")

    assert response.status_code == 200
    payload = response.json()
    assert payload["tables"] == {}
    assert payload["status"]["ready"] is True
    assert payload["status"]["source"] == "postgresql"


def test_options_radar_snapshot_returns_radar_tables() -> None:
    client = TestClient(app)

    response = client.get("/api/panel-snapshot?scope=options-radar")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload["tables"]) == set(panel_contracts.PANEL_SCOPE_TABLES["options-radar"])


def test_options_radar_snapshot_falls_back_to_last_good_payload(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "fallback-api.duckdb"
    _use_temp_api_db(monkeypatch, db_path)
    app_deps._LAST_GOOD_SCOPE_SNAPSHOTS.clear()
    calls = 0

    def fake_scope_loader(_config: dict[str, object], scope: str) -> PanelData:
        nonlocal calls
        calls += 1
        assert scope == "options-radar"
        if calls == 1:
            return PanelData(
                status=DataStatus(True, "loaded radar", "test"),
                tables={
                    "option_radar_summary": [{"latest_candidate_time": "2026-07-09T10:00:00"}],
                    "candidate_event": [{"event_id": "event-1"}],
                },
            )
        return PanelData(status=DataStatus(False, "DuckDB locked", "core-error"), tables={})

    monkeypatch.setattr(app_deps, "load_panel_scope_data", fake_scope_loader)

    client = TestClient(app)
    first = client.get("/api/panel-snapshot?scope=options-radar")
    assert first.status_code == 200
    assert first.json()["tables"]["candidate_event"]["rows"] == [{"event_id": "event-1"}]

    app_main._invalidate_context_cache()
    app_deps._LAST_GOOD_SCOPE_SNAPSHOTS.clear()
    second = client.get("/api/panel-snapshot?scope=options-radar")

    assert second.status_code == 200
    payload = second.json()
    assert payload["status"]["source"] == "panel-snapshot-cache"
    assert payload["tables"]["candidate_event"]["rows"] == [{"event_id": "event-1"}]


def test_table_endpoint_uses_scoped_loader(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "scoped-api.duckdb"
    _use_temp_api_db(monkeypatch, db_path)
    calls: list[str] = []

    def fake_table_loader(config: dict[str, object], table_name: str) -> PanelData:
        calls.append(table_name)
        return PanelData(
            status=DataStatus(True, "loaded scoped table", "test"),
            tables={table_name: [{"id": "feed-1", "title": "Scoped feed"}]},
        )

    monkeypatch.setattr(app_deps, "load_table_panel_data", fake_table_loader)

    client = TestClient(app)
    response = client.get("/api/feed")

    assert response.status_code == 200
    assert calls == ["feed_signals"]
    assert response.json()["rows"] == [{"id": "feed-1", "title": "Scoped feed"}]


def test_context_cache_does_not_hold_lock_while_loading(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "cache-lock.duckdb"
    _use_temp_api_db(monkeypatch, db_path)
    cached = PanelData(status=DataStatus(True, "cached", "test"), tables={"signals": [{"id": "cached"}]})
    slow_started = threading.Event()
    release_slow = threading.Event()
    cached_returned = threading.Event()
    errors: list[BaseException] = []

    app_deps._context(loader=lambda _config: cached)

    def slow_loader(_config: dict[str, object]) -> PanelData:
        slow_started.set()
        release_slow.wait(timeout=5)
        return PanelData(status=DataStatus(True, "slow", "test"), tables={})

    def run_slow_context() -> None:
        try:
            app_deps._context(cache_key="slow", loader=slow_loader)
        except BaseException as exc:  # pragma: no cover - threaded assertion capture
            errors.append(exc)

    slow_thread = threading.Thread(target=run_slow_context)
    slow_thread.start()
    assert slow_started.wait(timeout=1)

    def read_cached_context() -> None:
        try:
            _, panel_data = app_deps._context()
            assert panel_data is cached
            cached_returned.set()
        except BaseException as exc:  # pragma: no cover - threaded assertion capture
            errors.append(exc)

    cached_thread = threading.Thread(target=read_cached_context)
    cached_thread.start()
    assert cached_returned.wait(timeout=0.5)

    release_slow.set()
    slow_thread.join(timeout=1)
    cached_thread.join(timeout=1)
    assert not errors


def test_source_ticker_rankings_route_registered_once_and_uses_scoped_loader(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "source-rankings-api.duckdb"
    _use_temp_api_db(monkeypatch, db_path)
    calls: list[str] = []

    routes = [
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/source-ticker-rankings" and "GET" in getattr(route, "methods", set())
    ]

    def fake_table_loader(config: dict[str, object], table_name: str) -> PanelData:
        calls.append(table_name)
        return PanelData(
            status=DataStatus(True, "loaded scoped source rankings", "test"),
            tables={table_name: [{"symbol": "NVDA", "signal_count": 3, "rank_score": 42}]},
        )

    def fail_full_loader(config: dict[str, object]) -> PanelData:
        raise AssertionError("source ticker rankings should use the scoped table loader")

    monkeypatch.setattr(app_deps, "load_table_panel_data", fake_table_loader)
    monkeypatch.setattr(app_deps, "load_panel_data", fail_full_loader)

    client = TestClient(app)
    response = client.get("/api/source-ticker-rankings")

    assert len(routes) == 1
    assert response.status_code == 200
    assert calls == ["source_ticker_rankings"]
    assert response.json()["rows"] == [{"symbol": "NVDA", "signal_count": 3, "rank_score": 42}]


def test_source_ingestion_audit_get_is_read_only_and_does_not_sync(
    migrated_postgres_dsn: str, monkeypatch
) -> None:
    _use_postgres_api(monkeypatch, migrated_postgres_dsn)
    runtime = runtime_for_url(migrated_postgres_dsn)
    with runtime.transaction() as connection:
        connection.execute(
            "INSERT INTO ingest.source (id, name, family, kind) VALUES ('test-source', 'Test', 'test', 'fixture')"
        )
    with runtime.read() as connection:
        before = connection.execute("SELECT count(*) AS count FROM ingest.source").fetchone()["count"]

    client = TestClient(app)
    response = client.get("/api/source-ingestion-audit")

    assert response.status_code == 200
    with runtime.read() as connection:
        after = connection.execute("SELECT count(*) AS count FROM ingest.source").fetchone()["count"]
    assert after == before
    assert response.json()["status"] == "ok"
    assert response.json()["database"] == "postgresql"


def test_source_freshness_defaults_to_capped_browser_payload(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "source-freshness-api.duckdb"
    _use_temp_api_db(monkeypatch, db_path)
    rows = [
        {
            "source_key": f"source-{index:03d}",
            "freshness_status": "fresh",
            "status": "ok",
            "checked_at": "2026-06-11T12:00:00Z",
        }
        for index in range(125)
    ]

    def fake_table_loader(config: dict[str, object], table_name: str) -> PanelData:
        assert table_name == "source_freshness"
        return PanelData(
            status=DataStatus(True, "loaded source freshness", "test"),
            tables={table_name: rows},
        )

    monkeypatch.setattr(app_deps, "load_table_panel_data", fake_table_loader)

    client = TestClient(app)
    response = client.get("/api/source-freshness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 125
    assert payload["returned_count"] == 100
    assert payload["limit"] == 100
    assert len(payload["rows"]) == 100
    assert payload["rows"][0]["source_key"] == "source-000"
    assert payload["rows"][-1]["source_key"] == "source-099"


def test_refresh_job_launcher_rejects_unallowlisted_job() -> None:
    client = TestClient(app)
    response = client.post("/api/refresh-jobs/not-a-real-job")
    assert response.status_code == 400
    assert "allowlisted" in response.text


def test_refresh_jobs_exposes_options_radar_job(migrated_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app_deps,
        "load_config",
        lambda _path=None: {"database": {"url": migrated_postgres_dsn}},
    )
    client = TestClient(app)
    response = client.get("/api/refresh-jobs")

    assert response.status_code == 200
    assert "refresh_options_radar" in response.json()["allowlist"]


def test_api_startup_does_not_fail_recent_job_owned_by_another_process(
    migrated_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from investment_panel.database.jobs import JobRepository

    runtime = runtime_for_url(migrated_postgres_dsn)
    repository = JobRepository(runtime)
    job = repository.start("external-refresh")
    _use_postgres_api(monkeypatch, migrated_postgres_dsn)
    monkeypatch.setattr(app_main, "scheduler_enabled", lambda: False)

    with TestClient(app):
        pass

    repository = JobRepository(runtime_for_url(migrated_postgres_dsn))
    row = next(item for item in repository.rows() if item["id"] == job["id"])
    assert row["status"] == "running"


def test_agent_thesis_post_fulfills_request_and_validates(migrated_postgres_dsn: str, monkeypatch) -> None:
    _use_postgres_api(monkeypatch, migrated_postgres_dsn)
    repository = AgentRepository(runtime_for_url(migrated_postgres_dsn))
    queued = repository.queue_thesis("TSLA", trigger="manual")
    client = TestClient(app)
    response = client.post(
        "/api/agent-thesis",
        json={
            "request_id": queued["request_id"],
            "ticker": "TSLA",
            "strategy_version": DEFAULT_STRATEGY_VERSION,
            "created_at": "2026-06-03T12:00:00Z",
            "bull_target_price": 180,
            "bull_target_date": "2028-01-21",
            "base_target_price": 95,
            "core_thesis": "Energy storage and autonomy narrative returns while margins stabilize.",
            "required_proofs": ["gross margin stabilizes", "deliveries recover"],
            "catalysts": [{"type": "earnings", "what_to_watch": "margins and delivery guide"}],
            "invalidation": ["stock breaks below $80 without recovery"],
            "bear_case": "Demand weakness and pricing pressure can keep the stock below trend.",
            "confidence": 72,
            "evidence_refs": [{"type": "source_signal", "id": "source-tsla-api-proof"}],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["agent_thesis_validations"] == 1
    thesis = repository.rows("agent_thesis")[0]
    assert thesis["status"] == "completed"
    assert thesis["core_thesis"].startswith("Energy storage")


def test_agent_thesis_post_rejects_unstructured_payload(migrated_postgres_dsn: str, monkeypatch) -> None:
    _use_postgres_api(monkeypatch, migrated_postgres_dsn)
    queued = AgentRepository(runtime_for_url(migrated_postgres_dsn)).queue_thesis("TSLA", trigger="manual")
    client = TestClient(app)

    response = client.post("/api/agent-thesis", json={"request_id": queued["request_id"], "ticker": "TSLA", "bull_target_price": 180})

    assert response.status_code == 400
    assert "core_thesis" in response.text


def test_agent_postmortem_post_keeps_strategy_mutation_gated(migrated_postgres_dsn: str, monkeypatch) -> None:
    _use_postgres_api(monkeypatch, migrated_postgres_dsn)
    runtime = runtime_for_url(migrated_postgres_dsn)
    with runtime.transaction() as connection:
        instrument = connection.execute(
            "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('RBLX', 'RBLX', 'equity') RETURNING id"
        ).fetchone()
        run = connection.execute(
            "INSERT INTO analysis.run "
            "(run_type, input_cutoff, code_version, input_hash, started_at, finished_at, status) "
            "VALUES ('postmortem-test', now(), 'test', %s, now(), now(), 'succeeded') RETURNING id",
            ["0" * 64],
        ).fetchone()
        decision = connection.execute(
            "INSERT INTO analysis.decision (run_id, instrument_id, decision_key, kind, state, as_of, input_hash) "
            "VALUES (%s, %s, 'rblx-missed', 'option', 'missed', now(), %s) RETURNING id",
            [run["id"], instrument["id"], "1" * 64],
        ).fetchone()
    request = AgentRepository(runtime).queue_postmortem(decision["id"], reason="missed winner")
    client = TestClient(app)
    response = client.post(
        "/api/agent-postmortems",
        json={
                "request_id": request["request_id"],
                "ticker": "RBLX",
                "strategy_version": DEFAULT_STRATEGY_VERSION,
                "decision_id": request["decision_id"],
            "outcome_type": "missed_10x_winner",
            "failure_type": "delta_range_too_strict",
            "evidence": ["Contract was rejected for delta_outside_strategy_range before reaching 10x."],
            "proposed_rule_change": "Test a lower-delta sleeve for strong momentum reversals.",
            "proposed_parameter_changes": {"delta_min": 0.10, "candidate_note": "agent postmortem lower-delta sleeve"},
            "expected_effect": "Increase recall for lower-delta 10x winners.",
            "risk": "May increase false positives and earlier entries.",
            "confidence": 70,
                "evidence_refs": [{"type": "decision", "id": request["decision_id"]}],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["strategy_evaluations"] == 0
    postmortem = AgentRepository(runtime).rows("agent_postmortem")[0]
    assert postmortem["status"] == "completed"
    assert postmortem["failure_type"] == "delta_range_too_strict"


def test_strategy_mutation_promote_endpoint_requires_gates_and_approval(migrated_postgres_dsn: str, monkeypatch) -> None:
    _use_postgres_api(monkeypatch, migrated_postgres_dsn)
    runtime = runtime_for_url(migrated_postgres_dsn)
    with runtime.transaction() as connection:
        proposal_id = connection.execute(
            "INSERT INTO analysis.agent_task (task_kind, status, request, result) "
            "VALUES ('strategy_mutation_proposal', 'completed', %s, %s) RETURNING id",
            [Jsonb({"source": "test"}), Jsonb({"status": "backtest_required"})],
        ).fetchone()["id"]
    proposal_id = str(proposal_id)
    client = TestClient(app)
    blocked = client.post(
        f"/api/strategy-mutation-proposals/{proposal_id}/promote",
        json={"approved_by": "joe"},
    )

    assert blocked.status_code == 400
    assert "backtest" in blocked.text

    with runtime.transaction() as connection:
        connection.execute(
            "UPDATE analysis.agent_task SET result = %s WHERE id = %s",
            [Jsonb({"status": "forward_test_required"}), proposal_id],
        )

    forward_blocked = client.post(
        f"/api/strategy-mutation-proposals/{proposal_id}/promote",
        json={"approved_by": "joe"},
    )

    assert forward_blocked.status_code == 400
    assert "forward shadow test" in forward_blocked.text

    with runtime.transaction() as connection:
        connection.execute(
            "UPDATE analysis.agent_task SET result = %s WHERE id = %s",
            [Jsonb({"status": "approved", "proposed_strategy_version": "leap_10x_momentum_lottery__delta_max_delta_min"}), proposal_id],
        )
        candidate_id = connection.execute(
            "INSERT INTO analysis.strategy_revision "
            "(strategy_key, revision, name, status, parameters) "
            "VALUES ('leap_10x_momentum_lottery__delta_max_delta_min', 1, 'candidate', 'candidate', %s) RETURNING id",
            [Jsonb({})],
        ).fetchone()["id"]
        for evaluation_type in ("backtest", "forward_shadow_test"):
            connection.execute(
                "INSERT INTO analysis.strategy_evaluation "
                "(strategy_revision_id, evaluation_type, evaluated_at, verdict, metrics) "
                "VALUES (%s, %s, now(), 'pass', %s)",
                [candidate_id, evaluation_type, Jsonb({"sample_size": 100})],
            )

    unapproved = client.post(
        f"/api/strategy-mutation-proposals/{proposal_id}/promote",
        json={"approved_by": ""},
    )
    response = client.post(
        f"/api/strategy-mutation-proposals/{proposal_id}/promote",
        json={"approved_by": "joe"},
    )

    assert unapproved.status_code == 400
    assert "human approval" in unapproved.text
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "promoted"
    assert payload["proposal_id"] == proposal_id
    assert payload["strategy_version"] == "leap_10x_momentum_lottery__delta_max_delta_min"
    with runtime.read() as connection:
        validation = connection.execute(
            "SELECT validation FROM analysis.agent_task WHERE id = %s", [proposal_id]
        ).fetchone()["validation"]
        strategy = connection.execute(
            "SELECT strategy_key, status FROM analysis.strategy_revision WHERE strategy_key = %s",
            [payload["strategy_version"]],
        ).fetchone()
    assert validation == {"status": "promoted", "approved_by": "joe"}
    assert dict(strategy) == {"strategy_key": payload["strategy_version"], "status": "active"}


def test_local_write_guard_allows_private_lan_clients() -> None:
    _require_local_request(SimpleNamespace(client=SimpleNamespace(host="100.120.95.8")))
    _require_local_request(SimpleNamespace(client=SimpleNamespace(host="192.168.50.197")))
    _require_local_request(SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")))

    with pytest.raises(HTTPException):
        _require_local_request(SimpleNamespace(client=SimpleNamespace(host="8.8.8.8")))


def test_ticker_decision_brief_prefers_quote_row_over_decision_snapshot_price() -> None:
    brief = ticker_decision_brief(
        "AMD",
        {
            "quotes": [
                {
                    "symbol": "AMD",
                    "price": 424.10,
                    "change_pct": -5.69,
                    "observed_at": "2026-05-15T00:00:00",
                    "source": "previous_close:yahoo-chart",
                }
            ],
            "symbol_decision_snapshot": [
                {
                    "symbol": "AMD",
                    "action_grade": "Reject",
                    "freshness_status": "fresh",
                    "latest_quote": 455.19,
                    "blocking_gates": ["chart_extended_without_thesis"],
                    "decision_basis": {"summary": "snapshot price should not be canonical"},
                }
            ],
        },
    )

    assert brief["canonical_quote"]["price"] == 424.10
    assert brief["canonical_quote"]["source"] == "previous_close:yahoo-chart"
    assert brief["canonical_quote"]["type"] == "prior_close"
    assert brief["verdict"]["blockers"] == ["chart_extended_without_thesis", "decision_reject"]
    assert brief["setup"]["entry_zone"] == "No entry while the decision grade is Reject."
    assert brief["risk_plan"]["max_sizing"] == "No new exposure while decision grade remains Reject."


def test_ticker_decision_brief_surfaces_missing_thesis_news_and_filings() -> None:
    brief = ticker_decision_brief(
        "AMD",
        {
            "quotes": [{"symbol": "AMD", "price": 424.10, "source": "previous_close:yahoo-chart"}],
            "symbol_decision_snapshot": [
                {
                    "symbol": "AMD",
                    "action_grade": "Reject",
                    "freshness_status": "fresh",
                    "blocking_gates": ["chart_extended_without_thesis"],
                    "snapshot": {"invalidation": "Needs a verified thesis."},
                }
            ],
            "technicals": [{"symbol": "AMD", "technical_score": 99.8, "return_20d": 0.52, "ma50": 279.2}],
            "sepa": [{"symbol": "AMD", "verdict": "strong_setup", "stage": "stage_2_advancing"}],
            "liquidity": [{"symbol": "AMD", "grade": "very_high", "avg_dollar_volume": 10_000_000_000}],
            "valuations": [{"symbol": "AMD", "method": "relative", "fair_value": 407.79, "upside_pct": -10.41}],
            "options_payoff_scenarios": [{"symbol": "AMD", "strategy_type": "call_debit_spread", "max_loss": -165, "expiry": "2026-05-15"}],
            "research_packets": [{"symbol": "AMD", "decision": "monitor", "why_now": ["Technical setup is constructive."]}],
        },
    )

    assert any("Technical score" in item for item in brief["evidence_for"])
    assert any("valuation" in item.lower() for item in brief["evidence_against"])
    assert any("Optional thesis" in item for item in brief["unknowns"])
    assert any("No ticker-specific news row" in item for item in brief["unknowns"])
    assert any("No tracked disclosure row" in item for item in brief["unknowns"])
    assert brief["risk_plan"]["max_loss"] == "Not applicable while decision grade is Reject."
    assert brief["options_context"]["status"] == "expired"
    assert "expired_options_context" in brief["verdict"]["blockers"]
    assert any("Options context is expired" in item for item in brief["verdict"]["blocker_labels"])
    assert {row["label"] for row in brief["tab_summaries"]["Evidence Stack"]} == {"For", "Against", "Open Inputs"}


def test_ticker_decision_brief_uses_specific_source_gap_language() -> None:
    brief = ticker_decision_brief(
        "MU",
        {
            "symbol_decision_snapshot": [
                {
                    "symbol": "MU",
                    "action_grade": "Watch",
                    "blocking_gates": ["liquidity_unknown", "missing_daily_analysis", "stale_intraday_quote"],
                    "decision_basis": {"source_count": 3, "evidence_count": 1},
                }
            ],
        },
    )

    joined = " ".join(
        [
            brief["verdict"]["summary"],
            " ".join(brief["verdict"]["blocker_labels"]),
            " ".join(brief["evidence_against"]),
            " ".join(brief["unknowns"]),
        ]
    )
    assert "Liquidity unknown" not in joined
    assert "Missing daily analysis" not in joined
    assert "No explicit" not in joined
    assert "No major missing" not in joined
    assert "No current liquidity row is loaded for this ticker." in joined
    assert "Daily analysis rows are not loaded for this ticker." in joined
    assert brief["risk_plan"]["max_loss"] == "Not applicable while blockers are active."
    assert brief["risk_plan"]["max_sizing"] == "No new exposure until evidence gates clear."


def test_frontend_fallback_serves_spa_deep_links_after_build() -> None:
    dist_index = Path(__file__).resolve().parents[1] / "frontend" / "dist" / "index.html"
    if not dist_index.exists():
        pytest.skip("frontend build output is not present")

    client = TestClient(app)
    for path in [
        "/",
        "/feed",
        "/today",
        "/dashboard",
        "/watchlist",
        "/sources",
        "/superinvestors",
        "/market",
        "/opportunities",
        "/portfolio",
        "/research",
        "/research-queue",
        "/thesis-monitor",
        "/filings",
        "/calendar",
        "/health",
        "/settings",
        "/tickers/NVDA",
        "/not-a-market-route",
    ]:
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["content-type"].startswith("text/html")
        assert '<div id="root">' in response.text
