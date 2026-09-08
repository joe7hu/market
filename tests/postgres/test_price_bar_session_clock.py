from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from investment_panel.core.decision import market_session_bounds
from investment_panel.database import price_bar_ingestion
from investment_panel.database.confirmed_daily_prices import confirmed_daily_bars
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.panel_watchlist import technical_rows
from investment_panel.database.portfolio_intelligence import portfolio_performance_rows
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.ticker_decisions import TickerDecisionRepository
from conftest import typed_config


@pytest.fixture
def price_ingestion(application_postgres_dsn):
    runtime = DatabaseRuntime(application_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    ingestion.register_source("session-close-test", name="Session close test", family="test", kind="daily_bars")
    try:
        yield ingestion
    finally:
        runtime.close()


def _capture(ingestion, day, **values):
    run_id = ingestion.start_run("session-close-test", "price_bars")
    assert ingestion.store_price_bars(
        run_id, "session-close-test", [{"symbol": "QQQ", "date": day.isoformat(), "close": 500, **values}],
        asset_classes={"QQQ": "etf"},
    ) == 1
    ingestion.finish_run(run_id, "succeeded")
    with ingestion.runtime.read() as connection:
        return connection.execute("SELECT finished_at FROM ingest.run WHERE id = %s", [run_id]).fetchone()["finished_at"]


@pytest.mark.parametrize("day,hour", [
    (date(2026, 8, 12), 20), (date(2026, 1, 2), 21),
    (date(2025, 11, 28), 18), (date(2025, 7, 3), 17), (date(2025, 12, 24), 18),
])
def test_actual_daily_bar_producer_uses_session_close_and_real_availability(price_ingestion, day, hour):
    before = datetime.now(UTC)
    finished_at = _capture(price_ingestion, day)
    with price_ingestion.runtime.read() as connection:
        bar = connection.execute("SELECT * FROM raw.price_bar").fetchone()
        quote = connection.execute("SELECT * FROM raw.quote").fetchone()
        expected = market_session_bounds(day)[1].astimezone(UTC)
        assert expected.hour == hour
        assert bar["observed_at"] == quote["observed_at"] == expected
        assert before <= bar["available_at"] <= quote["available_at"] <= finished_at
        assert confirmed_daily_bars(connection, [bar["instrument_id"]], as_of=before, trading_dates=[day], require_session_close=True) == {}
        confirmed = confirmed_daily_bars(connection, [bar["instrument_id"]], as_of=finished_at, trading_dates=[day], require_session_close=True)[bar["instrument_id"]]
        assert len(confirmed) == 1 and confirmed[0]["observed_at"] == expected
        assert confirmed[0]["available_at"] == bar["available_at"] and confirmed[0]["confirmed_at"] == finished_at
        current = connection.execute("SELECT observed_at, price FROM raw.current_price_at(%s, ARRAY[%s::bigint])", [finished_at, bar["instrument_id"]]).fetchone()
        assert current == {"observed_at": expected, "price": 500}


@pytest.mark.parametrize("day", [date(2026, 1, 2), date(2025, 11, 28)])
def test_reingestion_versions_a_legacy_daily_clock_without_backdating(price_ingestion, monkeypatch, day):
    # Exercise the old producer clock without editing any stored fact clocks.
    with monkeypatch.context() as legacy:
        legacy.setattr(price_bar_ingestion, "market_session_bounds", lambda value: (
            market_session_bounds(value)[0], datetime(value.year, value.month, value.day, 20, tzinfo=UTC),
        ))
        first_finished = _capture(price_ingestion, day)
    with price_ingestion.runtime.read() as connection:
        original = connection.execute("SELECT id, instrument_id, observed_at, available_at FROM raw.price_bar").fetchone()
        original_quote = connection.execute("SELECT id, observed_at, available_at FROM raw.quote").fetchone()
    corrected_finished = _capture(price_ingestion, day)
    with price_ingestion.runtime.read() as connection:
        corrected = connection.execute("SELECT id, observed_at, available_at FROM raw.price_bar").fetchone()
        corrected_quote = connection.execute("SELECT id, observed_at, available_at FROM raw.quote").fetchone()
        assert corrected["id"] == original["id"] and corrected_quote["id"] == original_quote["id"]
        assert corrected["observed_at"] == corrected_quote["observed_at"] == market_session_bounds(day)[1].astimezone(UTC)
        assert first_finished < corrected["available_at"] <= corrected_quote["available_at"] <= corrected_finished
        assert connection.execute("SELECT id, instrument_id, observed_at, available_at FROM raw.price_bar_history").fetchone() == original
        assert connection.execute("SELECT id, observed_at, available_at FROM raw.quote_history").fetchone() == original_quote
        old = confirmed_daily_bars(connection, [original["instrument_id"]], as_of=first_finished, trading_dates=[day], require_session_close=True)[original["instrument_id"]]
        new = confirmed_daily_bars(connection, [original["instrument_id"]], as_of=corrected_finished, trading_dates=[day], require_session_close=True)[original["instrument_id"]]
        assert len(old) == len(new) == 1 and old[0]["observed_at"] == original["observed_at"]
        assert old[0]["available_at"] == original["available_at"] and new[0]["available_at"] == corrected["available_at"]
        current = connection.execute("SELECT observed_at, available_at FROM raw.current_price_at(%s, ARRAY[%s::bigint])", [corrected_finished, original["instrument_id"]]).fetchone()
        assert current == {"observed_at": corrected["observed_at"], "available_at": corrected_quote["available_at"]}
    _capture(price_ingestion, day)
    with price_ingestion.runtime.read() as connection:
        assert connection.execute("SELECT available_at FROM raw.price_bar").fetchone()["available_at"] == corrected["available_at"]
        assert connection.execute("SELECT available_at FROM raw.quote").fetchone()["available_at"] == corrected_quote["available_at"]
        counts = connection.execute("SELECT (SELECT count(*) FROM raw.price_bar_history) AS bars, (SELECT count(*) FROM raw.quote_history) AS quotes").fetchone()
        assert counts == {"bars": 1, "quotes": 1}


@pytest.mark.parametrize("day,legacy_hour", [(date(2026, 1, 2), 20), (date(2026, 8, 12), 17)])
def test_premature_legacy_winter_or_normal_day_1300_bar_stays_unconfirmed(price_ingestion, monkeypatch, day, legacy_hour):
    legacy_clock = datetime(day.year, day.month, day.day, legacy_hour, tzinfo=UTC)
    with monkeypatch.context() as legacy:
        legacy.setattr(price_bar_ingestion, "market_session_bounds", lambda value: (market_session_bounds(value)[0], legacy_clock))
        _capture(price_ingestion, day)
    # This malformed historical fixture asserts refusal, not a qualified close.
    available, confirmed = legacy_clock + timedelta(minutes=1), legacy_clock + timedelta(minutes=2)
    with price_ingestion.runtime.transaction() as connection:
        fact = connection.execute("SELECT id, instrument_id, ingest_run_id FROM raw.price_bar").fetchone()
        quote = connection.execute("SELECT id FROM raw.quote").fetchone()
        connection.execute("UPDATE ingest.run SET finished_at = %s WHERE id = %s", [confirmed, fact["ingest_run_id"]])
        connection.execute("UPDATE raw.price_bar SET available_at = %s WHERE id = %s", [available, fact["id"]])
        connection.execute("UPDATE raw.quote SET available_at = %s WHERE id = %s", [available, quote["id"]])
        connection.execute("UPDATE raw.price_bar_fact_availability SET fact_available_at = %s WHERE fact_id = %s", [available, fact["id"]])
        connection.execute("UPDATE raw.quote_fact_availability SET fact_available_at = %s WHERE fact_id = %s", [available, quote["id"]])
    with price_ingestion.runtime.read() as connection:
        for cutoff in (confirmed, market_session_bounds(day)[1].astimezone(UTC) + timedelta(minutes=1)):
            assert connection.execute("SELECT * FROM raw.current_price_at(%s, ARRAY[%s::bigint])", [cutoff, fact["instrument_id"]]).fetchone() is None
    if legacy_hour == 20:
        repaired_at = _capture(price_ingestion, day)
        with price_ingestion.runtime.read() as connection:
            repaired = connection.execute("SELECT observed_at, available_at FROM raw.current_price_at(%s, ARRAY[%s::bigint])", [repaired_at, fact["instrument_id"]]).fetchone()
            assert repaired["observed_at"] == market_session_bounds(day)[1].astimezone(UTC)
            assert repaired["available_at"] > confirmed


@pytest.mark.parametrize("symbol,asset_class,kind,expected_zone", [
    ("QQQ", "etf", "daily_quote", "America/New_York"),
    ("7203.T", "equity", "daily_bars", "Asia/Tokyo"),
    ("BTC-USD", "crypto", "daily_bars", "UTC"),
])
def test_existing_daily_quote_foreign_and_non_equity_clock_contracts_are_preserved(price_ingestion, symbol, asset_class, kind, expected_zone):
    from zoneinfo import ZoneInfo

    source = f"test-clock-compatibility-{kind}"
    day = date(2026, 1, 2)
    price_ingestion.register_source(source, name=source, family="test", kind=kind)
    run_id = price_ingestion.start_run(source, "price_bars" if kind == "daily_bars" else "quotes")
    if kind == "daily_bars":
        price_ingestion.store_price_bars(run_id, source, [{"symbol": symbol, "date": day.isoformat(), "close": 500}], asset_classes={symbol: asset_class})
    else:
        price_ingestion.store_quotes(run_id, source, [{"symbol": symbol, "price": 500, "observed_at": datetime(2026, 1, 2, 20, tzinfo=UTC)}])
    price_ingestion.finish_run(run_id, "succeeded")
    with price_ingestion.runtime.read() as connection:
        instrument_id = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = %s", [symbol]).fetchone()["id"]
        current = connection.execute("SELECT observed_at, price FROM raw.current_price_at(%s, ARRAY[%s::bigint])", [datetime.now(UTC), instrument_id]).fetchone()
        assert current == {"observed_at": datetime(2026, 1, 2, 16, tzinfo=ZoneInfo(expected_zone)), "price": 500}


def test_corrected_early_close_values_reach_current_analytics_and_keep_old_cutoffs(price_ingestion, migrated_postgres_dsn, monkeypatch):
    day = date(2025, 11, 28)
    before = datetime.now(UTC)
    with monkeypatch.context() as legacy:
        legacy.setattr(price_bar_ingestion, "market_session_bounds", lambda value: (
            market_session_bounds(value)[0], datetime(value.year, value.month, value.day, 20, tzinfo=UTC),
        ))
        original_at = _capture(price_ingestion, day, open=490, high=510, low=480, volume=100)
    corrected_at = _capture(price_ingestion, day, open=510, high=560, low=500, close=550, volume=200)
    revised_at = _capture(price_ingestion, day, open=520, high=580, low=510, close=575, volume=300)
    # Only the pre-existing portfolio position is seeded by its fixture owner.
    # All source writes and current/historical reads use the application login.
    with psycopg.connect(migrated_postgres_dsn) as connection:
        instrument_id = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = 'QQQ'").fetchone()[0]
        connection.execute(
            """INSERT INTO app.portfolio_transaction
               (instrument_id, transaction_type, quantity, price, amount, executed_at, idempotency_key)
               VALUES (%s, 'opening_balance', 1, 400, 400, %s, 'clock-correction-holding')""",
            [instrument_id, market_session_bounds(day)[0]],
        )
    with price_ingestion.runtime.read() as connection:
        assert connection.execute("SELECT current_user AS role").fetchone()["role"] == "market_app"
        for cutoff, expected in ((before, None), (original_at, 500), (corrected_at, 550), (revised_at, 575)):
            bars = connection.execute("SELECT close FROM raw.confirmed_price_bar_at(%s, ARRAY[%s::bigint])", [cutoff, instrument_id]).fetchall()
            quotes = connection.execute("SELECT price FROM raw.confirmed_quote_at(%s, ARRAY[%s::bigint])", [cutoff, instrument_id]).fetchall()
            assert [row["close"] for row in bars] == ([] if expected is None else [expected])
            assert [row["price"] for row in quotes] == ([] if expected is None else [expected])
        assert connection.execute("SELECT * FROM raw.confirmed_quote_at(%s, ARRAY[]::bigint[])", [revised_at]).fetchall() == []
        assert connection.execute("SELECT close FROM raw.confirmed_price_bar").fetchall() == [{"close": 575}]
        assert connection.execute("SELECT price FROM raw.confirmed_quote").fetchall() == [{"price": 575}]
        for scope in (None, {"QQQ"}):
            technical = technical_rows(connection, symbols=scope)
            assert len(technical) == 1
            assert technical[0]["price"] == technical[0]["sma_20"] == 575
            assert technical[0]["as_of"] == market_session_bounds(day)[1]
            assert technical[0]["chart_1y"] == [{"date": day.isoformat(), "close": 575}]
            assert technical[0]["volume_1m_bars"] == [{"date": day.isoformat(), "value": 300}]
        performance = portfolio_performance_rows(typed_config(), connection=connection)
        assert performance[-1]["portfolio_value"] == 575
        assert performance[-1]["total_pnl"] == 175
    pending_run = price_ingestion.start_run("session-close-test", "price_bars")
    price_ingestion.store_price_bars(pending_run, "session-close-test", [{"symbol": "QQQ", "date": day.isoformat(), "close": 800}])
    with price_ingestion.runtime.read() as connection:
        # A newer but unconfirmed correction cannot hide the last eligible version.
        assert connection.execute("SELECT close FROM raw.confirmed_price_bar").fetchall() == [{"close": 575}]
        assert connection.execute("SELECT price FROM raw.confirmed_quote").fetchall() == [{"price": 575}]
    price_ingestion.finish_run(pending_run, "failed")


def test_peer_return_keeps_entry_version_at_its_own_cutoff_after_correction(price_ingestion, monkeypatch):
    entry_day, mark_day = date(2025, 11, 28), date(2025, 12, 1)
    with monkeypatch.context() as legacy:
        legacy.setattr(price_bar_ingestion, "market_session_bounds", lambda value: (
            market_session_bounds(value)[0], datetime(value.year, value.month, value.day, 20, tzinfo=UTC),
        ))
        entry_cutoff = _capture(price_ingestion, entry_day, close=500)
    _capture(price_ingestion, entry_day, close=550)
    mark_cutoff = _capture(price_ingestion, mark_day, close=600)
    observed = TickerDecisionRepository(price_ingestion.runtime)._peer_return(
        ["QQQ"], entry_day, mark_day, entry_cutoff, mark_cutoff,
    )
    assert observed == pytest.approx(600 / 500 - 1)
