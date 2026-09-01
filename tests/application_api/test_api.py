from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import threading
from typing import Any
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

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
import app.routers.system as system_owner
from app.main import app
from app.request_security import require_local_request
from investment_panel.core.panel import PANEL_SCOPE_TABLES
from investment_panel.core.decision import TRACKED_METRICS, ticker_decision_brief
from investment_panel.core.config import AppConfig
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


def _seed_phase7_paper_provenance(connection: Any, candidate_id: int, instrument_id: int, sample: int = 30) -> tuple[list[str], list[str]]:
    run_id = connection.execute(
        "INSERT INTO analysis.run "
        "(run_type, input_cutoff, code_version, input_hash, started_at, finished_at, status, strategy_revision_id) "
        "VALUES ('phase7-evidence', now(), 'test', %s, now(), now(), 'succeeded', %s) RETURNING id",
        ["0" * 64, candidate_id],
    ).fetchone()["id"]
    paper_order_ids: list[str] = []
    decision_ids: list[str] = []
    for index in range(sample):
        decision_id = connection.execute(
            "INSERT INTO analysis.decision "
            "(run_id, instrument_id, decision_key, kind, state, as_of, input_hash, strategy_revision_id) "
            "VALUES (%s, %s, %s, 'option', 'resolved', now(), %s, %s) RETURNING id",
            [run_id, instrument_id, f"phase7-api-{uuid4().hex}-{index}", "1" * 64, candidate_id],
        ).fetchone()["id"]
        paper_order_id = connection.execute(
            "INSERT INTO app.paper_order "
            "(decision_id, instrument_id, side, quantity, limit_price, status, paper_only, "
            "filled_at, actual_fill_price, exit_at, exit_price, filled_quantity, exited_quantity, "
            "fees, entry_slippage, exit_slippage, lane) "
            "VALUES (%s, %s, 'buy', 1, 100, 'exited', TRUE, now(), 100, now(), 110, 1, 1, 0.5, 0.1, 0.1, 'ticker') "
            "RETURNING id",
            [decision_id, instrument_id],
        ).fetchone()["id"]
        paper_order_ids.append(str(paper_order_id))
        decision_ids.append(str(decision_id))
    return paper_order_ids, decision_ids


def test_today_uses_published_capital_actions_without_reloading_ticker_dossiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_temp_api_db(monkeypatch, tmp_path / "status.json")
    monkeypatch.setitem(
        app.dependency_overrides,
        dependencies.get_options_actions,
        lambda: SimpleNamespace(decision_inbox=lambda **_kwargs: {"items": []}),
    )
    capital = {
        "ticker": "ACME",
        "action": "BUY",
        "owned": False,
        "rationale": "Aligned views.",
        "price_condition": "100-105",
        "catalyst": "earnings",
        "expires_at": "2026-09-18",
    }
    panel = PanelData(
        status=DataStatus(True, "loaded", "test"),
        tables={
            "ticker_decisions": [{
                "ticker": "ACME",
                "decision_revision": "ticker-decision.v1:test",
                "capital_action": capital,
                "selected_expression": {"kind": "STOCK"},
                "as_of": "2026-08-23T13:00:00Z",
            }],
            "portfolio": [],
        },
    )
    monkeypatch.setattr(loaders_owner, "load_panel_scope_data", lambda _config, _scope: panel)
    monkeypatch.setattr(
        loaders_owner,
        "load_ticker_panel_data",
        lambda *_args, **_kwargs: pytest.fail("Today must not reload ticker dossiers"),
    )

    response = TestClient(app).get("/api/today")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 0
    assert payload["actions"] == []
    assert payload["missing_plan_count"] == 1
    assert payload["book_actions"][0]["ticker"] == "ACME"
    assert payload["book_actions"][0]["action"] == "NO_TRADE"
    assert payload["book_actions"][0]["selected_expression"] == "CASH"
    assert payload["book_actions"][0]["primary_blocker"] == "trade_plan_missing"
    assert payload["book_actions"][0]["projection_identity"].startswith("capital:ticker-decision:")
    assert payload["book_actions"][0]["resolution"]["action"] == "NO_TRADE"
    assert payload["book_actions"][0]["field_states"] == [{
        "field": "trade_plan",
        "availability_status": "missing",
        "source": "trade_plan",
        "reason": "trade_plan_missing",
        "blocking": True,
        "next_action": "Refresh the ticker decision and publish its canonical TradePlan.",
    }]


def test_today_and_snapshot_share_one_authoritative_load_and_invalidate_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_temp_api_db(monkeypatch, tmp_path / "today-shared-cache.json")
    monkeypatch.setitem(
        app.dependency_overrides,
        dependencies.get_options_actions,
        lambda: SimpleNamespace(decision_inbox=lambda **_kwargs: {"items": []}),
    )
    loads = 0
    panel = PanelData(status=DataStatus(True, "loaded", "test"), tables={})

    def load_today(_config: AppConfig, _scope: str, **_kwargs: Any) -> PanelData:
        nonlocal loads
        loads += 1
        return panel

    monkeypatch.setattr(loaders_owner, "load_panel_scope_data", load_today)
    client = TestClient(app)

    today_response = client.get("/api/today")
    snapshot_response = client.get("/api/panel-snapshot?scope=today")

    assert today_response.status_code == 200
    assert snapshot_response.status_code == 200
    assert loads == 1

    panel_owner.invalidate_context_cache()
    refreshed = client.get("/api/today")

    assert refreshed.status_code == 200
    assert loads == 2


def test_today_projects_named_context_contract_without_row_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_temp_api_db(monkeypatch, tmp_path / "today-context.json")
    monkeypatch.setitem(
        app.dependency_overrides,
        dependencies.get_options_actions,
        lambda: SimpleNamespace(decision_inbox=lambda **_kwargs: {"items": []}),
    )
    panel = PanelData(
        status=DataStatus(True, "loaded", "test"),
        tables={
            "daily_brief": [{
                "stable_key": "daily:AAA",
                "category": "decide_now",
                "headline": "Named decision title",
                "summary": "Named decision summary.",
                "score": 2.5,
                "symbol": "AAA",
                "sentiment": "bullish",
                "severity": "warn",
                "research_rank": 1,
            }],
            "preopen_daily_brief": [{
                "stable_key": "preopen:2026-09-01",
                "headline": "Named pre-open headline",
                "summary": "Named pre-open narrative.",
                "qqq_forecast": {"bias": "neutral", "expected_close": 500.0},
                "qqq_outcome": {"status": "pending"},
                "key_events": [{"event": "Payrolls"}],
            }],
            "portfolio_risk_cards": [],
            "ticker_decisions": [],
            "portfolio": [],
        },
    )
    monkeypatch.setattr(loaders_owner, "load_panel_scope_data", lambda _config, _scope: panel)

    response = TestClient(app).get("/api/today")

    assert response.status_code == 200
    payload = response.json()
    assert payload["brief_items"] == [{
        "stable_key": "daily:AAA",
        "category": "decide_now",
        "title": "Named decision title",
        "summary": "Named decision summary.",
        "score": 2.5,
        "symbol": "AAA",
        "sentiment": "bullish",
        "severity": "warn",
        "antithesis": None,
        "action": None,
        "next_action": None,
        "blockers": [],
        "days_until": None,
        "stats": ["Research rank 1"],
    }]
    assert payload["preopen_brief"]["headline"] == "Named pre-open headline"
    assert payload["preopen_brief"]["key_events"] == ["Payrolls"]


@pytest.mark.parametrize(
    ("reason", "availability_status"),
    (
        ("trade_plan_missing", "missing"),
        ("trade_plan_identity_mismatch", "conflicted"),
        ("trade_plan_invalid", "error"),
        ("risk_policy_blocked", "policy_blocked"),
    ),
)
def test_today_missing_plan_field_state_preserves_blocker_semantics(
    reason: str, availability_status: str,
) -> None:
    from app.routers.panel import today_field_states

    states = today_field_states(identity_missing=False, plan_missing=True, reason=reason)

    assert states == [{
        "field": "trade_plan",
        "availability_status": availability_status,
        "source": "trade_plan",
        "reason": reason,
        "blocking": True,
        "next_action": "Refresh the ticker decision and publish its canonical TradePlan.",
    }]


def test_non_today_snapshot_caches_compiled_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    panel_owner.invalidate_context_cache()
    loads = 0
    builds = 0
    panel = PanelData(status=DataStatus(True, "loaded", "test"), tables={})

    def load_snapshot(_config: AppConfig, _scope: str, **_kwargs: Any) -> PanelData:
        nonlocal loads
        loads += 1
        return panel

    def build_snapshot(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal builds
        builds += 1
        return {
            "scope": "portfolio",
            "status": {"ready": True, "message": "loaded", "source": "test", "metadata": {}},
            "dashboard": None,
            "tables": {},
        }

    monkeypatch.setattr(loaders_owner, "load_panel_scope_data", load_snapshot)
    monkeypatch.setattr(panel_owner, "scope_snapshot_payload", build_snapshot)
    client = TestClient(app)

    first = client.get("/api/panel-snapshot?scope=portfolio")
    second = client.get("/api/panel-snapshot?scope=portfolio")

    assert first.status_code == second.status_code == 200
    assert loads == builds == 1


def test_today_aggregates_missing_plan_backlog_before_queue_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_temp_api_db(monkeypatch, tmp_path / "missing-plan-backlog.json")
    transitions = [
        {
            "id": f"inbox-{index}",
            "event_type": "ready",
            "status": "active",
            "created_at": "2026-08-28T14:00:00Z",
            "payload": {"symbol": f"I{index:03d}"},
        }
        for index in range(10)
    ]
    monkeypatch.setitem(
        app.dependency_overrides,
        dependencies.get_options_actions,
        lambda: SimpleNamespace(decision_inbox=lambda **_kwargs: {"items": transitions}),
    )
    panel = PanelData(
        status=DataStatus(True, "loaded", "test"),
        tables={
            "ticker_decisions": [
                {
                    "symbol": f"T{index:03d}",
                    "decision_revision": f"ticker-decision.v1:{index}",
                    "capital_action": {"action": "NO_TRADE", "owned": False},
                    "as_of": "2026-08-28T14:00:00Z",
                }
                for index in range(100)
            ],
        },
        metadata={"today_missing_plan_count": 107},
    )
    monkeypatch.setattr(loaders_owner, "load_panel_scope_data", lambda _config, _scope: panel)

    response = TestClient(app).get("/api/today")

    assert response.status_code == 200
    payload = response.json()
    assert payload["missing_plan_count"] == 107
    assert payload["count"] == 10
    assert [item["projection_identity"] for item in payload["actions"]] == [
        f"inbox:decision-inbox:inbox-{index}" for index in range(10)
    ]
    assert all(item["source"] == "decision_inbox" for item in payload["actions"])


def test_today_keeps_other_sources_when_ticker_capital_exceeds_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_temp_api_db(monkeypatch, tmp_path / "bounded-today.json")
    monkeypatch.setitem(
        app.dependency_overrides,
        dependencies.get_options_actions,
        lambda: SimpleNamespace(decision_inbox=lambda **_kwargs: {
            "items": [{
                "id": "inbox-fair",
                "event_type": "ready",
                "status": "active",
                "created_at": "2026-08-28T14:00:00Z",
                "payload": {},
            }],
        }),
    )
    panel = PanelData(
        status=DataStatus(True, "loaded", "test"),
        tables={
            "ticker_decisions": [
                {
                    "symbol": f"T{index:03d}",
                    "decision_revision": f"ticker-decision.v1:{index}",
                    "capital_action": {"action": "BUY", "owned": True},
                    "as_of": "2026-08-28T14:00:00Z",
                }
                for index in range(150)
            ],
            "portfolio_risk_cards": [{
                "card_id": "risk-fair",
                "severity": "critical",
                "title": "Risk exception",
                "updated_at": "2026-08-28T14:00:00Z",
            }],
            "feed_signals": [{
                "id": "research-fair",
                "source_family": "research",
                "title": "Research update",
                "date": "2026-08-28T14:00:00Z",
            }],
        },
    )
    monkeypatch.setattr(loaders_owner, "load_panel_scope_data", lambda _config, _scope: panel)

    response = TestClient(app).get("/api/today")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 100
    assert {item["source"] for item in payload["actions"]} == {
        "capital_action", "decision_inbox", "portfolio_risk", "research",
    }


def test_api_routes_return_json(postgresql, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    info = postgresql.info
    credentials = info.user if not info.password else f"{info.user}:{info.password}"
    postgres_dsn = f"postgresql://{credentials}@{info.host}:{info.port}/{info.dbname}"
    upgrade_database(postgres_dsn)
    _use_postgres_api(monkeypatch, postgres_dsn)

    def panel_scope(_config: AppConfig | None, scope: str, **_kwargs: object) -> PanelData:
        return PanelData(
            status=DataStatus(True, "test", "postgresql"),
            tables={name: [] for name in PANEL_SCOPE_TABLES[scope]},
        )

    monkeypatch.setattr(loaders_owner, "load_panel_scope_data", panel_scope)
    monkeypatch.setattr(loaders_owner, "load_market_panel_data", lambda config, **_kwargs: panel_scope(config, "market"))
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
            "/api/today",
            "/api/market/breadth",
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


@pytest.mark.parametrize("fill_assumption", [7.1, 5.01])
def test_options_candidate_fill_basis_is_string_for_both_api_routes(
    fill_assumption: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    from investment_panel.database.options_decision_system import OptionsDecisionSystemRepository

    candidate_row = {
        "decision_id": "decision-1",
        "relative_value_id": 1,
        "paper_state": "WATCH",
        "discovery_lane": "thesis",
        "structure": "long_call",
        "expiration": "2026-09-18",
        "strike": 500,
        "option_type": "call",
        "fill_assumption": fill_assumption,
    }
    authority_row = {
        "payload": {"decision_id": "decision-1", "symbol": "QQQ"},
        "publication_id": "publication-1",
        "published_at": None,
        "rank": 1,
        "stable_key": "episode:1",
        "authoritative_decision_id": "decision-1",
        "episode_key": "episode:1",
        "source_row_count": 1,
        "authoritative_row_count": 1,
        "valid_row_count": 1,
        "episode_authority_count": 1,
        "row_valid": True,
    }
    responses = iter([
        SimpleNamespace(fetchone=lambda: {"id": "run-1", "summary": {}, "finished_at": None}),
        SimpleNamespace(fetchall=lambda: [authority_row]),
        SimpleNamespace(fetchone=lambda: {"count": 1}),
        SimpleNamespace(fetchall=lambda: [candidate_row]),
    ])
    connection = SimpleNamespace(execute=lambda *_args: next(responses))
    runtime = SimpleNamespace(read=lambda: nullcontext(connection))
    candidate = OptionsDecisionSystemRepository(runtime).candidates()["items"][0]
    readiness = {
        "capture": {"capture_state": None, "completeness": None, "capture_generation_id": None, "complete_captures": 0},
        "underlying": {"group_count": 0, "groups_with_missing_underlying": 0, "groups_with_inconsistent_underlying": 0},
        "analysis": {"eligible_groups": 0, "fit_attempts": 0, "succeeded_groups": 0, "solver_failures": 0},
        "thesis": {"eligible": False, "present": False, "revision": None, "invalidation": None, "blocker": None, "direction": None},
        "calibration": [],
        "canary": {
            "observed_regular_session_dates": 0,
            "qualified_regular_sessions": 0,
            "required_regular_sessions": 5,
            "canary_revision": "test",
            "canary_started_at": None,
            "disqualification_reasons": [],
        },
        "top_blockers": [],
        "next_required_action": "research_only",
    }
    brief = {
        "symbol": "QQQ",
        "lane": "thesis",
        "mode": "shadow",
        "analysis_run_id": None,
        "as_of": None,
        "state": "WATCH",
        "summary": {},
        "readiness": readiness,
        "strongest_candidate": candidate,
        "paper_only": True,
        "decision_truth": None,
    }
    page = {
        "items": [candidate],
        "total": 1,
        "next_cursor": None,
        "as_of": None,
        "capture_generation_id": None,
        "model_revision": "test",
        "scope": "current",
        "analysis_run_id": None,
        "rows": [candidate],
        "count": 1,
        "offset": 0,
        "limit": 100,
    }
    actions = SimpleNamespace(
        candidates=lambda **_kwargs: page,
        workspace=lambda **_kwargs: {"symbol": "QQQ", "decision_brief": brief},
    )
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_options_actions, lambda: actions)

    try:
        client = TestClient(app)
        candidates = client.get("/api/options/candidates")
        workspace = client.get("/api/options/workspace")
    finally:
        app.dependency_overrides.pop(dependencies.get_options_actions, None)

    expected = str(fill_assumption)
    assert candidate["conservative_entry"]["fill_basis"] == expected
    assert candidates.status_code == 200
    assert candidates.json()["items"][0]["conservative_entry"]["fill_basis"] == expected
    assert workspace.status_code == 200
    assert workspace.json()["decision_brief"]["strongest_candidate"]["conservative_entry"]["fill_basis"] == expected


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


def test_update_agent_settings_endpoint_is_local_and_scoped(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "settings-api.json"
    _use_temp_api_db(monkeypatch, db_path)
    captured: dict[str, Any] = {}

    def fake_update(config: AppConfig, section: str, payload: dict[str, Any]) -> None:
        captured["config"] = config
        captured["section"] = section
        captured["payload"] = payload

    monkeypatch.setattr(settings_owner, "persist_setting_section", fake_update)
    fresh_config = typed_config(raw={"agents": {"option_agent": {"enabled": True}}})
    monkeypatch.setattr(system_owner, "load_config", lambda: fresh_config)
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
                "enabled": True,
                "command": "market-run-option-agent",
                "timeout_seconds": 90,
                "thesis_limit": 3,
                "provider": "codex",
            },
        },
    )

    assert response.status_code == 200
    assert captured["section"] == "agents"
    assert captured["payload"]["option_agent"]["enabled"] is True
    assert response.json()["status"]["ready"] is True
    assert response.json()["config"]["agents"]["option_agent"]["enabled"] is True


def test_market_snapshot_only_returns_market_tables(monkeypatch) -> None:
    panel_owner.invalidate_context_cache()
    monkeypatch.setattr(
        loaders_owner,
        "load_market_panel_data",
        lambda _config, **_kwargs: PanelData(
            status=DataStatus(True, "test", "postgresql"),
            tables={name: [] for name in PANEL_SCOPE_TABLES["market"]},
        ),
    )
    client = TestClient(app)

    response = client.get("/api/panel-snapshot?scope=market")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload["tables"]) == {
        "market_state_snapshot",
        "coverage_matrix",
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


def test_ticker_route_dedupes_repeated_option_lineage_and_projects_impact(monkeypatch) -> None:
    available_at = "2026-08-25T14:40:00Z"
    option_row = {
        "symbol": "QQQ",
        "structure": "long_call",
        "max_loss": 250,
        "expiration": "2026-10-16",
        "available_at": available_at,
        "source_version": "7267600",
        "revision": "7267600",
        "legs": [{
            "contract_id": 1,
            "option_type": "call",
            "side": "long",
            "strike": 505,
            "bid": 2,
            "ask": 2.2,
            "bid_size": 10,
            "ask_size": 10,
            "quote_time": available_at,
        }],
    }
    panel = PanelData(
        status=DataStatus(True, "ticker", "postgresql"),
        tables={
            "quotes": [{"symbol": "QQQ", "price": 500, "available_at": available_at, "confirmed": True}],
            "broker_accounts": [{"symbol": "QQQ", "net_liquidation": 100_000, "available_at": available_at}],
            "decision_queue": [{
                "symbol": "QQQ", "stance": "BULLISH", "action": "BUY",
                "entry_low": 499, "entry_high": 501, "invalidation_price": 480,
                "available_at": available_at,
            }],
            "options_payoff_scenarios": [
                option_row,
                dict(option_row),
                {**option_row, "source_version": "7267601", "revision": "7267601"},
            ],
        },
    )
    _use_postgres_api(monkeypatch, "postgresql:///ticker-lineage-repair")
    monkeypatch.setattr(loaders_owner, "load_ticker_panel_data", lambda *_args: panel)

    client = TestClient(app)
    response = client.get("/api/tickers/QQQ")

    assert response.status_code == 200
    payload = response.json()
    decision = payload["ticker_decision"]
    selected_kind = decision["selected_expression"]["kind"]
    selected_impact = decision["portfolio_impacts"][selected_kind]
    assert selected_impact["expression_kind"] == selected_kind
    assert "input_lineage" not in selected_impact
    assert "inputs" not in decision["input_manifest"]
    assert set(decision) == {
        "as_of", "capital_action", "decision_contract_version", "decision_revision",
        "expressions", "fundamental", "input_manifest", "market_evidence_assessment",
        "portfolio_impacts", "resolution", "selected_expression", "tactical", "ticker",
    }
    assert "opportunity_episode" not in payload
    assert "resolution" not in payload
    assert "alpha_signals" not in payload
    assert "opportunity_rank" not in payload
    assert "trade_plan" not in payload
    assert "outcome_attributions" not in payload
    assert "expressions" not in payload
    assert "data_requests" not in payload
    assert "learning" not in payload
    assert "learning_history" not in payload
    assert "instrument_state_snapshot" not in payload
    assert decision["resolution"]["eligibility"] == "BLOCKED"
    assert decision["selected_expression"]["kind"] == "CASH"
    assert decision["resolution"]["authorization_mode"] == "NONE"
    assert "PAPER_READY" not in repr(payload)

    snapshot_response = client.get("/api/tickers/QQQ/decision-snapshot")

    assert snapshot_response.status_code == 200
    snapshot = snapshot_response.json()
    snapshot_kind = snapshot["selected_expression"]["kind"]
    assert snapshot["learning"]
    assert len(snapshot["opportunity_episode"]["input_lineage"]) == 5
    assert snapshot["portfolio_impacts"][snapshot_kind]["input_lineage"]
    assert snapshot["market_state_snapshot"]["coverage_matrix"]["rows"][0]["current_status"] == "unavailable"


def test_settings_snapshot_returns_no_panel_tables() -> None:
    client = TestClient(app)

    response = client.get("/api/panel-snapshot?scope=settings")

    assert response.status_code == 200
    payload = response.json()
    assert payload["tables"] == {}
    assert payload["status"]["ready"] is True
    assert payload["status"]["source"] == "postgresql"


def test_options_radar_snapshot_returns_radar_tables(monkeypatch) -> None:
    panel_owner.invalidate_context_cache()
    monkeypatch.setattr(
        loaders_owner,
        "load_panel_scope_data",
        lambda _config, scope, **_kwargs: PanelData(
            status=DataStatus(True, "test", "postgresql"),
            tables={name: [] for name in PANEL_SCOPE_TABLES[scope]},
        ),
    )
    client = TestClient(app)

    response = client.get("/api/panel-snapshot?scope=options-radar")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload["tables"]) == set(PANEL_SCOPE_TABLES["options-radar"])


def test_research_snapshot_pages_past_query_cap_with_true_count(monkeypatch) -> None:
    panel_owner.invalidate_context_cache()
    all_rows = [{"decision_id": f"decision-{index}"} for index in range(101)]
    query_limits: list[int] = []

    def fake_load_panel_data(_config: AppConfig, table_names: tuple[str, ...], **options: object) -> PanelData:
        limits = options.get("query_row_limits")
        if limits is None:
            return PanelData(
                status=DataStatus(True, "loaded research seed", "postgresql"),
                tables={name: [] for name in table_names},
            )
        assert isinstance(limits, dict)
        row_limit = limits["decision_queue"]
        assert isinstance(row_limit, int)
        query_limits.append(row_limit)
        tables = {name: [] for name in table_names}
        tables["decision_queue"] = all_rows[:row_limit]
        return PanelData(
            status=DataStatus(True, "loaded research", "postgresql"),
            tables=tables,
            metadata={"table_counts": {"decision_queue": len(all_rows)}},
        )

    monkeypatch.setattr(loaders_owner, "load_panel_data", fake_load_panel_data)
    client = TestClient(app)

    first = client.get("/api/panel-snapshot?scope=research")
    second = client.get("/api/panel-snapshot?scope=research&offset=100")
    negative_offset = client.get("/api/panel-snapshot?scope=research&offset=-1")
    oversized_limit = client.get("/api/panel-snapshot?scope=research&limit=501")

    assert first.status_code == second.status_code == 200
    assert len(first.json()["tables"]["decision_queue"]["rows"]) == 100
    second_table = second.json()["tables"]["decision_queue"]
    assert second_table == {
        "rows": [{"decision_id": "decision-100"}],
        "count": 101,
        "offset": 100,
    }
    assert query_limits == [100, 200]
    assert negative_offset.status_code == oversized_limit.status_code == 422


def test_options_radar_snapshot_does_not_cache_reads_or_fallback(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "read-only-api.json"
    _use_temp_api_db(monkeypatch, db_path)
    calls = 0
    cache_writes: list[Path] = []
    original_write_text = Path.write_text

    def record_cache_write(path: Path, *args: Any, **kwargs: Any) -> int:
        if path.name.startswith("panel-snapshot-"):
            cache_writes.append(path)
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", record_cache_write)

    def fake_scope_loader(_config: dict[str, object], scope: str, **_kwargs: object) -> PanelData:
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
    assert cache_writes == []

    panel_owner.invalidate_context_cache()
    second = client.get("/api/panel-snapshot?scope=options-radar")

    assert second.status_code == 503
    assert second.json()["detail"] == "PostgreSQL unavailable"


def test_watchlist_snapshot_returns_503_when_postgresql_unavailable(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "unavailable-watchlist-api.json"
    _use_temp_api_db(monkeypatch, db_path)
    monkeypatch.setattr(
        loaders_owner,
        "load_panel_scope_data",
        lambda _config, _scope, **_kwargs: PanelData(
            status=DataStatus(False, "PostgreSQL timed out", "postgresql-error"), tables={}
        ),
    )

    response = TestClient(app).get("/api/panel-snapshot?scope=watchlist")

    assert response.status_code == 503
    assert response.json()["detail"] == "PostgreSQL timed out"


def test_options_radar_ready_empty_snapshot_does_not_claim_postgres_is_unavailable(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "ready-empty-api.json"
    _use_temp_api_db(monkeypatch, db_path)

    monkeypatch.setattr(
        loaders_owner,
        "load_panel_scope_data",
        lambda _config, _scope, **_kwargs: PanelData(
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

    fast_returned = threading.Event()

    def read_uncached_fast_context() -> None:
        try:
            panel_owner.context(
                cache_key="fast",
                loader=lambda _config: PanelData(status=DataStatus(True, "fast", "test"), tables={}),
            )
            fast_returned.set()
        except BaseException as exc:  # pragma: no cover - threaded assertion capture
            errors.append(exc)

    fast_thread = threading.Thread(target=read_uncached_fast_context)
    fast_thread.start()
    assert fast_returned.wait(timeout=0.5)

    release_slow.set()
    slow_thread.join(timeout=1)
    cached_thread.join(timeout=1)
    fast_thread.join(timeout=1)
    assert not errors


def test_context_cache_coalesces_concurrent_same_key_loads() -> None:
    panel_owner.invalidate_context_cache()
    config = typed_config("postgresql:///single-flight")
    shared = PanelData(status=DataStatus(True, "shared", "test"), tables={})
    loader_started = threading.Event()
    release_loader = threading.Event()
    second_started = threading.Event()
    results: list[PanelData] = []
    errors: list[BaseException] = []
    loads = 0

    def loader(_config: AppConfig) -> PanelData:
        nonlocal loads
        loads += 1
        loader_started.set()
        release_loader.wait(timeout=5)
        return shared

    def read_context(started: threading.Event | None = None) -> None:
        try:
            if started is not None:
                started.set()
            _, panel_data = panel_owner.context(
                cache_key="same",
                loader=loader,
                config_loader=lambda: config,
                database_url_loader=lambda _config: "postgresql:///single-flight",
            )
            results.append(panel_data)
        except BaseException as exc:  # pragma: no cover - threaded assertion capture
            errors.append(exc)

    leader = threading.Thread(target=read_context)
    leader.start()
    assert loader_started.wait(timeout=1)
    waiter = threading.Thread(target=read_context, args=(second_started,))
    waiter.start()
    assert second_started.wait(timeout=1)
    waiter.join(timeout=0.05)
    assert waiter.is_alive()

    release_loader.set()
    leader.join(timeout=1)
    waiter.join(timeout=1)

    assert errors == []
    assert loads == 1
    assert results == [shared, shared]
    assert panel_owner._CONTEXT_INFLIGHT == {}


def test_context_cache_loader_error_wakes_waiter_and_does_not_poison_cache() -> None:
    panel_owner.invalidate_context_cache()
    config = typed_config("postgresql:///single-flight-error")
    loader_started = threading.Event()
    release_loader = threading.Event()
    second_started = threading.Event()
    errors: list[BaseException] = []
    loads = 0

    def failing_loader(_config: AppConfig) -> PanelData:
        nonlocal loads
        loads += 1
        loader_started.set()
        release_loader.wait(timeout=5)
        raise RuntimeError("shared load failed")

    def read_context(started: threading.Event | None = None) -> None:
        try:
            if started is not None:
                started.set()
            panel_owner.context(
                cache_key="error",
                loader=failing_loader,
                config_loader=lambda: config,
                database_url_loader=lambda _config: "postgresql:///single-flight-error",
            )
        except BaseException as exc:  # pragma: no cover - threaded assertion capture
            errors.append(exc)

    leader = threading.Thread(target=read_context)
    leader.start()
    assert loader_started.wait(timeout=1)
    waiter = threading.Thread(target=read_context, args=(second_started,))
    waiter.start()
    assert second_started.wait(timeout=1)
    waiter.join(timeout=0.05)
    assert waiter.is_alive()

    release_loader.set()
    leader.join(timeout=1)
    waiter.join(timeout=1)

    assert loads == 1
    assert [str(error) for error in errors] == ["shared load failed", "shared load failed"]
    assert "error" not in panel_owner._CONTEXT_CACHE["entries"]
    assert panel_owner._CONTEXT_INFLIGHT == {}

    recovered = PanelData(status=DataStatus(True, "recovered", "test"), tables={})
    _, panel_data = panel_owner.context(
        cache_key="error",
        loader=lambda _config: recovered,
        config_loader=lambda: config,
        database_url_loader=lambda _config: "postgresql:///single-flight-error",
    )
    assert panel_data is recovered


def test_context_cache_invalidation_rejects_stale_inflight_value() -> None:
    panel_owner.invalidate_context_cache()
    config = typed_config("postgresql:///single-flight-invalidation")
    old = PanelData(status=DataStatus(True, "old", "test"), tables={})
    fresh = PanelData(status=DataStatus(True, "fresh", "test"), tables={})
    old_started = threading.Event()
    release_old = threading.Event()
    old_results: list[PanelData] = []

    def load_old(_config: AppConfig) -> PanelData:
        old_started.set()
        release_old.wait(timeout=1)
        return old

    def read_old() -> None:
        _, panel_data = panel_owner.context(
            cache_key="invalidate",
            loader=load_old,
            config_loader=lambda: config,
            database_url_loader=lambda _config: "postgresql:///single-flight-invalidation",
        )
        old_results.append(panel_data)

    old_thread = threading.Thread(target=read_old)
    old_thread.start()
    assert old_started.wait(timeout=1)

    panel_owner.invalidate_context_cache()
    _, fresh_result = panel_owner.context(
        cache_key="invalidate",
        loader=lambda _config: fresh,
        config_loader=lambda: config,
        database_url_loader=lambda _config: "postgresql:///single-flight-invalidation",
    )
    release_old.set()
    old_thread.join(timeout=1)

    _, cached_result = panel_owner.context(
        cache_key="invalidate",
        loader=lambda _config: pytest.fail("stale flight replaced the fresh cache entry"),
        config_loader=lambda: config,
        database_url_loader=lambda _config: "postgresql:///single-flight-invalidation",
    )
    assert fresh_result is fresh
    assert old_results == [fresh]
    assert cached_result is fresh


def test_context_cache_waiter_reloads_config_after_invalidation() -> None:
    panel_owner.invalidate_context_cache()
    old_config = object()
    fresh_config = object()
    old = PanelData(status=DataStatus(True, "old", "test"), tables={})
    fresh = PanelData(status=DataStatus(True, "fresh", "test"), tables={})
    old_started = threading.Event()
    waiter_configured = threading.Event()
    fresh_started = threading.Event()
    release_old = threading.Event()
    fresh_mode = threading.Event()
    results: list[PanelData] = []
    errors: list[BaseException] = []
    config_calls = 0

    def config_loader() -> object:
        nonlocal config_calls
        config_calls += 1
        if config_calls >= 2:
            waiter_configured.set()
        return fresh_config if fresh_mode.is_set() else old_config

    def load(active_config: object) -> PanelData:
        if active_config is old_config:
            old_started.set()
            release_old.wait(timeout=2)
            return old
        fresh_started.set()
        return fresh

    def read_context() -> None:
        try:
            _, panel_data = panel_owner.context(
                cache_key="invalidate-waiter",
                loader=load,
                config_loader=config_loader,
                database_url_loader=lambda _config: "postgresql:///invalidate-waiter",
            )
            results.append(panel_data)
        except BaseException as exc:  # pragma: no cover - threaded assertion capture
            errors.append(exc)

    leader = threading.Thread(target=read_context)
    leader.start()
    assert old_started.wait(timeout=1)
    waiter = threading.Thread(target=read_context)
    waiter.start()
    assert waiter_configured.wait(timeout=1)

    fresh_mode.set()
    panel_owner.invalidate_context_cache()
    assert fresh_started.wait(timeout=1)
    release_old.set()
    leader.join(timeout=1)
    waiter.join(timeout=1)

    assert errors == []
    assert results == [fresh, fresh]
    assert panel_owner._CONTEXT_INFLIGHT == {}


def test_context_cache_evicts_expired_entries_and_bounds_cardinality(monkeypatch) -> None:
    panel_owner.invalidate_context_cache()
    clock = [0.0]
    loads = 0
    config = typed_config("postgresql:///cache-bound")

    def load(_config: AppConfig) -> PanelData:
        nonlocal loads
        loads += 1
        return PanelData(status=DataStatus(True, f"load-{loads}", "test"), tables={})

    def cached(key: str) -> None:
        panel_owner.context(
            cache_key=key,
            loader=load,
            config_loader=lambda: config,
            database_url_loader=lambda _config: "postgresql:///cache-bound",
        )

    monkeypatch.setattr(panel_owner.time, "monotonic", lambda: clock[0])
    cached("ttl")
    clock[0] = panel_owner.CONTEXT_CACHE_TTL_SECONDS - 0.01
    cached("ttl")
    assert loads == 1

    clock[0] = panel_owner.CONTEXT_CACHE_TTL_SECONDS
    cached("new")
    assert "ttl" not in panel_owner._CONTEXT_CACHE["entries"]

    for index in range(panel_owner.CONTEXT_CACHE_MAX_ENTRIES + 5):
        cached(f"page:{index}")

    entries = panel_owner._CONTEXT_CACHE["entries"]
    assert len(entries) == panel_owner.CONTEXT_CACHE_MAX_ENTRIES
    assert "page:0" not in entries
    assert f"page:{panel_owner.CONTEXT_CACHE_MAX_ENTRIES + 4}" in entries


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
    assert "latest_status" not in response.json()


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
            "evidence_refs": [{"type": "agent_request", "id": queued["request_id"]}],
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
    assert [row["evaluation_type"] for row in evaluations] == [
        "shadow", "walk_forward",
    ]
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
        paper_order_ids, decision_ids = _seed_phase7_paper_provenance(
            connection, ready_result["candidate_revision_id"], instrument["id"],
        )
        metrics = {
            name: ({"risk_on": 0.5} if name == "regime_performance" else 0.1)
            for name in TRACKED_METRICS
        }
        for stage in ("walk_forward", "shadow", "execution_grade_paper"):
            connection.execute(
                """
                INSERT INTO analysis.strategy_evaluation (
                    strategy_revision_id, evaluation_type, evaluated_at,
                    period_start, period_end, verdict, metrics, evidence
                ) VALUES (%s, %s, clock_timestamp(), now() - interval '30 days',
                          now(), 'pass', %s, %s)
                """,
                [ready_result["candidate_revision_id"], stage, Jsonb(metrics), Jsonb({
                    "sample_size": 30, "source": "analysis.option_outcome",
                    "method": "retained_actionable_decisions_forward_evaluation",
                    "version": "phase7-governance-evidence-v1",
                    "uncertainty": {"lower_95_expectancy": 0.01},
                    **({"paper_execution": {
                        "source": "app.paper_order", "paper_only": True,
                        "sample_size": 30, "completed_orders": 30,
                        "strategy_revision_id": ready_result["candidate_revision_id"],
                        "database_verified": True,
                        "paper_order_ids": paper_order_ids,
                        "decision_ids": decision_ids,
                    }} if stage == "execution_grade_paper" else {}),
                })],
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
        instrument_id = connection.execute(
            "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES (%s, %s, 'equity') RETURNING id",
            [f"P7{uuid4().hex[:8]}", "Phase 7 promotion evidence"],
        ).fetchone()["id"]
        paper_order_ids, decision_ids = _seed_phase7_paper_provenance(
            connection, candidate_id, instrument_id,
        )
        metrics = {
            name: ({"risk_on": 0.5} if name == "regime_performance" else 0.1)
            for name in TRACKED_METRICS
        }
        for evaluation_type in ("walk_forward", "shadow", "execution_grade_paper"):
            connection.execute(
                "INSERT INTO analysis.strategy_evaluation "
                "(strategy_revision_id, evaluation_type, evaluated_at, verdict, metrics, evidence) "
                "VALUES (%s, %s, now(), 'pass', %s, %s)",
                [candidate_id, evaluation_type, Jsonb(metrics), Jsonb({
                    "sample_size": 30, "source": "analysis.option_outcome",
                    "method": "retained_actionable_decisions_forward_evaluation",
                    "version": "phase7-governance-evidence-v1",
                    "uncertainty": {"lower_95_expectancy": 0.01},
                        **({"paper_execution": {
                            "source": "app.paper_order", "paper_only": True,
                            "sample_size": 30, "completed_orders": 30,
                            "strategy_revision_id": candidate_id,
                            "database_verified": True,
                            "paper_order_ids": paper_order_ids,
                            "decision_ids": decision_ids,
                        }} if evaluation_type == "execution_grade_paper" else {}),
                })],
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


def test_local_api_guard_allows_private_lan_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    def request(host: str, headers: dict[str, str] | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            client=SimpleNamespace(host=host),
            headers=Headers({"host": "mini1.local", **(headers or {})}),
        )

    require_local_request(request("100.120.95.8"))
    require_local_request(request("192.168.50.197"))
    require_local_request(request("127.0.0.1"))
    require_local_request(request("::ffff:100.120.95.8"))

    with pytest.raises(HTTPException):
        require_local_request(request("8.8.8.8"))
    with pytest.raises(HTTPException):
        require_local_request(request("2001:4860:4860::8888"))
    with pytest.raises(HTTPException):
        require_local_request(request("127.0.0.1", {"x-forwarded-for": "8.8.8.8"}))
    with pytest.raises(HTTPException):
        require_local_request(request("127.0.0.1", {"x-forwarded-for": "192.168.50.42"}))
    with pytest.raises(HTTPException):
        require_local_request(request("127.0.0.1", {"forwarded": "for=192.168.50.42"}))
    with pytest.raises(HTTPException):
        require_local_request(request("localhost", {"x-forwarded-for": "8.8.8.8"}))
    with pytest.raises(HTTPException):
        require_local_request(request("127.0.0.1", {"x-forwarded-for": "192.168.50.42, 8.8.8.8"}))
    with pytest.raises(HTTPException):
        require_local_request(
            request("127.0.0.1", {"x-forwarded-for": "8.8.8.8, 192.168.50.42"})
        )
    with pytest.raises(HTTPException):
        require_local_request(request("127.0.0.1", {"x-forwarded-for": "not-an-ip, 192.168.50.42"}))
    with pytest.raises(HTTPException):
        require_local_request(
            SimpleNamespace(client=SimpleNamespace(host="192.168.50.42"), headers=Headers())
        )


@pytest.mark.parametrize(
    "host_header",
    [
        "localhost:8000",
        "192.168.50.197:8000",
        "100.120.95.8:8000",
        "[::1]:8000",
        "mini1.local:8000",
        "mini1.tail46d3fb.ts.net:8000",
    ],
)
def test_api_allows_authorized_host_headers(host_header: str) -> None:
    response = TestClient(app, client=("192.168.50.42", 50000)).get(
        "/api/panel-contract",
        headers={"host": host_header},
    )

    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/api/panel-contract", "/docs"])
@pytest.mark.parametrize(
    "host_header",
    ["attacker.example", "8.8.8.8", "[::1", "mini1.local/path", "", ".tail46d3fb.ts.net"],
)
def test_api_rejects_unauthorized_or_malformed_host_headers(path: str, host_header: str) -> None:
    response = TestClient(app, client=("192.168.50.42", 50000)).get(
        path,
        headers={"host": host_header},
    )

    assert response.status_code == 403


def test_api_rejects_public_network_clients() -> None:
    direct_response = TestClient(app, client=("8.8.8.8", 50000)).get(
        "/api/status",
        headers={"host": "mini1.local", "x-forwarded-for": "192.168.50.42"},
    )
    proxied_response = TestClient(app, client=("127.0.0.1", 50000)).get(
        "/api/status",
        headers={"host": "mini1.local", "x-forwarded-for": "8.8.8.8, 192.168.50.42"},
    )

    assert direct_response.status_code == 403
    assert proxied_response.status_code == 403
    private_forwarded_response = TestClient(app, client=("127.0.0.1", 50000)).get(
        "/api/status",
        headers={"host": "mini1.local", "x-forwarded-for": "192.168.50.42"},
    )
    assert private_forwarded_response.status_code == 403


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"])
def test_api_documentation_rejects_public_network_clients(path: str) -> None:
    direct_response = TestClient(app, client=("8.8.8.8", 50000)).get(
        path, headers={"host": "mini1.local"}
    )
    proxied_response = TestClient(app, client=("127.0.0.1", 50000)).get(
        path,
        headers={"host": "mini1.local", "x-forwarded-for": "192.168.50.42, 8.8.8.8"},
    )

    assert direct_response.status_code == 403
    assert proxied_response.status_code == 403


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"])
def test_api_documentation_allows_private_network_clients(
    path: str,
) -> None:
    direct_response = TestClient(app, client=("192.168.50.42", 50000)).get(
        path, headers={"host": "192.168.50.197:8000"}
    )
    proxied_response = TestClient(app, client=("127.0.0.1", 50000)).get(
        path,
        headers={
            "host": "mini1.tail46d3fb.ts.net:8000",
        },
    )

    assert direct_response.status_code == 200
    assert proxied_response.status_code == 200


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
    dist_index = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "index.html"
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


def test_frontend_fallback_does_not_serve_files_outside_dist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dist_dir = tmp_path / "frontend" / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
    (tmp_path / "secret.txt").write_text("not-for-the-browser", encoding="utf-8")
    monkeypatch.setattr(app_main, "__file__", str(tmp_path / "app" / "main.py"))
    test_app = app_main.FastAPI()
    app_main._mount_frontend(test_app)

    response = TestClient(test_app).get("/%2e%2e/%2e%2e/secret.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == '<div id="root"></div>'
