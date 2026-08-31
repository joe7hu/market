from __future__ import annotations

from datetime import UTC, datetime, timedelta

from investment_panel.core.decision import MarketStateSnapshot
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import DatabaseRuntime
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
    config = typed_config(raw={"watchlist": [{"symbol": "CONFIG-ONLY"}]})
    events: list[tuple[str, object]] = []
    market_publication = {"status": "ok", "publication_id": "market-publication-full-test"}
    monkeypatch.setattr(postgres_refresh, "load_config", lambda _path=None: config)
    monkeypatch.setattr(postgres_refresh, "runtime_for_config", lambda _config: object())
    monkeypatch.setattr(
        update_market_data,
        "run",
        lambda _path, publish=False: {
            "status": "ok",
            "benchmark_symbols": ["CONFIG-ONLY"],
        },
    )
    monkeypatch.setattr(update_arco_sources, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(update_content_sources, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(update_market_events, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(update_disclosure_sources, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(update_robinhood_options, "run", lambda _path: {"status": "auth_required"})
    monkeypatch.setattr(update_ibkr_options, "run", lambda _path: {"status": "gateway_offline"})
    monkeypatch.setattr(update_broker_sources, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(postgres_refresh.refresh_options_radar, "run", lambda _path: {"status": "ok"})
    def publish_tickers(_path, *, as_of=None, market_state_publication_id=None):
        assert market_state_publication_id == market_publication["publication_id"]
        events.append(("ticker", as_of))
        return {"status": "ok"}

    def publish_today(_runtime, *, now=None, **_kwargs):
        events.append(("today", now))
        return {"status": "ok"}

    def publish_market(_runtime, *, now=None, configured_watchlist=None):
        assert configured_watchlist == config.watchlist
        events.append(("market", now))
        return {**market_publication, "published_at": now + timedelta(microseconds=1)}

    monkeypatch.setattr(postgres_refresh.ticker_decisions, "publish", publish_tickers)
    monkeypatch.setattr(postgres_refresh.run_option_agents, "run", lambda _path: {"status": "skipped"})
    monkeypatch.setattr(postgres_refresh.run_thesis_monitor, "run", lambda _path, **_kwargs: {"status": "skipped"})
    monkeypatch.setattr(postgres_refresh, "refresh_today_publication", publish_today)
    monkeypatch.setattr(postgres_refresh, "refresh_market_publication", publish_market)
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
    names = [name for name, _ in events]
    assert names.index("market") < names.index("ticker") < names.index("today")
    publication_cutoffs = [cutoff for name, cutoff in events if name in {"market", "ticker", "today"}]
    assert publication_cutoffs[0] < publication_cutoffs[1]
    assert publication_cutoffs[1] is publication_cutoffs[2]


def test_routine_publication_uses_config_only_exact_market_benchmark(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    import pandas as pd

    config = typed_config(
        migrated_postgres_dsn,
        raw={
            "data_sources": {"yfinance": {"enabled": False}},
            "watchlist": [{"symbol": "CONFIG-ONLY", "asset_class": "equity"}],
        },
    )
    monkeypatch.setattr(
        update_market_data,
        "fetch_prices",
        lambda *_args: pd.DataFrame(
            [{
                "symbol": "CONFIG-ONLY", "date": "2026-08-28", "open": 10,
                "high": 12, "low": 10, "close": 12, "volume": 120, "source": "test",
            }]
        ),
    )

    result = update_market_data.run_for_config(config, publish=False)
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) "
                "VALUES ('CATALOG-ONLY', 'Catalog only', 'equity')"
            )
            assert connection.execute(
                "SELECT count(*) AS count FROM app.watchlist_item"
            ).fetchone()["count"] == 0
        monkeypatch.setattr(postgres_refresh, "load_config", lambda _path=None: config)
        monkeypatch.setattr(postgres_refresh, "runtime_for_config", lambda _config: runtime)
        monkeypatch.setattr(
            postgres_refresh.refresh_options_radar,
            "run_deterministic_only",
            lambda _path: {"status": "ok"},
        )
        monkeypatch.setattr(
            postgres_refresh.ticker_decisions,
            "publish",
            lambda *_args, **_kwargs: {"status": "ok"},
        )
        monkeypatch.setattr(
            postgres_refresh,
            "refresh_today_publication",
            lambda *_args, **_kwargs: {"status": "ok"},
        )
        monkeypatch.setattr(
            postgres_refresh.OutcomeRepository,
            "refresh",
            lambda _self, **_kwargs: {"status": "ok"},
        )

        publication = postgres_refresh.publish_decisions("config.yaml")
        snapshot = MarketStateSnapshot.model_validate(
            AnalysisRepository(runtime).publication_rows("market", "market_state_snapshot")[0]
        )
    finally:
        runtime.close()

    state = next(
        row for row in snapshot.horizons["3-12 months"]
        if row.dimension == "corporate cycle"
    )
    assert publication["status"] == "ok"
    assert result["benchmark_symbols"] == ["CONFIG-ONLY"]
    assert state.eligible_members == ["CONFIG-ONLY"]


def test_publish_decisions_consumes_visible_same_cycle_market_publication(monkeypatch) -> None:
    config = typed_config()
    events: list[tuple[str, object]] = []
    market_publication = {"status": "ok", "publication_id": "market-publication-test"}

    monkeypatch.setattr(postgres_refresh, "load_config", lambda _path=None: config)
    monkeypatch.setattr(postgres_refresh, "runtime_for_config", lambda _config: object())
    monkeypatch.setattr(
        postgres_refresh.refresh_options_radar,
        "run_deterministic_only",
        lambda _path: {"status": "ok"},
    )

    def publish_market(_runtime, *, now=None, configured_watchlist=None):
        assert configured_watchlist == config.watchlist
        events.append(("market", now))
        return {**market_publication, "published_at": now + timedelta(microseconds=1)}

    def publish_tickers(_path, *, as_of=None, market_state_publication_id=None):
        assert market_state_publication_id == market_publication["publication_id"]
        events.append(("ticker", as_of))
        return {"status": "ok"}

    def publish_today(_runtime, *, now=None):
        events.append(("today", now))
        return {"status": "ok"}

    monkeypatch.setattr(postgres_refresh, "refresh_market_publication", publish_market)
    monkeypatch.setattr(postgres_refresh.ticker_decisions, "publish", publish_tickers)
    monkeypatch.setattr(postgres_refresh, "refresh_today_publication", publish_today)
    monkeypatch.setattr(
        postgres_refresh.OutcomeRepository,
        "refresh",
        lambda _self, **_kwargs: {"status": "ok"},
    )

    result = postgres_refresh.publish_decisions("config.yaml")

    assert result["status"] == "ok"
    assert [name for name, _ in events] == ["market", "ticker", "today"]
    assert events[0][1] < events[1][1]
    assert events[1][1] is events[2][1]


def test_premarket_threads_market_publication_id_after_market_publication(monkeypatch) -> None:
    config = typed_config()
    events: list[tuple[str, object]] = []
    market_publication = {"status": "ok", "publication_id": "market-publication-premarket-test"}
    monkeypatch.setattr(postgres_refresh, "load_config", lambda _path=None: config)
    monkeypatch.setattr(postgres_refresh, "runtime_for_config", lambda _config: object())
    monkeypatch.setattr(postgres_refresh.refresh_options_radar, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(
        postgres_refresh.refresh_options_radar,
        "run_deterministic_only",
        lambda _path: {"status": "ok"},
    )
    monkeypatch.setattr(postgres_refresh.run_option_agents, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(
        postgres_refresh.run_thesis_monitor,
        "run",
        lambda _path, **_kwargs: {"status": "skipped"},
    )

    def publish_market(_runtime, *, now=None, configured_watchlist=None):
        assert configured_watchlist == config.watchlist
        events.append(("market", now))
        return {**market_publication, "published_at": now + timedelta(microseconds=1)}

    def publish_tickers(_path, *, as_of=None, market_state_publication_id=None):
        assert market_state_publication_id == market_publication["publication_id"]
        events.append(("ticker", as_of))
        return {"status": "ok"}

    def publish_today(_runtime, *, now=None, **_kwargs):
        events.append(("today", now))
        return {"status": "ok"}

    monkeypatch.setattr(postgres_refresh, "refresh_market_publication", publish_market)
    monkeypatch.setattr(postgres_refresh.ticker_decisions, "publish", publish_tickers)
    monkeypatch.setattr(postgres_refresh, "refresh_today_publication", publish_today)
    monkeypatch.setattr(
        postgres_refresh.OutcomeRepository,
        "refresh",
        lambda _self, **_kwargs: {"status": "ok"},
    )

    result = postgres_refresh.premarket(
        "config.yaml",
        now=datetime(2026, 7, 6, 12, 15, tzinfo=UTC),
    )

    assert result["status"] == "ok"
    assert [name for name, _ in events] == ["market", "ticker", "today"]
    assert events[0][1] < events[1][1]
    assert events[1][1] is events[2][1]


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
