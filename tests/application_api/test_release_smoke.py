"""Seeded local release smoke: mocked transport plus isolated migrated PostgreSQL workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
import pytest

from conftest import typed_config
from investment_panel.api import dependencies
from investment_panel.api.main import app
from investment_panel.application.read_models import loaders as loaders_owner
from investment_panel.application.read_models import panel_snapshot as panel_owner
from investment_panel.application.read_models.types import DataStatus, PanelData
from investment_panel.core.robinhood_options import collect_robinhood_option_chains
from investment_panel.domain.panel import PANEL_SCOPE_TABLES
from investment_panel.infrastructure.postgres import experiment_events, options as option_database
from investment_panel.infrastructure.postgres.migrations import HEAD_REVISION
from investment_panel.infrastructure.postgres.options import active_paper_contracts, persist_collected_option_chains
from investment_panel.infrastructure.postgres.options_analysis import (
    DEFAULT_PARAMETERS,
    FEATURE_VERSION,
    IMPLEMENTATION_ID,
    refresh_options_radar,
)
from investment_panel.infrastructure.postgres.options_experiments import advance_experiment_shadows
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.infrastructure.postgres.workstation import WorkstationRepository, next_session_open
from scripts import verify_workstation as check


@dataclass
class _RobinhoodConfig:
    max_expiries: int = 1
    strikes_around_spot: int = 1
    quote_batch_size: int = 20
    collect_puts: bool = False
    near_term_dte: int = 0
    max_collection_seconds: int = 30


class _RobinhoodFixture:
    """One normal, executable contract returned through the real collector."""

    def __init__(self, *, expiry: str, provider_at: datetime) -> None:
        self.expiry = expiry
        self.provider_at = provider_at

    def get_equity_quotes(self, symbols: list[str]) -> dict[str, Any]:
        return {"data": {"results": [{"quote": {
            "symbol": symbol, "last_trade_price": "155", "venue_last_trade_time": self.provider_at.isoformat(),
        }} for symbol in symbols]}}

    def get_option_chains(self, underlying_symbol: str) -> dict[str, Any]:
        return {"data": {"chains": [{
            "id": "seeded-nvda-chain", "symbol": underlying_symbol, "cash_component": None,
            "settle_on_open": False, "trade_value_multiplier": "100.0000",
            "underlying_instruments": [{"instrument": "https://provider.test/NVDA"}],
            "expiration_dates": [self.expiry],
        }]}}

    def get_option_instruments(self, *, chain_id: str | None = None, expiration_dates: str | None = None,
                               option_type: str | None = None, **_kwargs: Any) -> dict[str, Any]:
        assert chain_id == "seeded-nvda-chain" and option_type == "call"
        return {"data": {"instruments": [{
            "id": "seeded-nvda-call", "chain_id": chain_id, "chain_symbol": "NVDA",
            "underlying_type": "equity", "expiration_date": expiration_dates, "strike_price": "160",
            "type": "call", "state": "active", "tradability": "tradable",
        }], "next": None}}

    def get_option_quotes(self, instrument_ids: list[str]) -> dict[str, Any]:
        return {"data": {"results": [{"quote": {
            "instrument_id": instrument_id, "bid_price": "0.48", "ask_price": "0.50",
            "mark_price": "0.49", "bid_size": 10, "ask_size": 10, "open_interest": 1000,
            "volume": 1000, "implied_volatility": "0.3", "delta": "0.4",
            "updated_at": self.provider_at.isoformat(), "market_data_status": "live",
        }} for instrument_id in instrument_ids]}}


class _FrozenDateTimeMeta(type):
    def __instancecheck__(cls, value: object) -> bool:
        return isinstance(value, datetime)


class _FrozenDateTime(datetime, metaclass=_FrozenDateTimeMeta):
    instant: datetime

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return cls.instant if tz is None else cls.instant.astimezone(tz)


def _collect_fixture(provider: _RobinhoodFixture, *, required_contracts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return collect_robinhood_option_chains(
        _RobinhoodConfig(), ["NVDA"], client=provider, min_dte=0, max_dte=900,
        max_expiries=1, strikes_around_spot=1, required_contracts=required_contracts,
        required_only=required_contracts is not None,
    )


class _Response:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        assert len(self.content) <= limit
        return self.content


def test_seeded_local_api_passes_release_smoke_without_production_database(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = "a" * 40
    monkeypatch.setenv("MARKET_BACKEND_COMMIT", expected)
    monkeypatch.setenv("MARKET_FRONTEND_BUILD", expected)
    monkeypatch.setenv("MARKET_SCHEDULER_RELEASE", expected)
    config = typed_config(status_dir=tmp_path / "status")
    tables = {name: [] for name in PANEL_SCOPE_TABLES["today"]}
    tables["ticker_decisions"] = [{
        "ticker": "ACME", "ticker_decision_id": "seed-acme", "decision_revision": "seed.v1",
        "policy_version": "risk-policy.v2", "as_of": "2026-09-20T13:00:00Z",
        "capital_action": {"ticker": "ACME", "action": "BUY", "rationale": "Seeded local decision.",
                           "price_condition": "100-105", "catalyst": "earnings", "owned": False},
    }]
    tables["daily_brief"] = [{"stable_key": "seed-brief", "category": "decide_now", "headline": "Seeded brief"}]
    panel = PanelData(
        status=DataStatus(True, "seeded local PostgreSQL-shaped data", "fixture"),
        tables=tables,
        metadata={"schema_revision": HEAD_REVISION, "expected_schema_revision": HEAD_REVISION},
    )
    panel_owner.invalidate_context_cache()
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_config, lambda: config)
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_options_history,
                        lambda: SimpleNamespace(health=lambda **_kwargs: {"available": True}))
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_options_research,
                        lambda: SimpleNamespace(decision_inbox=lambda **_kwargs: {"items": []}))
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_research_workbench,
                        lambda: SimpleNamespace(action_items=lambda **_kwargs: []))
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_workstation,
                        lambda: SimpleNamespace(status=lambda _config: {"status": "available", "as_of": "2026-09-20T20:00:00Z", "market_session": "closed", "blockers": []}))
    monkeypatch.setattr(panel_owner, "load_config", lambda: config)
    monkeypatch.setattr(loaders_owner, "load_panel_data", lambda *_args, **_kwargs: panel)
    monkeypatch.setattr(loaders_owner, "load_panel_scope_data", lambda *_args, **_kwargs: panel)
    client = TestClient(app)

    def local_urlopen(request, *, timeout: float):
        assert timeout == 5
        parsed = urlsplit(request.full_url)
        response = client.get(parsed.path + (f"?{parsed.query}" if parsed.query else ""))
        assert response.status_code == 200
        return _Response(response.content)

    monkeypatch.setattr(check, "urlopen", local_urlopen)
    report = check.verify("http://fixture.local", timeout=5, expected_commit=expected,
                          expected_schema=HEAD_REVISION, release_candidate=True)
    today = client.get("/api/today").json()
    snapshot = client.get("/api/panel-snapshot?scope=today").json()

    assert report["status"] == "pass"
    assert today["actions"] == []
    assert today["book_actions"][0]["action"] == "NO_TRADE"
    assert snapshot["tables"]["ticker_decisions"]["count"] == 1
    assert snapshot["tables"]["daily_brief"]["rows"][0]["stable_key"] == "seed-brief"
    panel_owner.invalidate_context_cache()


def test_seeded_postgres_workflow_preserves_entry_and_reports_partial_collection(
    migrated_postgres_dsn: str, application_postgres_dsn: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the collector, PostgreSQL, experiment worker, journal, and Health route together."""
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    application = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    application.open()
    config = typed_config(application_postgres_dsn)
    started = datetime.now(UTC)
    provider = _RobinhoodFixture(
        expiry=(started.date() + timedelta(days=40)).isoformat(),
        provider_at=started - timedelta(seconds=1),
    )
    monkeypatch.setattr(option_database, "runtime_for_config", lambda _config: runtime)
    # The fixture models a regular-session capture without depending on the
    # wall-clock time when a developer runs the test.
    monkeypatch.setattr(option_database, "_market_session", lambda _observed_at: "regular")
    try:
        with runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO analysis.strategy_revision "
                "(strategy_key, revision, name, status, parameters, authority_group, implementation_id, implementation_version, created_at, promoted_at) "
                "VALUES ('options-radar-core', 4, 'Seeded smoke incumbent', 'active', %s, 'options-radar-core', %s, %s, %s, %s)",
                [Jsonb(DEFAULT_PARAMETERS), IMPLEMENTATION_ID, FEATURE_VERSION,
                 started - timedelta(days=150), started - timedelta(days=150)],
            )

        initial = _collect_fixture(provider)
        initial_capture = persist_collected_option_chains(config, "robinhood", initial, universe="seeded-radar")
        receipt_at = datetime.fromisoformat(str(initial["received_at"]).replace("Z", "+00:00"))
        with runtime.read() as connection:
            quote = connection.execute(
                "SELECT observed_at, provider_observed_at, available_at FROM raw.option_quote WHERE snapshot_id = %s",
                [initial_capture["snapshot_id"]],
            ).fetchone()
        assert quote["observed_at"] == quote["provider_observed_at"] == provider.provider_at
        assert quote["available_at"] == receipt_at and quote["observed_at"] <= quote["available_at"]

        seeded = refresh_options_radar(runtime, source_id="robinhood", code_version="seeded-release-smoke")
        assert seeded["shadow_trades"] == 1
        required = active_paper_contracts(config, "robinhood")
        assert len(required) == 1 and required[0]["symbol"] == "NVDA"

        provider.provider_at = datetime.now(UTC)
        second = _collect_fixture(provider, required_contracts=required)
        persist_collected_option_chains(config, "robinhood", second, universe="paper-tickets")
        entered_at = datetime.now(UTC)
        monkeypatch.setattr("investment_panel.infrastructure.postgres.options_experiments.is_market_open", lambda _now: True)
        assert advance_experiment_shadows(application, now=entered_at)["entered"] == 1

        provider.provider_at = datetime.now(UTC)
        third = _collect_fixture(provider, required_contracts=required)
        persist_collected_option_chains(config, "robinhood", third, universe="paper-tickets")
        marked_at = datetime.now(UTC)
        advance_experiment_shadows(application, now=marked_at)
        with runtime.read() as connection:
            shadow_id = str(connection.execute(
                "SELECT id FROM analysis.shadow_trade WHERE source_kind = 'options_paper_experiment'"
            ).fetchone()["id"])
        history = experiment_events.experiment_history(application, observation_id=shadow_id, now=marked_at)
        assert history is not None and [event["kind"] for event in history["events"]] == ["entry", "mark"]
        assert history["events"][0]["at"] == entered_at
        assert history["events"][1]["at"] == marked_at

        _FrozenDateTime.instant = next_session_open(marked_at) + timedelta(minutes=6)
        monkeypatch.setattr("investment_panel.infrastructure.postgres.workstation.datetime", _FrozenDateTime)
        monkeypatch.setattr(experiment_events, "datetime", _FrozenDateTime)
        panel_owner.invalidate_context_cache()
        monkeypatch.setitem(app.dependency_overrides, dependencies.get_config, lambda: config)
        monkeypatch.setitem(app.dependency_overrides, dependencies.get_workstation, lambda: WorkstationRepository(application))
        response = TestClient(app).get("/api/workstation/status")
        assert response.status_code == 200
        workflow = response.json()
        assert workflow["observations"]["status"] == "partial"
        assert workflow["observations"]["counts"] == {"entered": 1}
        assert {incident["reason"] for incident in workflow["observations"]["collection"]["incidents"]} == {
            "experiment_management_overdue", "experiment_quote_overdue",
        }
        report = check.assess("workflow", workflow)
        assert report["status"] == "needs_attention"
        assert report["evidence"]["observations_record_count"] == 1
        assert any("observations population is partial" in warning for warning in report["warnings"])
    finally:
        panel_owner.invalidate_context_cache()
        application.close()
        runtime.close()


@pytest.mark.parametrize("health_status", ["partial", "unavailable"])
def test_working_transport_cannot_hide_failed_decision_service(tmp_path, monkeypatch, health_status):
    config = typed_config(status_dir=tmp_path / "status")
    panel = PanelData(status=DataStatus(True, "Database readable", "fixture"), tables={}, metadata={})
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_config, lambda: config)
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_options_history,
                        lambda: SimpleNamespace(health=lambda **_: {"available": True}))
    monkeypatch.setitem(app.dependency_overrides, dependencies.get_workstation,
                        lambda: SimpleNamespace(status=lambda _: {"status": health_status,
                            "as_of": "2026-09-20T20:00:00Z", "market_session": "closed",
                            "decision_service": {"failed_count": 1, "failures_by_owner": {"refresh_assessment_inputs": 1}}}))
    monkeypatch.setattr(loaders_owner, "load_panel_data", lambda *_args, **_kwargs: panel)
    result = TestClient(app).get("/api/status").json()
    assert result["transport_ready"] is True
    assert result["ready"] is False and result["service_ready"] is False
    assert result["workstation"]["decision_service"]["failed_count"] == 1
    assert result["message"] == "Workflow health degraded. System health identifies the affected workflows and blockers."
