from __future__ import annotations

from datetime import UTC, datetime

from investment_panel.jobs import (
    postgres_refresh,
    snapshot_database,
    update_broker_sources,
    update_arco_sources,
    update_content_sources,
    update_disclosure_sources,
    update_ibkr_options,
    update_market_data,
    update_market_events,
    update_robinhood_options,
)
from conftest import typed_config


def test_full_refresh_reports_unavailable_optional_providers_as_partial(monkeypatch) -> None:
    config = typed_config()
    monkeypatch.setattr(postgres_refresh, "load_config", lambda _path=None: config)
    monkeypatch.setattr(postgres_refresh, "runtime_for_config", lambda _config: object())
    monkeypatch.setattr(update_market_data, "run", lambda _path, publish=False: {"status": "ok"})
    monkeypatch.setattr(update_arco_sources, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(update_content_sources, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(update_market_events, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(update_disclosure_sources, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(update_robinhood_options, "run", lambda _path: {"status": "auth_required"})
    monkeypatch.setattr(update_ibkr_options, "run", lambda _path: {"status": "gateway_offline"})
    monkeypatch.setattr(update_broker_sources, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(postgres_refresh.refresh_options_radar, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(postgres_refresh.ticker_decisions, "publish", lambda _path: {"status": "ok"})
    monkeypatch.setattr(postgres_refresh.run_option_agents, "run", lambda _path: {"status": "skipped"})
    monkeypatch.setattr(postgres_refresh.run_thesis_monitor, "run", lambda _path, **_kwargs: {"status": "skipped"})
    monkeypatch.setattr(postgres_refresh, "refresh_today_publication", lambda _runtime: {"status": "ok"})
    monkeypatch.setattr(postgres_refresh, "refresh_market_publication", lambda _runtime: {"status": "ok"})
    monkeypatch.setattr(postgres_refresh.OutcomeRepository, "refresh", lambda _self, **_kwargs: {"status": "ok"})
    monkeypatch.setattr(snapshot_database, "run", lambda _path: {"status": "verified"})

    class _Retention:
        def __init__(self, _runtime) -> None:
            pass

        def prune(self):
            return {"status": "ok"}

    monkeypatch.setattr(postgres_refresh, "RetentionRepository", _Retention)

    result = postgres_refresh.full("config.yaml")

    assert result["ok"] is True
    assert result["status"] == "partial"
    assert result["warning_steps"] == ["robinhood_options", "ibkr_options"]
    assert result["failed_steps"] == []


def test_scheduled_preopen_skips_outside_window_and_publishes_inside(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    from investment_panel.database.runtime import DatabaseRuntime

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    config = typed_config(migrated_postgres_dsn)
    monkeypatch.setattr(postgres_refresh, "load_config", lambda _path=None: config)
    monkeypatch.setattr(postgres_refresh, "runtime_for_config", lambda _config: runtime)
    monkeypatch.setattr(
        postgres_refresh,
        "refresh_today_publication",
        lambda _runtime, now=None, **kwargs: {"status": "ok", "publication_id": "today", "now": now, **kwargs},
    )
    try:
        outside = postgres_refresh.scheduled_preopen(
            now=datetime(2026, 7, 13, 15, 0, tzinfo=UTC)
        )
        inside = postgres_refresh.scheduled_preopen(
            now=datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
        )
        assert outside["reason"] == "outside_premarket_window"
        assert inside["status"] == "ok"
        assert inside["ok"] is True
        assert inside["use_agent_narrative"] is True
        assert inside["agent_model"] == "gpt-5.6-luna"
    finally:
        runtime.close()


def test_premarket_skips_us_market_holidays_before_running_agents(monkeypatch) -> None:
    monkeypatch.setattr(
        postgres_refresh.run_option_agents,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("agents should not run")),
    )

    result = postgres_refresh.premarket(
        now=datetime(2026, 7, 3, 12, 15, tzinfo=UTC),
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "market_closed"


def test_scheduled_preopen_skips_us_market_holidays_before_loading_runtime(monkeypatch) -> None:
    monkeypatch.setattr(
        postgres_refresh,
        "load_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("runtime should not load")),
    )

    result = postgres_refresh.scheduled_preopen(
        now=datetime(2026, 7, 3, 12, 15, tzinfo=UTC),
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "market_closed"
    assert result["reason"] == "market_closed"
