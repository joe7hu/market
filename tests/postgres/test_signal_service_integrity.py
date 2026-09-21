"""Actual PostgreSQL source -> feature -> publication -> health contracts.

Only provider transport is replaced with deterministic fixture facts. No private
production database, broker credentials or live account operations are used.
"""
from datetime import UTC, datetime, timedelta
from dataclasses import replace

import pytest
from psycopg.types.json import Jsonb

from conftest import typed_config
from investment_panel.domain.decision.calendar import is_market_open, is_us_market_day, latest_completed_market_day, market_session_bounds
from investment_panel.infrastructure.postgres.analysis import AnalysisRepository
from investment_panel.infrastructure.postgres.ingestion import IngestionRepository
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.infrastructure.postgres.symbol_trends import refresh_symbol_trend_features
from investment_panel.infrastructure.postgres.ticker_decisions import TickerDecisionRepository
from investment_panel.infrastructure.postgres.workstation import WorkstationRepository
from investment_panel.infrastructure.postgres.monitored_universe import monitored_universe
from investment_panel.infrastructure.postgres.confirmed_daily_prices import confirmed_daily_bars
from investment_panel.workflows import ticker_decisions


@pytest.fixture
def runtime(migrated_postgres_dsn):
    value = DatabaseRuntime(migrated_postgres_dsn)
    value.open()
    yield value
    value.close()


def seed_history(runtime, symbols):
    now = datetime.now(UTC)
    repository = IngestionRepository(runtime)
    repository.register_source("integrity-history", name="Integrity test history", family="market_data", kind="daily_bars", operational_state="active", health_owner="update_market_data", freshness_seconds=3600)
    rows, last = [], {}
    for symbol, crypto in symbols.items():
        day = now.date()-timedelta(days=1) if crypto else latest_completed_market_day(now)
        days = []
        while len(days) < 230:
            if crypto or is_us_market_day(day):
                days.append(day)
            day -= timedelta(days=1)
        for i, day in enumerate(reversed(days)):
            close = 100 + i * .3
            rows.append({"symbol": symbol, "date": day, "open": close-.2, "high": close+1,
                         "low": close-1, "close": close, "volume": 1000000, "is_complete": True})
            last[symbol] = close
    with repository.run("integrity-history", "fixture-history") as ingestion:
        assert repository.store_price_bars(ingestion.id, "integrity-history", rows,
            asset_classes={symbol: "crypto" if crypto else "etf" for symbol, crypto in symbols.items()}) == len(rows)
        ingestion.finish("succeeded", item_count=len(rows))
    for symbol, crypto in symbols.items():
        source = "assessment-crypto-quotes" if crypto else "assessment-equity-quotes"
        repository.register_source(source, name="Assessment fixture", family="market_data", kind="crypto_quote" if crypto else "intraday_quote", operational_state="active")
        quote_time = datetime.now(UTC)
        observed = quote_time if crypto or is_market_open(quote_time) else market_session_bounds(latest_completed_market_day(quote_time))[1].astimezone(UTC)
        with repository.run(source, "fixture-reference") as ingestion:
            assert repository.store_quotes(ingestion.id, source, [{"symbol": symbol, "asset_class": "crypto" if crypto else "etf", "price": last[symbol], "observed_at": observed}]) == 1
            ingestion.finish("succeeded", item_count=1)


def test_missing_configured_instrument_is_a_named_failure_not_healthy_empty(runtime, migrated_postgres_dsn):
    config = replace(typed_config(migrated_postgres_dsn), watchlist=[{"symbol": "NOTSEEDED", "asset_class": "equity"}])
    result = WorkstationRepository(runtime).status(config)
    assert result["failed_reads"] == []
    assert result["status"] != "available"
    service = result["decision_service"]
    assert service["monitored_count"] == 1 and service["failed_count"] == 1
    assert service["instruments"][0]["owner_jobs"] == ["refresh_assessment_inputs", "refresh_decision_models"]


def test_real_price_feature_and_publisher_deliver_the_same_signal_to_all_readers(runtime, migrated_postgres_dsn, monkeypatch):
    symbols = {"QQQ": False, "BTC-USD": True}
    config = replace(typed_config(migrated_postgres_dsn), watchlist=[
        {"symbol": symbol, "asset_class": "crypto" if crypto else "etf"} for symbol, crypto in symbols.items()])
    seed_history(runtime, symbols)
    universe = monitored_universe(runtime, config.watchlist)
    assert {row["symbol"] for row in universe} == set(symbols)
    repository = AnalysisRepository(runtime)
    now = datetime.now(UTC)
    run = repository.start_run("monitored-symbol-features", input_cutoff=now, code_version="test", inputs={"symbols": list(symbols)})
    result = refresh_symbol_trend_features(runtime, run, as_of=now, symbols=list(symbols))
    assert result["failures"] == [] and result["complete_count"] == 2
    repository.finish_run(run, "succeeded", {"complete_count": 2})
    monkeypatch.setattr(ticker_decisions, "load_config", lambda _: config)
    monkeypatch.setattr(ticker_decisions, "runtime_for_config", lambda _: runtime)
    published = ticker_decisions.publish("fixture", symbols=list(symbols), refresh_outcomes=False)
    assert published["failed_count"] == 0, published
    assert published["signal_failures"] == [], published
    assert published["signal_ready_count"] == 2
    for symbol in symbols:
        decision = TickerDecisionRepository(runtime).latest(symbol)
        signal = decision.reference_signal
        assert signal.action == "BUY_SETUP" and signal.entry_low < signal.entry_high
        assert decision.opportunity_rank["reference_signal"]["signal_id"] == signal.signal_id
        assert decision.trade_plan is not None  # CASH/BLOCKED is a complete allocation decision.
        assert signal.order_authorized is False
    status = WorkstationRepository(runtime).status(config)
    assert status["failed_reads"] == []
    # Conditions are usable, but no account or Market facts were fabricated.
    assert all(item["conditions_status"] == "available" for item in status["decision_service"]["instruments"])
    assert status["decision_service"]["status"] == "failed"
    assert any(item["capability"] == "Capital decisions" for item in status["decision_service"]["incidents"])
    # It is still NOT globally healthy: no scheduler/Market publication is invented.
    assert status["status"] == "partial"
    assert any(item["job"] == "refresh_market_publication" for item in status["blockers"])
    # It is still NOT globally healthy: no scheduler/Market publication is invented.
    with runtime.read() as connection:
        assert connection.execute("SELECT count(*) AS count FROM app.paper_order").fetchone()["count"] == 0


def test_failed_feature_run_is_visible_as_failure_not_hidden_by_older_good_feature(runtime, migrated_postgres_dsn):
    seed_history(runtime, {"QQQ": False})
    config = replace(typed_config(migrated_postgres_dsn), watchlist=[{"symbol": "QQQ", "asset_class": "etf"}])
    repository = AnalysisRepository(runtime)
    now = datetime.now(UTC)
    run = repository.start_run("test-failed-feature", input_cutoff=now, code_version="test", inputs={})
    with runtime.transaction() as connection:
        connection.execute("""INSERT INTO analysis.symbol_feature
            (run_id, instrument_id, as_of, feature_set, feature_version, trend_state, data_quality_status, reason_codes, metrics)
            SELECT %s, id, %s, 'daily_trend', 'daily-trend-v1', 'unavailable', 'unavailable', %s, %s
            FROM catalog.instrument WHERE symbol = 'QQQ'""", [run, now, ["terminal_daily_price_bar_missing"], Jsonb({"bar_count": 199})])
    repository.finish_run(run, "failed", {"reason": "history gap"})
    status = WorkstationRepository(runtime).status(config)
    assert status["failed_reads"] == []
    assert "update_market_data" in status["decision_service"]["instruments"][0]["owner_jobs"]


def test_crypto_candle_observation_is_next_midnight_not_synthetic_same_day(runtime):
    seed_history(runtime, {"BTC-USD": True})
    now = datetime.now(UTC)
    with runtime.read() as connection:
        instrument = connection.execute("SELECT id FROM catalog.instrument WHERE symbol='BTC-USD'").fetchone()["id"]
        bars = confirmed_daily_bars(connection, [instrument], as_of=now)[instrument]
        assert len(bars) == 230
        assert all(row["observed_at"] == datetime.combine(row["trading_date"]+timedelta(days=1), datetime.min.time(), tzinfo=UTC) for row in bars)
        assert all(row["available_at"] <= now for row in bars)


def test_ingestion_finalization_uses_bulk_worker_profile(runtime, monkeypatch):
    from investment_panel.infrastructure.postgres.runtime import API_PROFILE, JOB_PROFILE
    repository = IngestionRepository(runtime)
    repository.register_source("profile-check", name="Profile check", family="test", kind="daily_bars")
    run = repository.start_run("profile-check", "price_bars")
    original = runtime.transaction
    profiles = []
    def transaction(profile=API_PROFILE):
        profiles.append(profile)
        return original(profile)
    monkeypatch.setattr(runtime, "transaction", transaction)
    repository.finish_run(run, "succeeded")
    assert profiles == [JOB_PROFILE]
