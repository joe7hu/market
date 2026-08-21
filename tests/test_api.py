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

from investment_panel.database.options_constants import DEFAULT_STRATEGY_VERSION
from investment_panel.database.authority import close_cached_runtimes
from investment_panel.database.agents import AgentRepository
from investment_panel.database.authority import runtime_for_url
from investment_panel.database.migrations import upgrade_database
from app.data_access.settings import settings_payload
from app.data_access.types import DataStatus, PanelData
from app.data_access import loaders as loaders_owner, settings as settings_owner
from app import job_control
from app import dependencies
import app.panel_snapshot as panel_owner
import app.main as app_main
from app.main import app
from app.request_security import require_local_request
from investment_panel.core.panel import PANEL_SCOPE_TABLES
from investment_panel.core.decision import ticker_decision_brief
from investment_panel.core.config import AppConfig
from investment_panel.core.config_mutations import update_agent_settings_config, update_research_sources_config
from conftest import typed_config


def _use_temp_api_db(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    panel_owner.invalidate_context_cache()
    config = typed_config(status_dir=db_path.parent / "status")
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_config, lambda: config)
    monkeypatch.setattr(panel_owner, "load_config", lambda: config)


def _use_postgres_api(monkeypatch: pytest.MonkeyPatch, dsn: str) -> None:
    panel_owner.invalidate_context_cache()
    config = typed_config(dsn)
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_config, lambda: config)
    monkeypatch.setattr(panel_owner, "load_config", lambda: config)


def test_api_routes_return_json(postgresql, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    info = postgresql.info
    credentials = info.user if not info.password else f"{info.user}:{info.password}"
    postgres_dsn = f"postgresql://{credentials}@{info.host}:{info.port}/{info.dbname}"
    upgrade_database(postgres_dsn)
    _use_postgres_api(monkeypatch, postgres_dsn)
    client = TestClient(app)
    try:
        paths = [
            "/api/status",
            "/api/agent",
            "/api/agent/experiments/current",
            "/api/agent/research-prompt",
            "/api/panel-contract",
            "/api/panel-snapshot?scope=feed",
            "/api/panel-snapshot?scope=watchlist",
            "/api/panel-snapshot?scope=sources",
            "/api/panel-snapshot?scope=superinvestors",
            "/api/panel-snapshot?scope=market",
            "/api/panel-snapshot?scope=options-radar",
            "/api/panel-snapshot?scope=today",
            "/api/panel-snapshot?scope=dashboard",
            "/api/quotes?symbols=TSLA",
            "/api/options/history/snapshots",
            "/api/options/history/symbols",
            "/api/options/history/chain",
            "/api/options/history/surface?expiration=2026-08-21&option_type=call",
            "/api/options/history/surface-groups",
            "/api/options/history/surface-grid?option_type=call",
            "/api/options/history/curves",
            "/api/options/history/anomalies",
            "/api/options/history/health",
            "/api/options/history/relative-values",
            "/api/options/decision-brief",
            "/api/options/candidates",
            "/api/options/paper-journal",
            "/api/options/shadow-observations",
            "/api/options/event-study?ticker=TSLA&event_kind=earnings&as_of=2026-08-21T00:00:00Z",
            "/api/options/history/distribution-shift?ticker=TSLA&as_of=2026-08-21T00:00:00Z",
            "/api/options/workspace",
            "/api/decision-inbox",
            "/api/opportunity-scorecard?lane=radar&window=120",
            "/api/health/options-recovery",
            "/api/health/storage",
            "/api/event-scout",
            "/api/event-scout/packets",
            "/api/event-scout/replay",
            "/api/source-catalog",
            "/api/source-ingestion-audit",
            "/api/sources/sec_edgar",
            "/api/refresh-jobs",
            "/api/settings",
            "/api/tickers/TSLA",
            "/api/tickers/TSLA/decision-snapshot",
            "/api/portfolio/transactions",
            "/api/theses/TSLA/history",
        ]
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers["content-type"].startswith("application/json")
        agent_overview = client.get("/api/agent").json()
        assert agent_overview["materialization"]["historical_unmaterialized"] == 0
        assert client.get("/api/agent/recommendations").status_code == 404
        assert client.get("/api/paper-orders").status_code == 404
    finally:
        close_cached_runtimes()


def test_removed_compatibility_routes_return_404() -> None:
    client = TestClient(app)
    removed = [
        ("GET", "/api/dashboard"),
        ("GET", "/api/decision-truth"),
        ("GET", "/api/options/history/surface/legacy"),
        ("GET", "/api/options-chain"),
        ("GET", "/api/options-expiries"),
        ("GET", "/api/portfolio"),
        ("GET", "/api/portfolio/summary"),
        ("GET", "/api/portfolio/performance"),
        ("POST", "/api/portfolio/positions"),
        ("GET", "/api/paper-orders"),
        ("GET", "/api/broker/status"),
        ("GET", "/api/etf-premiums"),
        ("GET", "/api/tradingview-chart-state"),
        ("GET", "/api/watchlist-screen"),
    ]
    for method, path in removed:
        response = client.request(method, path)
        assert response.status_code == 404, f"{method} {path} returned {response.status_code}"


def test_settings_payload_includes_agent_control_metadata() -> None:
    payload = settings_payload(
        typed_config(
            "postgresql:///test",
            raw={
            "database": {"url": "postgresql:///test"},
            "research_sources": {
                "x": {"enabled": True, "list_id": "123", "priority_handles": ["balajis"]},
                "news": {"enabled": True, "providers": ["bloomberg"]},
                "blogs": {"enabled": True, "substack_urls": ["https://example.substack.com"], "rss_urls": ["https://example.com/feed"]},
            },
            "agents": {
                "option_agent": {
                    "enabled": True,
                    "command": "market-run-option-agent",
                    "timeout_seconds": 180,
                    "thesis_limit": 8,
                    "postmortem_limit": 2,
                    "provider": "codex",
                },
            },
            },
        ),
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

    assert payload["agents"]["config"]["option_agent"]["thesis_limit"] == 8
    assert payload["agents"]["runtime"]["option_agent"]["active"] is True
    assert payload["agents"]["runtime"]["option_agent"]["postmortem_limit"] == 2
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
  url: postgresql:///test

agents:
  option_agent:
    enabled: true
    command: market-run-option-agent
    timeout_seconds: 180
    thesis_limit: 8
    postmortem_limit: 4
    provider: codex

disclosures:
  public_disclosure_csvs: []
""".lstrip(),
        encoding="utf-8",
    )

    update_agent_settings_config(
        config_path,
        {
            "option_agent": {
                "enabled": False,
                "command": "market-run-option-agent",
                "timeout_seconds": 90,
                "thesis_limit": 3,
                "postmortem_limit": 0,
                "provider": "codex",
            },
        },
    )

    text = config_path.read_text(encoding="utf-8")
    assert "url: postgresql:///test" in text
    assert "command: market-run-option-agent" in text
    assert "thesis_limit: 3" in text
    assert "postmortem_limit: 0" in text
    assert "disclosures:" in text


def test_update_research_sources_config_rewrites_only_research_block(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
database:
  url: postgresql:///test

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
    assert "url: postgresql:///test" in text
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
    db_path = tmp_path / "settings-api.json"
    _use_temp_api_db(monkeypatch, db_path)
    captured: dict[str, Any] = {}

    def fake_update(config: AppConfig, section: str, payload: dict[str, Any]) -> None:
        captured["config"] = config
        captured["section"] = section
        captured["payload"] = payload

    monkeypatch.setattr(settings_owner, "persist_setting_section", fake_update)
    monkeypatch.setattr(
        loaders_owner,
        "load_panel_data",
        lambda _config, **_kwargs: PanelData(status=DataStatus(True, "loaded settings", "test"), tables={}),
    )

    client = TestClient(app)
    response = client.patch(
        "/api/settings/agents",
        json={
            "option_agent": {
                "enabled": False,
                "command": "market-run-option-agent",
                "timeout_seconds": 90,
                "thesis_limit": 3,
                "provider": "codex",
            },
        },
    )

    assert response.status_code == 200
    assert captured["section"] == "agents"
    assert captured["payload"]["option_agent"]["enabled"] is False
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


def test_ticker_route_reuses_cached_snapshot(monkeypatch) -> None:
    panel_owner.invalidate_context_cache()
    calls = 0

    def loader(_config, _ticker):
        nonlocal calls
        calls += 1
        return PanelData(
            status=DataStatus(True, "ticker", "postgresql"),
            tables={"quotes": [{"symbol": "NVDA", "price": 175}]},
        )

    _use_postgres_api(monkeypatch, "postgresql:///cached")
    monkeypatch.setattr(loaders_owner, "load_ticker_panel_data", loader)
    client = TestClient(app)
    assert client.get("/api/tickers/NVDA").status_code == 200
    assert client.get("/api/tickers/NVDA").status_code == 200
    assert calls == 1


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
    assert set(payload["tables"]) == set(PANEL_SCOPE_TABLES["options-radar"])


def test_options_radar_snapshot_falls_back_to_last_good_payload(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "fallback-api.json"
    _use_temp_api_db(monkeypatch, db_path)
    panel_owner._LAST_GOOD_SCOPE_SNAPSHOTS.clear()
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
                    "option_radar_opportunity": [{"decision_id": "event-1"}],
                },
            )
        return PanelData(status=DataStatus(False, "PostgreSQL unavailable", "postgresql-error"), tables={})

    monkeypatch.setattr(loaders_owner, "load_panel_scope_data", fake_scope_loader)

    client = TestClient(app)
    first = client.get("/api/panel-snapshot?scope=options-radar")
    assert first.status_code == 200
    assert first.json()["tables"]["option_radar_opportunity"]["rows"] == [{"decision_id": "event-1"}]

    panel_owner.invalidate_context_cache()
    panel_owner._LAST_GOOD_SCOPE_SNAPSHOTS.clear()
    second = client.get("/api/panel-snapshot?scope=options-radar")

    assert second.status_code == 200
    payload = second.json()
    assert payload["status"]["source"] == "panel-snapshot-cache"
    assert payload["tables"]["option_radar_opportunity"]["rows"] == [{"decision_id": "event-1"}]


def test_watchlist_snapshot_returns_error_when_no_current_or_last_good_payload(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "unavailable-watchlist-api.json"
    _use_temp_api_db(monkeypatch, db_path)
    panel_owner._LAST_GOOD_SCOPE_SNAPSHOTS.clear()
    panel_owner._scope_snapshot_cache_path(typed_config(), "watchlist").unlink(missing_ok=True)
    monkeypatch.setattr(
        loaders_owner,
        "load_panel_scope_data",
        lambda _config, _scope: PanelData(status=DataStatus(False, "PostgreSQL timed out", "postgresql-error"), tables={}),
    )

    response = TestClient(app).get("/api/panel-snapshot?scope=watchlist")

    assert response.status_code == 503
    assert response.json()["detail"] == "PostgreSQL timed out"


def test_options_radar_ready_empty_snapshot_does_not_claim_postgres_is_unavailable(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "ready-empty-api.json"
    _use_temp_api_db(monkeypatch, db_path)
    panel_owner._LAST_GOOD_SCOPE_SNAPSHOTS.clear()
    panel_owner._LAST_GOOD_SCOPE_SNAPSHOTS["options-radar"] = {
        "status": {"ready": True, "source": "old"},
        "tables": {"option_radar_opportunity": {"rows": [{"decision_id": "stale"}], "count": 1}},
    }

    monkeypatch.setattr(
        loaders_owner,
        "load_panel_scope_data",
        lambda _config, _scope: PanelData(
            status=DataStatus(True, "PostgreSQL loaded; no current candidates", "postgresql"),
            tables={"option_strategy_versions": [{"strategy_version": "active-v1"}]},
        ),
    )

    response = TestClient(app).get("/api/panel-snapshot?scope=options-radar")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"]["source"] == "postgresql"
    assert "option_strategy_versions" not in payload["tables"]
    assert payload["tables"]["option_radar_opportunity"]["rows"] == []


def test_scope_snapshot_rejects_known_stale_schema_cache(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "stale-schema-api.json"
    _use_temp_api_db(monkeypatch, db_path)
    panel_owner._LAST_GOOD_SCOPE_SNAPSHOTS.clear()
    monkeypatch.setattr(panel_owner, "_scope_snapshot_cache_path", lambda *_args: tmp_path / "missing-cache.json")
    panel_owner._LAST_GOOD_SCOPE_SNAPSHOTS["today"] = {
        "status": {
            "ready": True,
            "source": "old",
            "metadata": {"schema_revision": "20260803_0025"},
        },
        "tables": {"portfolio": {"rows": [{"symbol": "TSLA"}], "count": 1}},
    }
    monkeypatch.setattr(
        loaders_owner,
        "load_panel_scope_data",
        lambda _config, _scope: PanelData(status=DataStatus(False, "PostgreSQL timed out", "postgresql-error"), tables={}),
    )

    response = TestClient(app).get("/api/panel-snapshot?scope=today")

    assert response.status_code == 503
    assert response.json()["detail"] == "PostgreSQL timed out"


def test_scope_snapshot_rejects_known_schema_cache_with_old_panel_contract(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "stale-contract-api.json"
    _use_temp_api_db(monkeypatch, db_path)
    panel_owner._LAST_GOOD_SCOPE_SNAPSHOTS.clear()
    monkeypatch.setattr(panel_owner, "_scope_snapshot_cache_path", lambda *_args: tmp_path / "missing-cache.json")
    panel_owner._LAST_GOOD_SCOPE_SNAPSHOTS["today"] = {
        "status": {
            "ready": True,
            "source": "old",
            "metadata": {
                "schema_revision": panel_owner.HEAD_REVISION,
                "panel_contract_revision": "obsolete-contract",
            },
        },
        "tables": {"portfolio": {"rows": [{"symbol": "TSLA"}], "count": 1}},
    }
    monkeypatch.setattr(
        loaders_owner,
        "load_panel_scope_data",
        lambda _config, _scope: PanelData(status=DataStatus(False, "PostgreSQL timed out", "postgresql-error"), tables={}),
    )

    response = TestClient(app).get("/api/panel-snapshot?scope=today")

    assert response.status_code == 503
    assert response.json()["detail"] == "PostgreSQL timed out"


def test_context_cache_does_not_hold_lock_while_loading(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "cache-lock.json"
    _use_temp_api_db(monkeypatch, db_path)
    cached = PanelData(status=DataStatus(True, "cached", "test"), tables={"signals": [{"id": "cached"}]})
    slow_started = threading.Event()
    release_slow = threading.Event()
    cached_returned = threading.Event()
    errors: list[BaseException] = []

    panel_owner.context(loader=lambda _config: cached)

    def slow_loader(_config: dict[str, object]) -> PanelData:
        slow_started.set()
        release_slow.wait(timeout=5)
        return PanelData(status=DataStatus(True, "slow", "test"), tables={})

    def run_slow_context() -> None:
        try:
            panel_owner.context(cache_key="slow", loader=slow_loader)
        except BaseException as exc:  # pragma: no cover - threaded assertion capture
            errors.append(exc)

    slow_thread = threading.Thread(target=run_slow_context)
    slow_thread.start()
    assert slow_started.wait(timeout=1)

    def read_cached_context() -> None:
        try:
            _, panel_data = panel_owner.context()
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


def test_refresh_job_launcher_rejects_unallowlisted_job() -> None:
    client = TestClient(app)
    response = client.post("/api/refresh-jobs/not-a-real-job")
    assert response.status_code == 400
    assert "allowlisted" in response.text


def test_refresh_jobs_exposes_options_radar_job(migrated_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_postgres_api(monkeypatch, migrated_postgres_dsn)
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
                "direction": "long",
                "strategy_version": DEFAULT_STRATEGY_VERSION,
            "created_at": "2026-06-03T12:00:00Z",
            "bull_target_price": 180,
            "bull_target_date": "2028-01-21",
            "base_target_price": 95,
            "bear_target_price": 65,
            "scenario_probabilities": {"base": 0.55, "bull": 0.25, "bear": 0.20},
            "preferred_structures": ["long_call", "call_debit_spread"],
            "core_thesis": "Energy storage and autonomy narrative returns while margins stabilize.",
            "required_proofs": ["gross margin stabilizes", "deliveries recover"],
            "catalysts": [{"type": "earnings", "expected_window": "next quarter", "what_to_watch": "margins and delivery guide"}],
            "invalidation": ["stock breaks below $80 without recovery"],
            "bear_case": "Demand weakness and pricing pressure can keep the stock below trend.",
            "confidence": 0.72,
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
    with repository.runtime.read() as connection:
        expression = connection.execute(
            """
            SELECT expression.structure, task.validation
            FROM app.thesis_expression expression
            JOIN app.thesis thesis ON thesis.id = expression.thesis_revision_id
            JOIN analysis.agent_task task ON task.id = %s
            WHERE thesis.status = 'current' AND expression.status = 'active'
            """,
            [queued["request_id"]],
        ).fetchone()
    assert expression["structure"]["preferred_structures"] == ["long_call", "call_debit_spread"]
    assert expression["validation"]["materialization"]["status"] == "materialized"


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
        strategy = connection.execute(
            "INSERT INTO analysis.strategy_revision "
            "(strategy_key, revision, name, status, parameters, authority_group, promoted_at) "
            "VALUES (%s, 1, %s, 'active', %s, 'options-radar-core', now()) RETURNING id",
            [
                "options-radar-core",
                "options-radar-core",
                Jsonb({"delta_min": 0.20, "dte_min": 14, "dte_max": 900}),
            ],
        ).fetchone()
        decision = connection.execute(
            "INSERT INTO analysis.decision "
            "(run_id, instrument_id, decision_key, kind, state, as_of, input_hash, strategy_revision_id) "
            "VALUES (%s, %s, 'rblx-missed', 'option', 'missed', now(), %s, %s) RETURNING id",
            [run["id"], instrument["id"], "1" * 64, strategy["id"]],
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
            "proposed_strategy_version": DEFAULT_STRATEGY_VERSION,
            "expected_effect": "Increase recall for lower-delta 10x winners.",
            "risk": "May increase false positives and earlier entries.",
            "confidence": 70,
                "evidence_refs": [{"type": "decision", "id": request["decision_id"]}],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["strategy_proposals"] == 1
    assert payload["strategy_evaluations"] == 2
    assert payload["strategy_backtests"] == 1
    assert payload["strategy_forward_tests"] == 1
    postmortem = AgentRepository(runtime).rows("agent_postmortem")[0]
    assert postmortem["status"] == "completed"
    assert postmortem["failure_type"] == "delta_range_too_strict"
    with runtime.read() as connection:
        proposal = connection.execute(
            "SELECT id, result FROM analysis.agent_task WHERE task_kind = 'strategy_mutation_proposal'"
        ).fetchone()
        evaluations = connection.execute(
            "SELECT evaluation_type, verdict FROM analysis.strategy_evaluation ORDER BY evaluation_type"
        ).fetchall()
    assert proposal["result"]["status"] == "backtest_required"
    assert [row["evaluation_type"] for row in evaluations] == ["backtest", "forward_shadow_test"]
    assert {row["verdict"] for row in evaluations} == {
        "requires_rejected_or_shadow_outcomes", "collecting_data",
    }
    assert proposal["result"]["proposed_strategy_version"] != DEFAULT_STRATEGY_VERSION
    with runtime.read() as connection:
        active = connection.execute(
            "SELECT status, parameters FROM analysis.strategy_revision WHERE id = %s",
            [strategy["id"]],
        ).fetchone()
    assert active["status"] == "active"
    assert active["parameters"]["delta_min"] == 0.20

    ready_result = {**dict(proposal["result"]), "status": "ready"}
    with runtime.transaction() as connection:
        connection.execute(
            "UPDATE analysis.agent_task SET result = %s WHERE id = %s",
            [Jsonb(ready_result), proposal["id"]],
        )
        connection.execute(
            "UPDATE analysis.strategy_evaluation SET verdict = 'pass' "
            "WHERE strategy_revision_id = %s",
            [ready_result["candidate_revision_id"]],
        )
    promoted = client.post(
        f"/api/strategy-mutation-proposals/{proposal['id']}/promote",
        json={"approved_by": "joe"},
    )
    assert promoted.status_code == 200
    assert promoted.json()["strategy_version"] == ready_result["proposed_strategy_version"]


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
        base_id = connection.execute(
            "INSERT INTO analysis.strategy_revision "
            "(strategy_key, revision, name, status, parameters, authority_group, promoted_at) "
            "VALUES ('options-radar-core', 1, 'core', 'active', %s, "
            "'options-radar-core', now()) RETURNING id",
            [Jsonb({})],
        ).fetchone()["id"]
        candidate_id = connection.execute(
            "INSERT INTO analysis.strategy_revision "
            "(strategy_key, revision, name, status, parameters, supersedes_id, authority_group) "
            "VALUES ('leap_10x_momentum_lottery__delta_max_delta_min', 1, 'candidate', "
            "'candidate', %s, %s, 'options-radar-core') RETURNING id",
            [Jsonb({}), base_id],
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
    assert payload["radar_refresh"]["status"] == "ok"
    assert payload["radar_refresh"]["reason"] == "legacy_publication_replaced"
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
    require_local_request(SimpleNamespace(client=SimpleNamespace(host="100.120.95.8")))
    require_local_request(SimpleNamespace(client=SimpleNamespace(host="192.168.50.197")))
    require_local_request(SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")))

    with pytest.raises(HTTPException):
        require_local_request(SimpleNamespace(client=SimpleNamespace(host="8.8.8.8")))


def test_thesis_monitor_automation_accepts_symbol_scoped_background_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _use_postgres_api(monkeypatch, "postgresql://test/market")
    monkeypatch.setattr(panel_owner, "invalidate_context_cache", lambda: None)
    monkeypatch.setattr(
        job_control,
        "execute_thesis_monitor_automation",
        lambda symbols, *, dry_run, force: calls.append({"symbols": symbols, "dry_run": dry_run, "force": force}),
    )

    response = TestClient(app).post(
        "/api/thesis-monitor/automation",
        json={"symbols": ["msft", "LLY", "msft"], "dry_run": True, "force": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["job"]["status"] == "accepted"
    assert payload["symbols"] == ["LLY", "MSFT"]
    assert calls == [{"symbols": ["LLY", "MSFT"], "dry_run": True, "force": False}]


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


def test_ticker_decision_brief_keeps_current_option_signal_visible_without_strategy() -> None:
    brief = ticker_decision_brief(
        "NBIS",
        {
            "options_ticker_signals": [
                {"symbol": "NBIS", "atm_iv": 103.9, "expected_move_pct": 32.6, "nearest_expiry": "2026-09-18"}
            ],
            "thesis_monitor": [{"symbol": "NBIS", "thesis": "Verified event thesis."}],
        },
    )

    assert brief["options_context"]["status"] == "signal"
    assert "32.60%" in brief["options_context"]["summary"]
    assert brief["source_health_by_family"]["options"]["status"] == "live"


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
