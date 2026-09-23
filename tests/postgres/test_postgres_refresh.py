from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.domain.decision import MarketStateSnapshot, latest_completed_market_day
from investment_panel.infrastructure.postgres.analysis import AnalysisRepository
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.jobs import (
    assessment_inputs,
    postgres_refresh,
    snapshot_database,
    update_broker_sources,
    update_arco_sources,
    update_content_sources,
    update_company_financials,
    update_disclosure_sources,
    update_ibkr_options,
    update_market_data,
    update_market_events,
    update_robinhood_options,
    update_phase2_sources,
)
from conftest import typed_config


@pytest.mark.parametrize("entrypoint", ["learning_marks", "postgres_refresh"])
@pytest.mark.parametrize("transition", ["automatic_promotions", "automatic_rollbacks", None])
def test_policy_transition_recalculates_signals_with_actual_config(monkeypatch, entrypoint, transition) -> None:
    from investment_panel.jobs import refresh_options_radar as job

    config = typed_config(raw={"analysis": {"options_decision_system": {"options_risk_sleeve_capital": 12345}}})
    runtime = object()
    outcome_result = {"automatic_promotions": 0, "automatic_rollbacks": 0}
    if transition:
        outcome_result[transition] = 1
    monkeypatch.setattr(job, "load_config", lambda _path=None: config)
    monkeypatch.setattr(job, "runtime_for_config", lambda passed: runtime if passed is config else None)
    monkeypatch.setattr(job.OutcomeRepository, "refresh", lambda _self, **_kwargs: outcome_result)
    calls = []

    def calculate(passed_runtime, **kwargs):
        calls.append((passed_runtime, kwargs))
        return {"publication_id": "fresh-policy-publication"}

    monkeypatch.setattr(job, "refresh_options_radar", calculate)
    result = job.run_learning_marks("actual-config.yaml") if entrypoint == "learning_marks" else postgres_refresh._refresh_option_outcomes(runtime, config)
    if transition:
        assert calls == [(runtime, {"config": config, "options_risk_sleeve_capital": 12345})]
        assert result["policy_signal_refresh"] == {"publication_id": "fresh-policy-publication"}
    else:
        assert calls == []
        assert "policy_signal_refresh" not in result


def test_full_refresh_reports_unavailable_optional_providers_as_partial(monkeypatch) -> None:
    config = typed_config(raw={"watchlist": [{"symbol": "CONFIG-ONLY"}]})
    events: list[tuple[str, object]] = []
    market_publication = {"status": "ok", "publication_id": "market-publication-full-test"}
    monkeypatch.setattr(postgres_refresh, "load_config", lambda _path=None: config)
    monkeypatch.setattr(postgres_refresh, "runtime_for_config", lambda _config: object())
    monkeypatch.setattr(postgres_refresh, "_priority_ticker_symbols", lambda *_: ["HELD"])
    monkeypatch.setattr(
        update_market_data,
        "run",
        lambda _path, publish=False: {
            "status": "ok",
            "benchmark_symbols": ["CONFIG-ONLY"],
        },
    )
    monkeypatch.setattr(assessment_inputs, "features", lambda _path: {"status": "ok"})
    monkeypatch.setattr(assessment_inputs, "collect", lambda _path: {"status": "ok"})
    monkeypatch.setattr(update_arco_sources, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(update_content_sources, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(update_company_financials, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(update_phase2_sources, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(update_market_events, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(update_disclosure_sources, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(update_robinhood_options, "run", lambda _path: {"status": "auth_required"})
    monkeypatch.setattr(update_ibkr_options, "run", lambda _path: {"status": "gateway_offline"})
    monkeypatch.setattr(update_broker_sources, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(postgres_refresh.refresh_options_radar, "run", lambda _path: {"status": "ok"})
    def publish_tickers(_path, *, symbols, as_of=None, market_state_publication_id=None, **_kwargs):
        assert symbols == ["HELD"]
        assert market_state_publication_id == market_publication["publication_id"]
        events.append(("ticker", as_of))
        return {"status": "ok"}

    def publish_today(_runtime, *, now=None, **_kwargs):
        events.append(("today", now))
        return {"status": "ok"}

    def publish_market(_runtime, *, now=None, configured_watchlist=None, **_kwargs):
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
        def __init__(self, _runtime, **_kwargs) -> None:
            pass

        def prune(self):
            return {"status": "ok"}

    monkeypatch.setattr(postgres_refresh, "RetentionRepository", _Retention)

    result = postgres_refresh.full("config.yaml")

    assert result["ok"] is True, result
    assert result["status"] == "partial"
    assert result["warning_steps"] == ["robinhood_options", "ibkr_options"]
    assert result["failed_steps"] == []
    names = [name for name, _ in events]
    assert names.index("market") < names.index("ticker") < names.index("today")
    publication_cutoffs = [cutoff for name, cutoff in events if name in {"market", "ticker", "today"}]
    assert publication_cutoffs[0] < publication_cutoffs[1]
    assert publication_cutoffs[1] <= publication_cutoffs[2]


def test_full_refresh_stops_at_a_terminal_bar_retry(monkeypatch) -> None:
    config = typed_config()
    calls: list[str] = []
    monkeypatch.setattr(postgres_refresh, "load_config", lambda _path=None: config)
    monkeypatch.setattr(update_arco_sources, "run", lambda _path: {"status": "ok"})
    monkeypatch.setattr(
        update_market_data,
        "run",
        lambda _path, publish=False: {
            "status": "partial",
            "retry_after_seconds": 300,
            "terminal_bar_checked": True,
            "expected_terminal_bar": "2026-09-22",
            "missing_terminal_bars": ["UNH"],
        },
    )
    monkeypatch.setattr(
        assessment_inputs,
        "features",
        lambda _path: calls.append("features") or {"status": "ok"},
    )

    result = postgres_refresh.full("config.yaml")

    assert result["status"] == "partial"
    assert result["retry_after_seconds"] == 300
    assert result["missing_terminal_bars"] == ["UNH"]
    assert result["failed_steps"] == ["market_data"]
    assert [step["name"] for step in result["steps"]] == ["arco_sources", "market_data"]
    assert calls == []


def test_routine_publication_uses_config_only_exact_market_benchmark(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    import pandas as pd
    from investment_panel.workflows import market_data

    config = typed_config(
        migrated_postgres_dsn,
        raw={
            "data_sources": {"yfinance": {"enabled": False}},
            "watchlist": [{"symbol": "CONFIG-ONLY", "asset_class": "equity"}],
        },
    )
    terminal_date = latest_completed_market_day(datetime.now(UTC)).isoformat()
    monkeypatch.setattr(
        market_data,
        "fetch_prices",
        lambda symbol, *_args: pd.DataFrame(
            [{
                "symbol": symbol, "date": terminal_date, "open": 10,
                "high": 12, "low": 10, "close": 12, "volume": 120, "source": "test", "is_complete": True,
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
    assert result["benchmark_symbols"] == ["CONFIG-ONLY", "QQQ"]
    assert result["source_status"] == "ok"
    assert state.eligible_members == ["CONFIG-ONLY"]


def test_market_data_retries_missing_completed_terminal_bars(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    import pandas as pd
    from investment_panel.workflows import market_data

    config = typed_config(
        migrated_postgres_dsn,
        raw={
            "data_sources": {"yfinance": {"enabled": False}},
            "watchlist": [
                {"symbol": "CONFIG-ONLY", "asset_class": "equity"},
                {"symbol": "8035", "asset_class": "equity"},
            ],
        },
    )
    stale_date = (latest_completed_market_day(datetime.now(UTC)) - timedelta(days=1)).isoformat()
    monkeypatch.setattr(
        market_data,
        "fetch_prices",
        lambda symbol, *_args: pd.DataFrame([{
            "symbol": symbol, "date": stale_date, "open": 10, "high": 12, "low": 10,
            "close": 12, "volume": 120, "source": "test", "is_complete": True,
        }]),
    )
    monkeypatch.setattr(
        market_data,
        "refresh_market_publication",
        lambda *_args, **_kwargs: pytest.fail("terminal-bar retry published a stale Market state"),
    )

    result = update_market_data.run_for_config(config)

    assert result["status"] == result["source_status"] == "partial"
    assert result["missing_terminal_bars"] == ["CONFIG-ONLY", "QQQ"]
    assert result["retry_after_seconds"] == 300
    assert result["market_publication"] == {"status": "deferred", "reason": "terminal_bar_retry"}
    without_publication = update_market_data.run_for_config(config, publish=False)
    assert without_publication["status"] == without_publication["source_status"] == "partial"
    assert without_publication["missing_terminal_bars"] == ["CONFIG-ONLY", "QQQ"]
    assert without_publication["market_publication"] == {"status": "deferred", "reason": "publication_disabled"}
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.read() as connection:
            source_run = connection.execute(
                "SELECT status, instrument_count, summary FROM ingest.run WHERE id = %s", [result["run_id"]],
            ).fetchone()
    finally:
        runtime.close()
    assert source_run["status"] == "partial"
    assert source_run["instrument_count"] == 1
    assert source_run["summary"]["failed_symbols"] == 2
    assert source_run["summary"]["missing_terminal_bars"] == ["CONFIG-ONLY", "QQQ"]


def test_market_data_reuses_confirmed_terminal_bars_when_the_latest_response_is_stale(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    import pandas as pd
    from investment_panel.workflows import market_data

    config = typed_config(
        migrated_postgres_dsn,
        raw={
            "data_sources": {"yfinance": {"enabled": False}},
            "watchlist": [{"symbol": "CONFIG-ONLY", "asset_class": "equity"}],
        },
    )
    terminal_date = latest_completed_market_day(datetime.now(UTC))
    stale_date = terminal_date - timedelta(days=1)
    requested_date = terminal_date

    def fetch(symbol, *_args):
        return pd.DataFrame([{
            "symbol": symbol,
            "date": requested_date,
            "open": 10,
            "high": 12,
            "low": 10,
            "close": 12,
            "volume": 120,
            "source": "test",
            "is_complete": True,
        }])

    monkeypatch.setattr(market_data, "fetch_prices", fetch)
    initial = update_market_data.run_for_config(config, publish=False)
    requested_date = stale_date
    repeated = update_market_data.run_for_config(config, publish=False)

    assert initial["status"] == "ok"
    assert repeated["status"] == repeated["source_status"] == "ok"
    assert repeated["missing_terminal_bars"] == []


def test_market_data_persists_a_terminal_retry_from_the_publication_recheck(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    import pandas as pd
    from investment_panel.workflows import market_data

    config = typed_config(
        migrated_postgres_dsn,
        raw={
            "data_sources": {"yfinance": {"enabled": False}},
            "watchlist": [{"symbol": "CONFIG-ONLY", "asset_class": "equity"}],
        },
    )
    terminal_date = latest_completed_market_day(datetime.now(UTC)).isoformat()
    retry = {
        "status": "partial",
        "reason": "terminal_bar_retry",
        "expected_terminal_bar": terminal_date,
        "missing_terminal_bars": ["CONFIG-ONLY"],
        "retry_after_seconds": 300,
    }
    monkeypatch.setattr(
        market_data,
        "fetch_prices",
        lambda symbol, *_args: pd.DataFrame([{
            "symbol": symbol, "date": terminal_date, "open": 10, "high": 12, "low": 10,
            "close": 12, "volume": 120, "source": "test", "is_complete": True,
        }]),
    )
    monkeypatch.setattr(market_data, "terminal_bar_retry", lambda *_args: None)
    monkeypatch.setattr(market_data, "refresh_market_publication", lambda *_args, **_kwargs: retry)

    result = update_market_data.run_for_config(config)

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.read() as connection:
            source_run = connection.execute(
                "SELECT status, summary FROM ingest.run WHERE id = %s", [result["run_id"]],
            ).fetchone()
    finally:
        runtime.close()
    assert result["status"] == result["source_status"] == "partial"
    assert source_run["status"] == "partial"
    assert source_run["summary"]["missing_terminal_bars"] == ["CONFIG-ONLY"]


def test_market_data_rechecks_the_new_session_after_a_publication_cutoff_change(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    import pandas as pd
    from investment_panel.workflows import market_data

    config = typed_config(
        migrated_postgres_dsn,
        raw={
            "data_sources": {"yfinance": {"enabled": False}},
            "watchlist": [{"symbol": "CONFIG-ONLY", "asset_class": "equity"}],
        },
    )
    terminal_date = latest_completed_market_day(datetime.now(UTC)).isoformat()
    retry = {
        "status": "partial",
        "reason": "terminal_bar_retry",
        "expected_terminal_bar": terminal_date,
        "missing_terminal_bars": ["CONFIG-ONLY"],
        "retry_after_seconds": 300,
    }
    cutoff_changed = {
        "status": "partial",
        "reason": "terminal_bar_cutoff_changed",
        "expected_terminal_bar": terminal_date,
        "missing_terminal_bars": [],
        "retry_after_seconds": 300,
    }
    checks = iter((None, retry))
    monkeypatch.setattr(
        market_data,
        "fetch_prices",
        lambda symbol, *_args: pd.DataFrame([{
            "symbol": symbol, "date": terminal_date, "open": 10, "high": 12, "low": 10,
            "close": 12, "volume": 120, "source": "test", "is_complete": True,
        }]),
    )
    monkeypatch.setattr(market_data, "terminal_bar_retry", lambda *_args: next(checks))
    monkeypatch.setattr(market_data, "refresh_market_publication", lambda *_args, **_kwargs: cutoff_changed)

    result = update_market_data.run_for_config(config)

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.read() as connection:
            source_run = connection.execute(
                "SELECT status, summary FROM ingest.run WHERE id = %s", [result["run_id"]],
            ).fetchone()
    finally:
        runtime.close()
    assert result["status"] == result["source_status"] == "partial"
    assert result["expected_terminal_bar"] == terminal_date
    assert result["missing_terminal_bars"] == ["CONFIG-ONLY"]
    assert source_run["status"] == "partial"
    assert source_run["summary"]["missing_terminal_bars"] == ["CONFIG-ONLY"]


def test_market_data_checks_the_terminal_bar_after_a_close_crossing_collection(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    import pandas as pd
    from investment_panel.workflows import market_data

    config = typed_config(
        migrated_postgres_dsn,
        raw={
            "data_sources": {"yfinance": {"enabled": False}},
            "watchlist": [{"symbol": "CONFIG-ONLY", "asset_class": "equity"}],
        },
    )
    clock = iter((
        datetime(2026, 9, 22, 19, 59, tzinfo=UTC),
        datetime(2026, 9, 22, 20, 1, tzinfo=UTC),
    ))

    class Clock:
        @staticmethod
        def now(_timezone):
            return next(clock)

    monkeypatch.setattr(market_data, "datetime", Clock)
    monkeypatch.setattr(
        market_data,
        "fetch_prices",
        lambda symbol, *_args: pd.DataFrame([{
            "symbol": symbol, "date": "2026-09-21", "open": 10, "high": 12, "low": 10,
            "close": 12, "volume": 120, "source": "test", "is_complete": True,
        }]),
    )

    result = update_market_data.run_for_config(config, publish=False)

    assert result["missing_terminal_bars"] == ["CONFIG-ONLY", "QQQ"]
    assert result["expected_terminal_bar"] == "2026-09-22"


def test_decision_refresh_does_not_reuse_market_during_a_terminal_bar_retry(monkeypatch) -> None:
    config = typed_config()
    retry = {
        "status": "partial",
        "reason": "terminal_bar_retry",
        "expected_terminal_bar": "2026-09-22",
        "missing_terminal_bars": ["UNH"],
        "retry_after_seconds": 300,
    }
    monkeypatch.setattr(postgres_refresh, "load_config", lambda _path=None: config)
    monkeypatch.setattr(postgres_refresh, "runtime_for_config", lambda _config: object())
    monkeypatch.setattr(
        postgres_refresh.refresh_options_radar,
        "run_deterministic_only",
        lambda _path: {"status": "ok"},
    )
    monkeypatch.setattr(postgres_refresh, "terminal_bar_retry", lambda *_args: retry)
    monkeypatch.setattr(
        postgres_refresh,
        "_visible_market_publication",
        lambda *_args: pytest.fail("decision refresh reused a stale Market publication"),
    )

    result = postgres_refresh.publish_decisions("config.yaml", include_market_publication=False)

    assert result["status"] == "partial"
    assert result["retry_after_seconds"] == 300
    assert result["ticker_decisions"] == {"status": "skipped", "reason": "terminal_bar_retry"}


def test_decision_refresh_uses_a_published_baseline_when_advanced_market_is_partial(monkeypatch) -> None:
    config = typed_config()
    market = {
        "status": "partial",
        "baseline_status": "published",
        "publication_id": "market-publication-test",
        "published_at": datetime.now(UTC),
    }
    seen: dict[str, object] = {}
    monkeypatch.setattr(postgres_refresh, "load_config", lambda _path=None: config)
    monkeypatch.setattr(postgres_refresh, "runtime_for_config", lambda _config: object())
    monkeypatch.setattr(postgres_refresh, "_priority_ticker_symbols", lambda *_args: ["UNH"])
    monkeypatch.setattr(
        postgres_refresh.refresh_options_radar,
        "run_deterministic_only",
        lambda _path: {"status": "ok"},
    )
    monkeypatch.setattr(postgres_refresh, "refresh_market_publication", lambda *_args, **_kwargs: market)
    monkeypatch.setattr(
        postgres_refresh.ticker_decisions,
        "publish",
        lambda _path, **kwargs: seen.update(kwargs) or {"status": "ok"},
    )
    monkeypatch.setattr(postgres_refresh, "refresh_today_publication", lambda *_args, **_kwargs: {"status": "ok"})
    monkeypatch.setattr(postgres_refresh, "_refresh_portfolio_allocation", lambda *_args, **_kwargs: {"status": "ok"})

    result = postgres_refresh.publish_decisions("config.yaml", include_option_outcomes=False)

    assert result["status"] == "partial"
    assert seen["market_state_publication_id"] == "market-publication-test"


def test_publish_decisions_consumes_visible_same_cycle_market_publication(monkeypatch) -> None:
    config = typed_config()
    events: list[tuple[str, object]] = []
    market_publication = {"status": "ok", "publication_id": "market-publication-test"}

    monkeypatch.setattr(postgres_refresh, "load_config", lambda _path=None: config)
    monkeypatch.setattr(postgres_refresh, "runtime_for_config", lambda _config: object())
    monkeypatch.setattr(postgres_refresh, "_priority_ticker_symbols", lambda *_: ["HELD"])
    monkeypatch.setattr(
        postgres_refresh.refresh_options_radar,
        "run_deterministic_only",
        lambda _path: {"status": "ok"},
    )

    def publish_market(_runtime, *, now=None, configured_watchlist=None, **_kwargs):
        assert configured_watchlist == config.watchlist
        events.append(("market", now))
        return {**market_publication, "published_at": now + timedelta(microseconds=1)}

    def publish_tickers(_path, *, symbols, as_of=None, market_state_publication_id=None, **_kwargs):
        assert symbols == ["HELD"]
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
    assert events[1][1] <= events[2][1]


def test_lightweight_decision_publication_skips_expensive_options_rebuild(monkeypatch) -> None:
    config = typed_config()
    calls: list[str] = []
    market_cutoff = datetime.now(UTC) - timedelta(seconds=1)
    market_publication = {
        "status": "ok",
        "publication_id": "market-publication-test",
        "input_cutoff": market_cutoff,
    }
    monkeypatch.setattr(postgres_refresh, "load_config", lambda _path=None: config)
    monkeypatch.setattr(postgres_refresh, "runtime_for_config", lambda _config: object())
    monkeypatch.setattr(postgres_refresh, "_priority_ticker_symbols", lambda *_args: [])
    monkeypatch.setattr(postgres_refresh, "terminal_bar_retry", lambda *_args: None)
    monkeypatch.setattr(postgres_refresh, "_visible_market_publication", lambda *_args: market_publication)
    monkeypatch.setattr(
        postgres_refresh.refresh_options_radar,
        "run_deterministic_only",
        lambda _path: calls.append("options") or {"status": "ok"},
    )
    monkeypatch.setattr(
        postgres_refresh,
        "refresh_market_publication",
        lambda *_args, **_kwargs: calls.append("market") or {"status": "ok"},
    )
    monkeypatch.setattr(
        postgres_refresh.ticker_decisions,
        "publish",
        lambda _path, **kwargs: calls.append(
            f"ticker:{kwargs['as_of']}:{kwargs['refresh_outcomes']}"
        ) or {"status": "ok"},
    )
    monkeypatch.setattr(postgres_refresh, "refresh_today_publication", lambda *_args, **_kwargs: {"status": "ok"})
    monkeypatch.setattr(postgres_refresh.OutcomeRepository, "refresh", lambda _self, **_kwargs: {"status": "ok"})
    monkeypatch.setattr(postgres_refresh, "_refresh_portfolio_allocation", lambda *_args, **_kwargs: {"status": "ok"})

    result = postgres_refresh.publish_decisions(
        "config.yaml",
        include_options_radar=False,
        include_market_publication=False,
        include_ticker_outcomes=False,
        include_option_outcomes=False,
    )

    assert result["status"] == "ok"
    assert result["options_radar"] == {"status": "skipped", "reason": "dedicated_options_radar_cadence"}
    assert result["outcomes"] == {"status": "skipped", "reason": "dedicated_outcome_cadence"}
    assert result["market"] == market_publication
    assert len(calls) == 1 and calls[0].startswith("ticker:") and calls[0].endswith(":False")
    # A reused Market publication retains its identity, not an old consumer cutoff.
    assert datetime.fromisoformat(calls[0][7:-6]) > market_cutoff


def test_premarket_threads_market_publication_id_after_market_publication(monkeypatch) -> None:
    config = typed_config()
    events: list[tuple[str, object]] = []
    market_publication = {"status": "ok", "publication_id": "market-publication-premarket-test"}
    monkeypatch.setattr(postgres_refresh, "load_config", lambda _path=None: config)
    monkeypatch.setattr(postgres_refresh, "runtime_for_config", lambda _config: object())
    monkeypatch.setattr(postgres_refresh, "_priority_ticker_symbols", lambda *_: ["HELD"])
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

    def publish_market(_runtime, *, now=None, configured_watchlist=None, **_kwargs):
        assert configured_watchlist == config.watchlist
        events.append(("market", now))
        return {**market_publication, "published_at": now + timedelta(microseconds=1)}

    def publish_tickers(_path, *, symbols, as_of=None, market_state_publication_id=None, **kwargs):
        assert symbols == ["HELD"]
        assert market_state_publication_id == market_publication["publication_id"]
        assert kwargs["refresh_outcomes"] is False
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
    assert events[1][1] <= events[2][1]


def test_scheduled_preopen_skips_outside_window_and_publishes_inside(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime

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
